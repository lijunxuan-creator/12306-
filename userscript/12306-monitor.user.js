// ==UserScript==
// @name         12306 余票监控助手（本地服务版）
// @namespace    local.t12306.monitor
// @version      1.10.0
// @description  从本机监控服务领取查询任务，在你自己已登录的 12306 页面里驱动查询、解析余票表格并回报；命中后弹提示、可选自动进入预订页并预填乘客与席别。不代登录、不代提交订单、不代付款。
// @author       you
// @match        https://kyfw.12306.cn/otn/leftTicket/*
// @match        https://kyfw.12306.cn/otn/confirmPassenger/*
// @grant        GM_xmlhttpRequest
// @updateURL    http://127.0.0.1:8765/userscript/12306-monitor.user.js
// @downloadURL  http://127.0.0.1:8765/userscript/12306-monitor.user.js
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-idle
// @noframes
// ==/UserScript==

(function () {
  "use strict";

  /* ======================================================================
   * 1. 配置
   * ==================================================================== */
  var CFG = {
    server: "http://127.0.0.1:8765", // 与 config.json 里 server 保持一致
    token: "",                       // 若配置了 server.token，填同样的值
    autoJump: true,                  // 服务端下发 jump_booking 时才真正跳转
    autoSubmit: true,                // 允许脚本代点「提交订单」，验证码仍需本人完成
    ticketType: "成人票",            // 订单页强制统一的票种（学生票需资质核验，否则出票失败 WT.9901）
    debug: false
  };

  // 顺序有意义：12306 的表头会把席位合并成一格显示成两行，
  // 例如「商务座/特等座」「二等座/二等包座」「软卧/动卧/一等卧」「硬卧/二等卧」。
  // 匹配时先命中更具体的名字，所以「高级软卧」必须排在「软卧」前、「软卧」排在「动卧」前，
  // 否则「软卧/动卧/一等卧」这一列会被错认成「动卧」。
  var SEAT_ORDER = [
    "优选一等座", "商务座", "特等座", "一等座", "二等座",
    "高级软卧", "软卧", "动卧", "硬卧", "软座", "硬座", "无座"
  ];
  var AVAIL_RE = /^(有|\d{1,3}|无|--|—|-|候补|\*)$/;
  var PENDING_KEY = "T12306_PENDING_ACTION";
  var CLIENT_ID = "tab-" + Math.random().toString(36).slice(2, 10);

  var W = (typeof unsafeWindow !== "undefined") ? unsafeWindow : window;
  var state = { paused: false, hits: 0, cycles: 0, lastPlanId: null, busy: false };

  /* ======================================================================
   * 2. 基础工具
   * ==================================================================== */
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function log() {
    if (!CFG.debug) return;
    try { console.log.apply(console, ["[12306-monitor]"].concat([].slice.call(arguments))); } catch (e) {}
  }
  function q(sel, root) { return (root || document).querySelector(sel); }
  function qa(sel, root) { return [].slice.call((root || document).querySelectorAll(sel)); }
  function cleanText(el) {
    if (!el) return "";
    return String(el.textContent || "").replace(/\s+/g, "").trim();
  }

  function api(method, path, body) {
    var url = CFG.server + path;
    if (CFG.token) url += (url.indexOf("?") >= 0 ? "&" : "?") + "token=" + encodeURIComponent(CFG.token);
    var data = body ? JSON.stringify(body) : undefined;
    var headers = { "Content-Type": "application/json" };
    if (CFG.token) headers["X-Token"] = CFG.token;

    if (typeof GM_xmlhttpRequest === "function") {
      return new Promise(function (resolve, reject) {
        GM_xmlhttpRequest({
          method: method,
          url: url,
          headers: headers,
          data: data,
          timeout: 20000,
          onload: function (res) {
            try { resolve(JSON.parse(res.responseText)); }
            catch (e) { reject(new Error("响应不是 JSON：" + String(res.responseText).slice(0, 120))); }
          },
          onerror: function () { reject(new Error("无法连接本地监控服务")); },
          ontimeout: function () { reject(new Error("本地监控服务超时")); }
        });
      });
    }
    return fetch(url, { method: method, headers: headers, body: data }).then(function (r) {
      return r.json();
    });
  }

  /* ======================================================================
   * 3. 浮动面板
   * ==================================================================== */
  var panelEls = {};

  function buildPanel() {
    if (!document.body || q("#t12306-panel")) return;
    var style = document.createElement("style");
    style.textContent =
      "#t12306-panel{position:fixed;right:14px;bottom:14px;z-index:2147483000;width:262px;" +
      "background:#111820f2;color:#e6edf3;border:1px solid #2b3648;border-radius:12px;" +
      "box-shadow:0 8px 28px #00000066;font:12.5px/1.55 'Microsoft YaHei',system-ui,sans-serif;" +
      "padding:10px 12px;backdrop-filter:blur(6px)}" +
      "#t12306-panel .hd{display:flex;align-items:center;gap:6px;font-weight:600;font-size:13px;margin-bottom:6px}" +
      "#t12306-panel .dot{width:8px;height:8px;border-radius:50%;background:#f85149;display:inline-block}" +
      "#t12306-panel .dot.on{background:#2ea043;box-shadow:0 0 7px #2ea043}" +
      "#t12306-panel .ln{color:#9fb0c6;word-break:break-all;min-height:18px}" +
      "#t12306-panel .hit{color:#4ade80;font-weight:600}" +
      "#t12306-panel .err{color:#f87171}" +
      "#t12306-panel .acts{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}" +
      "#t12306-panel button{flex:1;min-width:62px;background:#1d2733;color:#dbe5f1;border:1px solid #2f3b4d;" +
      "border-radius:7px;padding:4px 6px;font-size:12px;cursor:pointer}" +
      "#t12306-panel button:hover{background:#25313f}" +
      "#t12306-panel a{color:#6cb6ff;text-decoration:none;font-size:12px;align-self:center}";
    document.head.appendChild(style);

    var box = document.createElement("div");
    box.id = "t12306-panel";
    box.innerHTML =
      '<div class="hd"><span class="dot" id="t12306-dot"></span>12306 监控</div>' +
      '<div class="ln" id="t12306-status">连接本地服务…</div>' +
      '<div class="ln" id="t12306-last">—</div>' +
      '<div class="acts">' +
      '<button id="t12306-toggle">暂停</button>' +
      '<button id="t12306-force">立即查询</button>' +
      '<a href="' + CFG.server + '/" target="_blank">看板</a>' +
      "</div>";
    document.body.appendChild(box);

    panelEls.dot = q("#t12306-dot");
    panelEls.status = q("#t12306-status");
    panelEls.last = q("#t12306-last");

    q("#t12306-toggle").addEventListener("click", function () {
      state.paused = !state.paused;
      this.textContent = state.paused ? "继续" : "暂停";
      setStatus(state.paused ? "已暂停（点「继续」恢复）" : "运行中");
    });
    q("#t12306-force").addEventListener("click", function () {
      state.force = true;
      setStatus("准备立即查询…");
    });
  }

  function setStatus(text, kind) {
    if (!panelEls.status) return;
    panelEls.status.textContent = text;
    panelEls.status.className = "ln" + (kind === "err" ? " err" : kind === "hit" ? " hit" : "");
  }
  function setOnline(on) {
    if (panelEls.dot) panelEls.dot.className = "dot" + (on ? " on" : "");
  }
  function setLast(text) {
    if (panelEls.last) panelEls.last.textContent = text;
  }

  /* ======================================================================
   * 4. 页面适配：表单 / 表头 / 表格
   * ==================================================================== */
  function findQueryForm() {
    return {
      fromText: q("#fromStationText"),
      fromCode: q("#fromStation"),
      toText: q("#toStationText"),
      toCode: q("#toStation"),
      date: q("#train_date"),
      button: q("#query_ticket") || q("#queryTicket") || qa("a,button").filter(function (el) {
        return cleanText(el) === "查询";
      })[0]
    };
  }

  function setValue(el, value) {
    if (!el) return false;
    el.value = value;
    el.setAttribute("value", value);
    ["input", "change", "blur"].forEach(function (type) {
      try { el.dispatchEvent(new Event(type, { bubbles: true })); } catch (e) {}
    });
    return true;
  }

  function applyPlan(plan) {
    var f = findQueryForm();
    if (!f.fromCode || !f.toCode || !f.date) return false;
    if (f.fromText) setValue(f.fromText, plan.from.name);
    if (f.toText) setValue(f.toText, plan.to.name);
    setValue(f.date, plan.date);
    // 站名的自动补全可能把隐藏域清空，所以最后再写一次
    f.fromCode.value = plan.from.code;
    f.toCode.value = plan.to.code;
    return true;
  }

  function formMatchesPlan(plan) {
    var f = findQueryForm();
    if (!f.fromCode || !f.toCode || !f.date) return false;
    return (
      String(f.fromCode.value).toUpperCase() === plan.from.code &&
      String(f.toCode.value).toUpperCase() === plan.to.code &&
      String(f.date.value).slice(0, 10) === plan.date
    );
  }

  function clickQuery() {
    var f = findQueryForm();
    if (!f.button) return false;
    f.button.click();
    return true;
  }

  // 表格每次重绘都会触发 childList 变更，用它作为「刷新完成」的信号
  var render = { gen: 0 };
  var tableObserver = null;
  var watchedNode = null;

  function watchTable() {
    var tbody = q("#queryLeftTable");
    if (!tbody) return false;
    // 12306 偶尔整块替换 tbody，此时旧 observer 会失效，需要重新挂
    if (tableObserver && watchedNode === tbody && document.contains(tbody)) return true;
    if (tableObserver) tableObserver.disconnect();
    tableObserver = new MutationObserver(function () { render.gen += 1; });
    tableObserver.observe(tbody, { childList: true, subtree: true });
    watchedNode = tbody;
    return true;
  }

  /** 一行实际占多少列（把 colspan 算进去） */
  function tableWidth(tr) {
    var col = 0;
    [].slice.call(tr.children).forEach(function (cell) {
      if (cell.tagName !== "TD") return;
      var span = parseInt(cell.getAttribute("colspan") || "1", 10);
      col += (span && span > 0) ? span : 1;
    });
    return col;
  }

  /** 取数据行的众数列宽，用来校验表头是不是同一张表的 */
  function dataRowWidth(tbody) {
    var counts = {};
    qa("tr", tbody).forEach(function (tr) {
      var width = tableWidth(tr);
      if (width >= 4) counts[width] = (counts[width] || 0) + 1;
    });
    var best = 0, bestCount = 0;
    Object.keys(counts).forEach(function (key) {
      var width = Number(key);
      if (counts[key] > bestCount || (counts[key] === bestCount && width > best)) {
        best = width;
        bestCount = counts[key];
      }
    });
    return best;
  }

  /** 把 td 展开成「列号 → 单元格」，这样 colspan 合并格也能对到正确的列 */
  function cellColumns(tr) {
    var map = [];
    var col = 0;
    [].slice.call(tr.children).forEach(function (cell) {
      if (cell.tagName !== "TD") return;
      var span = parseInt(cell.getAttribute("colspan") || "1", 10);
      if (!span || span < 1) span = 1;
      for (var k = 0; k < span; k++) map[col + k] = cell;
      col += span;
    });
    return map;
  }

  function headerCandidates(tbody) {
    var table = tbody.closest ? tbody.closest("table") : null;
    var rows = table ? qa("thead tr", table) : [];
    if (!rows.length) {
      var header = q("#queryLeftHeader");
      if (header) rows = qa("tr", header);
    }

    var candidates = [];
    rows.forEach(function (tr) {
      var labels = qa("th", tr).map(cleanText);
      if (labels.length >= 4) candidates.push(labels);
    });

    // 表头有时被拆到页面另一张表里，退一步收集全页所有表头
    if (!candidates.length) {
      qa("thead").forEach(function (head) {
        var labels = qa("th", head).map(cleanText);
        if (labels.length >= 4) candidates.push(labels);
      });
    }

    var seen = {};
    return candidates.filter(function (labels) {
      var key = labels.join("|");
      if (seen[key]) return false;
      seen[key] = true;
      return true;
    });
  }

  /** 表头必须和数据行同宽，否则席别会整体错位。
   *  12306 有时把表头放在另一张表里，此时「挑列数最多的」会挑错，所以这里按数据行宽度精确匹配。 */
  function headerLabels(tbody) {
    var candidates = headerCandidates(tbody);
    if (!candidates.length) return [];
    var width = dataRowWidth(tbody);
    if (!width) {
      candidates.sort(function (a, b) { return b.length - a.length; });
      return candidates[0];
    }
    for (var i = 0; i < candidates.length; i++) {
      if (candidates[i].length === width) return candidates[i];
    }
    candidates.sort(function (a, b) {
      return Math.abs(a.length - width) - Math.abs(b.length - width);
    });
    return candidates[0];
  }

  function matchSeatName(label) {
    for (var i = 0; i < SEAT_ORDER.length; i++) {
      if (label.indexOf(SEAT_ORDER[i]) >= 0) return SEAT_ORDER[i];
    }
    return null;
  }

  function cellIndexBy(header, keyword) {
    for (var i = 0; i < header.length; i++) {
      if (header[i].indexOf(keyword) >= 0) return i;
    }
    return -1;
  }

  function rowCells(tr) {
    return [].slice.call(tr.children).filter(function (c) { return c.tagName === "TD"; });
  }

  function extractTrainNo(tr, cells) {
    var el = tr.querySelector("a.number, .number, .train a");
    if (el) {
      var text = cleanText(el);
      if (text) return text;
    }
    var head = cells.slice(0, 2).map(cleanText).join(" ");
    var m = head.match(/([GDCZTKYL]\d{1,4})/i);
    return m ? m[1].toUpperCase() : "";
  }

  function extractTimes(tr, cells, header) {
    var dep = "", arr = "";
    var idx = cellIndexBy(header, "出发时间");
    if (idx < 0) idx = cellIndexBy(header, "到达时间");
    if (idx >= 0 && cells[idx]) {
      var strongs = qa("strong", cells[idx]);
      if (strongs.length >= 2) { dep = cleanText(strongs[0]); arr = cleanText(strongs[1]); }
      else {
        var tm = cleanText(cells[idx]).match(/(\d{2}:\d{2})/g) || [];
        dep = tm[0] || ""; arr = tm[1] || "";
      }
    }
    var didx = cellIndexBy(header, "历时");
    var duration = didx >= 0 && cells[didx] ? cleanText(cells[didx]) : "";
    return { dep: dep, arr: arr, duration: duration };
  }

  function parseTable(plan) {
    var tbody = q("#queryLeftTable");
    if (!tbody) return { rows: [], header: [], error: "找不到余票表格（#queryLeftTable）" };
    var header = headerLabels(tbody);
    if (!header.length) {
      return { rows: [], header: [], error: "无法识别表头，页面结构可能已变化", warnings: ["header_missing"] };
    }

    var warnings = [];
    var width = dataRowWidth(tbody);
    if (width && width !== header.length) {
      warnings.push("表头 " + header.length + " 列、数据行 " + width + " 列，席别可能错位");
    }

    var seatCols = [];
    header.forEach(function (label, index) {
      var name = matchSeatName(label);
      if (name) seatCols.push({ index: index, name: name });
    });
    if (!seatCols.length) warnings.push("表头里没有识别出任何席别");

    var stationIdx = cellIndexBy(header, "出发站");
    var wantFrom = (plan && plan.from && plan.from.name) || "";
    var wantTo = (plan && plan.to && plan.to.name) || "";
    var routeMismatch = 0;

    var rows = [];
    qa("tr", tbody).forEach(function (tr) {
      var cells = rowCells(tr);
      if (cells.length < 4) return;
      var train = extractTrainNo(tr, cells);
      if (!train) return;

      var columns = cellColumns(tr);

      // 页面上的车次和计划路线对不上，说明表格还是上一次查询的结果，读它只会报错票
      if (stationIdx >= 0 && wantFrom && wantTo && columns[stationIdx]) {
        var stationText = cleanText(columns[stationIdx]);
        if (stationText.indexOf(wantFrom) < 0 || stationText.indexOf(wantTo) < 0) {
          routeMismatch += 1;
          return;
        }
      }

      var seats = {};
      seatCols.forEach(function (col) {
        var cell = columns[col.index];
        if (!cell) return;
        var text = cleanText(cell);
        if (!text) return;
        seats[col.name] = text;
      });
      if (!Object.keys(seats).length) return; // 例如「同城车站」提示行

      var times = extractTimes(tr, columns, header);
      var bookable = qa("a,button", tr).some(function (el) {
        return cleanText(el).indexOf("预订") >= 0;
      });
      rows.push({
        train: train,
        dep: times.dep,
        arr: times.arr,
        duration: times.duration,
        bookable: bookable,
        seats: seats
      });
    });

    if (!rows.length) {
      var reason = routeMismatch
        ? "页面上的车次不是 " + wantFrom + "→" + wantTo + "（页面可能还停在上一次查询）"
        : "表格里没有解析出车次";
      return { rows: [], header: header, warnings: warnings, error: reason };
    }
    return { rows: rows, header: header, warnings: warnings };
  }

  /* ======================================================================
   * 5. 验证码 / 异常识别
   * ==================================================================== */
  function humanCheckNeeded() {
    var selectors = ["#nc_1_wrapper", ".nc-container", "#nc_1_n1z", ".slide-verify", "#captcha_img"];
    for (var i = 0; i < selectors.length; i++) {
      var el = q(selectors[i]);
      if (el && el.offsetParent !== null) return true;
    }
    return false;
  }

  /** 只在「可见」的提示框里找关键词，避免隐藏模板误伤（否则正常的订单页也会被判成未登录） */
  function visibleText(selectors) {
    for (var i = 0; i < selectors.length; i++) {
      var els = qa(selectors[i]);
      for (var j = 0; j < els.length; j++) {
        var el = els[j];
        if (!el || el.offsetParent === null) continue;
        var text = cleanText(el);
        if (text) return text;
      }
    }
    return "";
  }

  /** 12306 会话失效时会把人踢回登录页或弹提示，这时必须停手 */
  function loginExpired() {
    if (/\/otn\/login|\/passport\/|login\/init/i.test(location.pathname + location.search)) {
      return true;
    }
    var text = visibleText([
      ".layui-layer-content", ".dialog-content", ".error_msg", "#msg_error", ".alert-danger"
    ]);
    return /登录信息已过期|请重新登录|登录超时|账号已退出/.test(text);
  }

  function pageErrorText() {
    var candidates = [".alert-danger", "#query_ticket_tip", ".error_msg", "#msg_error"];
    for (var i = 0; i < candidates.length; i++) {
      var el = q(candidates[i]);
      if (el && el.offsetParent !== null && cleanText(el)) return cleanText(el).slice(0, 120);
    }
    return "";
  }

  /* ======================================================================
   * 6. 命中后的动作
   * ==================================================================== */
  function findTrainRow(trainNo) {
    var tbody = q("#queryLeftTable");
    if (!tbody) return null;
    var wanted = trainNo.toUpperCase().replace(/\s/g, "");
    var found = null;
    qa("tr", tbody).forEach(function (tr) {
      if (found) return;
      var cells = rowCells(tr);
      if (!cells.length) return;
      if (extractTrainNo(tr, cells) === wanted) found = tr;
    });
    return found;
  }

  function highlightRow(tr) {
    if (!tr) return;
    tr.style.outline = "2px solid #2ea043";
    tr.style.outlineOffset = "-2px";
    try { tr.scrollIntoView({ block: "center", behavior: "smooth" }); } catch (e) {}
  }

  function clickBook(tr) {
    if (!tr) return false;
    var btn = q("a.btn72", tr) || qa("a", tr).filter(function (a) {
      return cleanText(a).indexOf("预订") >= 0;
    })[0];
    if (!btn) return false;
    btn.click();
    return true;
  }

  function handleActions(actions) {
    (actions || []).forEach(function (action) {
      if (action.type !== "jump_booking") return;
      var tr = findTrainRow(action.train);
      highlightRow(tr);
      state.hits += 1;
      setStatus("命中 " + action.train + " " + action.seat + "（" + action.date + "）", "hit");
      setLast("累计命中 " + state.hits + " 次 · 已查询 " + state.cycles + " 轮");
      if (!CFG.autoJump) return;
      try { sessionStorage.setItem(PENDING_KEY, JSON.stringify(action)); } catch (e) {}
      setTimeout(function () {
        if (!clickBook(tr)) {
          setStatus("命中但没找到「预订」按钮，请手动点击", "err");
        }
      }, 400);
    });
  }

  /* ======================================================================
   * 7. 主循环（查票页）
   * ==================================================================== */
  async function queryLoop() {
    buildPanel();
    while (true) {
      watchTable(); // 表格可能被页面整块重建，每轮都确认一次监听
      if (state.paused) { await sleep(800); continue; }

      // 被 12306 踢回登录页时立刻停手，别对着登录页空转刷请求
      if (loginExpired()) {
        if (!state.paused) {
          state.paused = true;
          var toggle = q("#t12306-toggle");
          if (toggle) toggle.textContent = "继续";
          setStatus("登录已过期：请重新登录，然后点「继续」", "err");
          try { await api("POST", "/api/debug", { event: "login_expired", href: location.href }); } catch (e) {}
        }
        await sleep(2000);
        continue;
      }
      if (state.busy) { await sleep(500); continue; }

      if (humanCheckNeeded()) {
        setStatus("检测到人机验证，请在页面上手动完成", "err");
        await sleep(2500);
        continue;
      }

      state.busy = true;
      try {
        var response = await api("POST", "/api/plan/next", {
          client_id: CLIENT_ID,
          page: "leftTicket",
          force: !!state.force
        });
        state.force = false;

        if (!response || response.idle) {
          setOnline(true);
          setStatus(state.paused ? "已暂停" : "待命中（等待服务端调度）");
          await sleep(Math.max(0.8, Number(response && response.retry_after) || 2) * 1000);
          continue;
        }

        var plan = response.plan;
        state.lastPlanId = plan.id;
        if (!applyPlan(plan)) {
          setStatus("页面上找不到查票表单", "err");
          await api("POST", "/api/plan/" + plan.id + "/report", {
            ok: false,
            error: "表单元素缺失（fromStation/toStation/train_date）"
          });
          await sleep(3000);
          continue;
        }

        setStatus("查询 " + plan.date + " " + plan.route);
        var genBefore = render.gen;
        if (!clickQuery()) {
          await api("POST", "/api/plan/" + plan.id + "/report", { ok: false, error: "找不到查询按钮" });
          await sleep(3000);
          continue;
        }

        var ready = await waitRerender(genBefore, plan);
        if (!ready) {
          var errText = pageErrorText();
          await api("POST", "/api/plan/" + plan.id + "/report", {
            ok: false,
            error: errText || "等待查询结果超时"
          });
          setStatus("本轮未拿到结果，稍后重试", "err");
          await sleep(1200);
          continue;
        }

        var parsed = parseTable(plan);
        var report = await api("POST", "/api/plan/" + plan.id + "/report", {
          ok: !parsed.error,
          rows: parsed.rows || [],
          header: parsed.header || [],
          warnings: parsed.warnings || [],
          error: parsed.error || ""
        });
        state.cycles += 1;
        setOnline(true);

        var msg = parsed.error
          ? "解析失败：" + parsed.error
          : parsed.rows.length + " 个车次，命中 " + ((report && report.new_alerts) || []).length + " 处";
        setStatus(msg, parsed.error ? "err" : "ok");
        setLast("累计命中 " + state.hits + " 次 · 已查询 " + state.cycles + " 轮");
        if (report && report.actions) handleActions(report.actions);
      } catch (err) {
        setOnline(false);
        setStatus("本地服务未连接：" + (err && err.message ? err.message : err), "err");
        await sleep(3000);
      } finally {
        state.busy = false;
      }

      await sleep(120);
    }
  }

  async function waitRerender(genBefore, plan) {
    var deadline = Date.now() + 25000;
    while (Date.now() < deadline) {
      await sleep(180);
      if (humanCheckNeeded()) return false;
      if (render.gen === genBefore) continue;
      if (!formMatchesPlan(plan)) return false; // 表单被页面重置，结果与目标不符
      // 12306 会先清空 tbody 再填行，这时立刻解析会读到空表，别急着判定失败
      if (await waitForRows(6000)) return true;
      return false;
    }
    return false;
  }

  /** 表格里真正出现车次行才算渲染完成 */
  function rowCount() {
    var tbody = q("#queryLeftTable");
    if (!tbody) return 0;
    var count = 0;
    qa("tr", tbody).forEach(function (tr) {
      if (extractTrainNo(tr, rowCells(tr))) count += 1;
    });
    return count;
  }

  async function waitForRows(timeoutMs) {
    var deadline = Date.now() + (timeoutMs || 6000);
    while (Date.now() < deadline) {
      if (rowCount() > 0) return true;
      await sleep(180);
    }
    return false;
  }

  /* ======================================================================
   * 8. 订单确认页：预填乘客/席别
   * ==================================================================== */
  function banner(text) {
    var box = document.createElement("div");
    box.style.cssText =
      "position:fixed;left:0;right:0;top:0;z-index:2147483000;background:#17361f;color:#d7f5e0;" +
      "border-bottom:2px solid #2ea043;padding:10px 16px;font:13px/1.6 'Microsoft YaHei',system-ui;" +
      "text-align:center";
    box.textContent = text;
    document.body.appendChild(box);
    setTimeout(function () { box.style.transition = "opacity .6s"; box.style.opacity = "0"; }, 12000);
    setTimeout(function () { box.remove(); }, 13000);
  }

  function findSubmitButton() {
    return q("#submitOrder_id") || qa("a,button,input").filter(function (el) {
      var text = cleanText(el) || String(el.value || "");
      return text.indexOf("提交订单") >= 0 || text.indexOf("确认订单") >= 0;
    })[0];
  }

  /** 等订单真正生成：验证码通过后会离开确认页 / 出现支付入口 / 出现「订单提交成功」 */
  async function waitOrderCreated(timeoutMs) {
    var deadline = Date.now() + (timeoutMs || 90000);
    while (Date.now() < deadline) {
      await sleep(700);
      if (/\/otn\/payOrder\//.test(location.pathname)) return true;
      if (!/\/otn\/confirmPassenger\//.test(location.pathname)) return true;
      if (q("#payButton, .opay, #pay_id, .pay-btn")) return true;
      if (/订单提交成功|未完成订单|请在.{0,6}30分钟内/.test(cleanText(document.body).slice(0, 6000))) {
        return true;
      }
    }
    return false;
  }

  /** 乘客列表在不同版本里位置不一样，多试几个选择器 */
  function findPassengerItems() {
    var selectors = [
      "#normal_passenger_id li",
      ".pass-list li",
      ".passenger-list li",
      ".pass_list li",
      ".passenger-item",
      "#passenger_list li"
    ];
    for (var i = 0; i < selectors.length; i++) {
      var items = qa(selectors[i]);
      if (items.length) return items;
    }
    return [];
  }

  /** 新版把常用联系人藏在「输入乘客姓名」框后面（id=quickQueryPassenger_id），点一下才会展开 */
  function openPassengerPicker() {
    var input =
      q("#quickQueryPassenger_id") ||
      q("input[placeholder*='乘客姓名']") ||
      q("#qg_input") ||
      q("#search_passenger") ||
      q("#passenger_name_search");
    if (!input) return false;
    try {
      input.click();
      input.focus();
    } catch (e) {}
    ["focus", "click", "input", "keydown", "keyup"].forEach(function (type) {
      try {
        input.dispatchEvent(new Event(type, { bubbles: true }));
      } catch (e) {}
    });
    // 有些版本列表挂在这个容器上，点一下容器也能触发
    var list = q("#normal_passenger_id");
    if (list && !qa("li", list).length) {
      try {
        list.click();
      } catch (e) {}
    }
    return true;
  }

  /** 订单页结构对不上时，把现场信息发给本地服务，省得人工开 F12 */
  function collectConfirmDiagnostics() {
    var info = {
      href: location.href,
      normalList: qa("#normal_passenger_id li").length,
      normalListExists: !!q("#normal_passenger_id"),
      counts: {},
      inputs: [],
      selects: [],
      tableHtml: ""
    };
    [
      "#normal_passenger_id li",
      ".pass-list li",
      ".passenger-list li",
      ".pass_list li",
      ".passenger-item",
      "#passenger_list li"
    ].forEach(function (sel) {
      info.counts[sel] = qa(sel).length;
    });
    qa("input").slice(0, 40).forEach(function (el) {
      info.inputs.push([el.id || "", el.name || "", el.placeholder || "", el.type || ""].join("|"));
    });
    qa("select").slice(0, 20).forEach(function (el) {
      info.selects.push([el.id || "", el.name || "", cleanText(el).slice(0, 40)].join("|"));
    });
    qa("table").forEach(function (table) {
      if (info.tableHtml) return;
      if (cleanText(table).indexOf("票种") >= 0) {
        info.tableHtml = String(table.innerHTML || "").slice(0, 2500);
      }
    });
    if (!info.tableHtml) {
      info.tableHtml = String(document.body.innerHTML || "").slice(0, 1500);
    }
    return info;
  }

  /** 12306 有时会给乘客默认勾「学生票」，没做优惠资质核验就会出票失败（WT.9901）。
   *  这里把所有票种统一拨成 CFG.ticketType（默认成人票）。 */
  function forceTicketType(items) {
    var want = CFG.ticketType;
    if (!want) return 0;
    var changed = 0;

    function pickWant(select) {
      var option = [].slice.call(select.options || []).filter(function (o) {
        return cleanText(o).indexOf(want) >= 0;
      })[0];
      if (!option) return false;
      if (select.value === option.value) return false;
      select.value = option.value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }

    (items || []).forEach(function (li) {
      qa("select", li).forEach(function (select) {
        if (pickWant(select)) changed += 1;
      });
      qa("input[type=radio]", li).forEach(function (radio) {
        var holder = radio.parentNode || li;
        if (cleanText(holder).indexOf(want) >= 0 && !radio.checked) {
          radio.click();
          changed += 1;
        }
      });
    });

    // 兜底：页面上任何像「票种」的下拉框
    qa("select").forEach(function (select) {
      var text = cleanText(select);
      var name = String(select.name || "") + String(select.id || "");
      var looksLikeTicketType =
        /票种|ticket/i.test(name) || (text.indexOf("学生票") >= 0 && text.indexOf(want) >= 0);
      if (looksLikeTicketType && pickWant(select)) changed += 1;
    });

    if (changed && CFG.debug) log("票种已改为", want, "共", changed, "处");
    return changed;
  }

  /** 订单页上的「用户未登录」弹窗 = 会话已失效，这一单做不了了 */
  function confirmLoginBroken() {
    if (/\/otn\/login|\/passport\//i.test(location.pathname)) return true;
    var text = visibleText([
      ".layui-layer-content", ".dialog-content", ".error_msg", "#msg_error", ".alert-danger"
    ]);
    return /用户未登录|请先登录|登录已过期|请重新登录/.test(text);
  }

  async function prefillConfirmPage() {
    var raw = null;
    try { raw = sessionStorage.getItem(PENDING_KEY); } catch (e) {}
    if (!raw) return;
    var action;
    try { action = JSON.parse(raw); } catch (e) { return; }
    try { sessionStorage.removeItem(PENDING_KEY); } catch (e) {}

    var willSubmit = !!(action.submit && CFG.autoSubmit);
    banner(
      willSubmit
        ? "已命中 " + action.shortcut + "：正在自动提交订单。看到验证码请马上完成（约 2 秒），通过后座位就锁定了。"
        : "已命中 " + action.shortcut + "：已预填乘客与席别，请核对后点「提交订单」。"
    );

    // 乘客列表可能异步渲染，重试至多 8 秒
    var deadline = Date.now() + 8000;
    var done = false;
    var passengerItems = [];
    var triedPicker = false;
    while (Date.now() < deadline && !done) {
      passengerItems = findPassengerItems();
      if (!passengerItems.length) {
        if (!triedPicker) {
          triedPicker = openPassengerPicker();
          await sleep(400);
          passengerItems = findPassengerItems();
          if (passengerItems.length) continue;
        }
        await sleep(500);
        continue;
      }
      if (action.passengers && action.passengers.length) {
        passengerItems.forEach(function (li) {
          var cb = q("input[type=checkbox]", li);
          if (!cb) return;
          var name = cleanText(li.querySelector("label") || li);
          var want = action.passengers.some(function (p) { return p && name.indexOf(p) >= 0; });
          if (want && !cb.checked) cb.click();
        });
      } else {
        passengerItems.forEach(function (li) { li.style.outline = "1px dashed #2ea043"; });
      }
      done = true;
    }

    // 一个乘客都没找到：绝不盲目提交（空单会被 12306 拒绝，还可能算异常操作）
    if (!passengerItems.length) {
      if (confirmLoginBroken()) {
        banner("登录已失效（12306 提示「用户未登录」）：请重新登录后，回到查票页再抢一次。");
        try { await api("POST", "/api/debug", { event: "confirm_login_broken", href: location.href }); } catch (e) {}
        return;
      }
      banner(
        "这个账号的常用联系人列表是空的（#normal_passenger_id 里没有乘客）。请先在 12306「我的12306 → 常用联系人」里添加乘客，再重试。"
      );
      try { await api("POST", "/api/debug", collectConfirmDiagnostics()); } catch (e) {}
      return;
    }

    // 一个都没勾上就别提交：名对不上时提交只会换来「请选择乘客」的报错
    var ticked = passengerItems.filter(function (li) {
      var cb = q("input[type=checkbox]", li);
      return cb && cb.checked;
    }).length;
    if (action.passengers && action.passengers.length && ticked === 0) {
      banner(
        "没能在乘客列表里勾上「" + action.passengers.join("、") +
        "」——可能是名字对不上。请在页面上手动勾选后提交。"
      );
      try { await api("POST", "/api/debug", collectConfirmDiagnostics()); } catch (e) {}
      return;
    }

    // 票种必须在选席别之前定下来：学生票没有资质核验会直接出票失败（WT.9901）
    if (forceTicketType(passengerItems)) await sleep(400);

    var select = q("#seatType");
    if (select && action.seat) {
      var option = [].slice.call(select.options).filter(function (o) {
        return cleanText(o).indexOf(action.seat) >= 0;
      })[0];
      if (option) {
        select.value = option.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }

    var submit = findSubmitButton();
    if (!submit) return;

    if (!willSubmit) {
      submit.style.outline = "3px solid #2ea043";
      submit.style.outlineOffset = "2px";
      try { submit.scrollIntoView({ block: "center" }); } catch (e) {}
      return;
    }

    // auto_submit：脚本点「提交订单」，人手只负责过验证码，成功后座位锁定 30 分钟
    if (confirmLoginBroken()) {
      banner("登录已失效，无法提交。请重新登录后回到查票页重来。");
      return;
    }
    try { submit.scrollIntoView({ block: "center" }); } catch (e) {}
    await sleep(350);
    submit.click();
    var locked = await waitOrderCreated(90000);
    banner(
      locked
        ? "订单已提交成功，座位已锁定 30 分钟 —— 现在去付款即可（30 分钟内不付会自动取消）。"
        : "已点「提交订单」，但没检测到成功。请看页面上是否还有验证码或提示信息，必要时手动点一次提交。"
    );
  }

  /* ======================================================================
   * 9. 入口
   * ==================================================================== */
  // 供离线单测调用；在 Tampermonkey 里只是个挂在 window 上的对象，无副作用
  try {
    if (typeof window !== "undefined") {
      window.__T12306_TEST__ = {
        parseTable: parseTable,
        headerLabels: headerLabels,
        matchSeatName: matchSeatName,
        extractTrainNo: extractTrainNo,
        cleanText: cleanText,
        visibleText: visibleText,
        loginExpired: loginExpired,
        confirmLoginBroken: confirmLoginBroken
      };
    }
  } catch (e) {}

  var path = location.pathname;
  if (/\/otn\/confirmPassenger\//.test(path)) {
    prefillConfirmPage();
  } else if (/\/otn\/leftTicket\//.test(path)) {
    queryLoop();
  }
})();
