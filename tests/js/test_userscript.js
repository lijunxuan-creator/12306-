// 用最小 DOM 桩验证用户脚本的表格解析逻辑（parseTable / headerLabels）。
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SCRIPT = path.join(__dirname, "..", "..", "userscript", "12306-monitor.user.js");
const code = fs.readFileSync(SCRIPT, "utf8");

/* ------------------------------------------------------------ 迷你 DOM */
function El(tag, opts) {
  opts = opts || {};
  const el = {
    tagName: tag.toUpperCase(),
    id: opts.id || "",
    className: opts.cls || "",
    children: [],
    attrs: opts.attrs || {},
    style: {},
    offsetParent: opts.hidden ? null : {},
    parentNode: null,
    _text: opts.text || "",
    _clicked: false,
    appendChild(child) {
      child.parentNode = el;
      el.children.push(child);
      return child;
    },
    setAttribute(k, v) { el.attrs[k] = v; },
    getAttribute(k) { return el.attrs[k]; },
    dispatchEvent() {},
    addEventListener() {},
    click() { el._clicked = true; },
    remove() {},
    closest(tagName) {
      let node = el;
      while (node) {
        if (node.tagName === tagName.toUpperCase()) return node;
        node = node.parentNode;
      }
      return null;
    },
    querySelector(sel) { return queryAll(sel, el)[0] || null; },
    querySelectorAll(sel) { return queryAll(sel, el); }
  };
  Object.defineProperty(el, "textContent", {
    get() { return el.children.map((c) => c.textContent).join("") + el._text; },
    set(v) { el._text = String(v); el.children = []; }
  });
  return el;
}

function matchSimple(el, part) {
  const m = part.match(/^([a-zA-Z][a-zA-Z0-9]*)?((?:[.#][\w-]+)*)$/);
  if (!m) return false;
  const tag = m[1] ? m[1].toUpperCase() : null;
  const ids = [];
  const classes = [];
  (m[2] || "").replace(/([.#])([\w-]+)/g, (_, kind, value) => {
    if (kind === "#") ids.push(value); else classes.push(value);
  });
  if (tag && el.tagName !== tag) return false;
  if (ids.length && el.id !== ids[0]) return false;
  if (classes.length) {
    const own = (el.className || "").split(/\s+/);
    if (!classes.every((c) => own.indexOf(c) >= 0)) return false;
  }
  return true;
}

function matchPart(el, part) {
  const chain = part.trim().split(/\s+/).filter(Boolean);
  if (!chain.length) return false;
  if (!matchSimple(el, chain[chain.length - 1])) return false;
  let node = el.parentNode;
  for (let i = chain.length - 2; i >= 0; i--) {
    while (node && !matchSimple(node, chain[i])) node = node.parentNode;
    if (!node) return false;
    node = node.parentNode;
  }
  return true;
}

function walk(root, visit) {
  root.children.forEach((child) => {
    visit(child);
    walk(child, visit);
  });
}

function queryAll(selector, root) {
  const parts = selector.split(",").map((s) => s.trim()).filter(Boolean);
  const found = [];
  walk(root, (node) => {
    if (parts.some((part) => matchPart(node, part)) && found.indexOf(node) < 0) found.push(node);
  });
  return found;
}

function text(str) { return El("span", { text: str }); }

function td(children, attrs) {
  const cell = El("td", { attrs: attrs || {} });
  (Array.isArray(children) ? children : [children]).forEach((c) => cell.appendChild(c));
  return cell;
}

/* ------------------------------------------------------------ 造 12306 风格的 DOM */
function appendRow(tbody, row) {
  const tr = El("tr");
  const numberLink = El("a", { cls: "number" });
  numberLink.appendChild(text(row.no));
  tr.appendChild(td(numberLink));
  tr.appendChild(td(text(row.stations || "广州南肇庆东")));

  const timeCell = El("td");
  const from = El("strong");
  from.appendChild(text(row.dep));
  const to = El("strong");
  to.appendChild(text(row.arr));
  timeCell.appendChild(from);
  timeCell.appendChild(to);
  tr.appendChild(timeCell);

  tr.appendChild(td(text(row.dur || "00:40")));

  (row.seats || []).forEach((value) => {
    if (value && typeof value === "object") {
      tr.appendChild(td([El("span", { text: value.text })], { colspan: String(value.span || 1) }));
    } else {
      tr.appendChild(td(text(value)));
    }
  });

  const tail = El("td");
  if (row.bookable !== false) {
    const button = El("a", { cls: "btn72" });
    button.appendChild(text("预订"));
    tail.appendChild(button);
  }
  tr.appendChild(tail);
  tbody.appendChild(tr);
}

function headerTable(headers) {
  const table = El("table");
  const thead = El("thead");
  const headRow = El("tr");
  headers.forEach((h) => {
    const th = El("th");
    th.appendChild(text(h));
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);
  return table;
}

function dataTable(rows, withTheadHeaders) {
  const table = El("table");
  if (withTheadHeaders) {
    table.children.push(...headerTable(withTheadHeaders).children);
    table.children.forEach((c) => { c.parentNode = table; });
  }
  const tbody = El("tbody", { id: "queryLeftTable" });
  table.appendChild(tbody);
  rows.forEach((row) => appendRow(tbody, row));
  return { table, tbody };
}

/* ------------------------------------------------------------ 运行脚本 */
const body = El("body");
const documentStub = {
  body,
  head: El("head"),
  createElement: (tag) => El(tag),
  querySelector: (sel) => queryAll(sel, body)[0] || null,
  querySelectorAll: (sel) => queryAll(sel, body),
  contains: () => true,
  addEventListener() {},
  getElementById: (id) => queryAll("#" + id, body)[0] || null
};

const sandbox = {
  console,
  document: documentStub,
  location: { pathname: "/otn/other/page", href: "https://kyfw.12306.cn/otn/other/page" },
  sessionStorage: { _d: {}, getItem(k) { return this._d[k] || null; }, setItem(k, v) { this._d[k] = v; } },
  setTimeout,
  clearTimeout,
  setInterval,
  Date,
  JSON,
  Math,
  Promise,
  Object,
  String,
  Number,
  Array,
  Event: function (type) { this.type = type; },
  MutationObserver: function () { this.observe = () => {}; this.disconnect = () => {}; },
  GM_xmlhttpRequest: undefined,
  fetch: () => Promise.reject(new Error("offline")),
  unsafeWindow: undefined
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: "12306-monitor.user.js" });

const api = sandbox.window.__T12306_TEST__;
const checks = [];
function check(name, ok, detail) {
  checks.push([name, ok, detail]);
  console.log((ok ? "  ✓ " : "  ✗ ") + name + (ok ? "" : "  →  " + JSON.stringify(detail)));
}

if (!api) {
  console.error("脚本没有导出测试钩子");
  process.exit(1);
}

function resetBody() { body.children.length = 0; }

/* ============================================================
 * A：常规 9 席别表头（含同城车站提示行、候补、--）
 * ========================================================== */
const SIMPLE_HEADERS = ["车次", "出发站到达站", "出发时间到达时间", "历时",
  "商务座", "一等座", "二等座", "高级软卧", "软卧", "硬卧", "软座", "硬座", "无座", "备注"];

const A = dataTable([
  { no: "G1", dep: "06:30", arr: "12:00", dur: "05:30", stations: "北京南上海虹桥",
    seats: ["无", "3", "有", "--", "--", "--", "--", "--", "--"] },
  { no: "G3", dep: "07:00", arr: "12:30", dur: "05:30", stations: "北京南上海虹桥",
    seats: ["--", "--", "无", "--", "--", "--", "--", "--", "--"] },
  { no: "D22", dep: "08:00", arr: "14:10", dur: "06:10", stations: "北京南上海虹桥",
    seats: ["", "候补", "12", "", "", "", "", "", ""], bookable: false }
], SIMPLE_HEADERS);
A.tbody.appendChild(td(text("北京南、北京 同城车站，请注意核对")));
resetBody();
body.appendChild(A.table);

const planA = { date: "2026-11-10", from: { name: "北京南", code: "VNP" }, to: { name: "上海虹桥", code: "AOH" } };
const parsedA = api.parseTable(planA);
check("A: 识别 3 个车次（跳过提示行）", parsedA.rows.length === 3, parsedA.rows.map((r) => r.train));
check("A: 表头 14 列", parsedA.header.length === 14, parsedA.header);
check("A: 无错位告警", (parsedA.warnings || []).length === 0, parsedA.warnings);
const g1 = parsedA.rows.find((r) => r.train === "G1");
check("A: G1 时间", g1 && g1.dep === "06:30" && g1.arr === "12:00", g1 && [g1.dep, g1.arr]);
check("A: G1 历时", g1 && g1.duration === "05:30", g1 && g1.duration);
check("A: G1 可预订", g1 && g1.bookable === true, g1 && g1.bookable);
check("A: G1 商务座=无", g1 && g1.seats["商务座"] === "无", g1 && g1.seats);
check("A: G1 一等座=3", g1 && g1.seats["一等座"] === "3", g1 && g1.seats);
check("A: G1 二等座=有", g1 && g1.seats["二等座"] === "有", g1 && g1.seats);
check("A: G1 无座=--", g1 && g1.seats["无座"] === "--", g1 && g1.seats);
const d22 = parsedA.rows.find((r) => r.train === "D22");
check("A: D22 候补", d22 && d22.seats["一等座"] === "候补", d22 && d22.seats);
check("A: D22 不可预订", d22 && d22.bookable === false, d22 && d22.bookable);

/* ============================================================
 * B：真实 16 列表头，席位两行合并成一格（无 colspan）
 * ========================================================== */
const REAL_HEADERS = ["车次", "出发站到达站", "出发时间到达时间", "历时",
  "商务座特等座", "优选一等座", "一等座", "二等座二等包座",
  "高级软卧", "软卧动卧一等卧", "硬卧二等卧", "软座", "硬座", "无座", "其他", "备注"];

const B = dataTable([
  { no: "Z8006", dep: "20:24", arr: "22:02", dur: "01:38", stations: "广州白云肇庆",
    seats: ["--", "--", "--", "--", "1折", "6折", "有折", "--", "候补", "无", "--"] },
  { no: "D7556", dep: "21:47", arr: "22:30", dur: "00:43",
    seats: ["--", "--", "11", "有", "--", "--", "--", "--", "--", "有", "--"] },
  { no: "G6238", dep: "21:37", arr: "22:19", dur: "00:42",
    seats: ["候补", "--", "候补", "有", "--", "--", "--", "--", "--", "无", "--"] }
], REAL_HEADERS);
resetBody();
body.appendChild(B.table);

const planB = { date: "2026-11-06", from: { name: "广州" , code: "GZQ" }, to: { name: "肇庆", code: "ZVQ" } };
const parsedB = api.parseTable(planB);
check("B: 表头 16 列", parsedB.header.length === 16, parsedB.header);
check("B: 无错位告警", (parsedB.warnings || []).length === 0, parsedB.warnings);
check("B: 识别 3 个车次", parsedB.rows.length === 3, parsedB.rows.map((r) => r.train));
const z8006 = parsedB.rows.find((r) => r.train === "Z8006");
check("B: Z8006 高级软卧=1折", z8006 && z8006.seats["高级软卧"] === "1折", z8006 && z8006.seats);
check("B: Z8006 软卧=6折", z8006 && z8006.seats["软卧"] === "6折", z8006 && z8006.seats);
check("B: Z8006 不把软卧认成动卧", z8006 && z8006.seats["动卧"] === undefined, z8006 && z8006.seats);
check("B: Z8006 硬座=候补", z8006 && z8006.seats["硬座"] === "候补", z8006 && z8006.seats);
check("B: Z8006 无座=无", z8006 && z8006.seats["无座"] === "无", z8006 && z8006.seats);
const d7556 = parsedB.rows.find((r) => r.train === "D7556");
check("B: D7556 二等座=有", d7556 && d7556.seats["二等座"] === "有", d7556 && d7556.seats);
const g6238 = parsedB.rows.find((r) => r.train === "G6238");
check("B: G6238 商务座=候补", g6238 && g6238.seats["商务座"] === "候补", g6238 && g6238.seats);

check("B: 席别名匹配（软卧合并表头）", api.matchSeatName("软卧动卧一等卧") === "软卧", api.matchSeatName("软卧动卧一等卧"));
check("B: 席别名匹配（二等座合并表头）", api.matchSeatName("二等座二等包座") === "二等座", api.matchSeatName("二等座二等包座"));
check("B: 非席别表头", api.matchSeatName("车次") === null, api.matchSeatName("车次"));

/* ============================================================
 * C：colspan 合并格（16 列宽，但每行只有 15 个 td）
 * ========================================================== */
const C = dataTable([
  { no: "G6238", dep: "21:37", arr: "22:19", dur: "00:42",
    seats: [
      { text: "--", span: 2 }, "--", "有", "1折", "6折", "有折", "--", "候补", "无", "--"
    ] }
], REAL_HEADERS);
resetBody();
body.appendChild(C.table);

const parsedC = api.parseTable(planB);
check("C: colspan 行列宽与表头一致（无告警）", (parsedC.warnings || []).length === 0, parsedC.warnings);
check("C: colspan 行解析成功", parsedC.rows.length === 1, parsedC.rows.length);
const cRow = parsedC.rows[0];
check("C: 合并格两侧席别都拿到值", cRow && cRow.seats["商务座"] === "--" && cRow.seats["优选一等座"] === "--", cRow && cRow.seats);
check("C: 合并格之后的席别不串位", cRow && cRow.seats["二等座"] === "有" && cRow.seats["软卧"] === "6折", cRow && cRow.seats);
check("C: 硬座/无座正确", cRow && cRow.seats["硬座"] === "候补" && cRow.seats["无座"] === "无", cRow && cRow.seats);

/* ============================================================
 * D：表头被拆到另一张表（数据表自己没有 thead）
 *    页面里还躺着一张 16 列的无关表头，不能选中它
 * ========================================================== */
const FOREIGN_HEADERS = ["车次", "出发站到达站", "出发时间到达时间", "历时",
  "商务座特等座", "优选一等座", "一等座", "二等座二等包座",
  "高级软卧", "软卧动卧一等卧", "硬卧二等卧", "软座", "硬座", "无座", "其他", "备注"];
const HEADERS_13 = ["车次", "出发站到达站", "出发时间到达时间", "历时",
  "商务座", "一等座", "二等座", "高级软卧", "软卧", "硬卧", "硬座", "无座", "备注"];

const D = dataTable([
  { no: "D3666", dep: "17:14", arr: "17:53", dur: "00:39",
    seats: ["--", "5", "有", "--", "--", "--", "无座值", "无"] }
]);
resetBody();
body.appendChild(headerTable(FOREIGN_HEADERS)); // 干扰：16 列
body.appendChild(headerTable(HEADERS_13));      // 真正的表头：13 列
body.appendChild(D.table);

const parsedD = api.parseTable(planB);
check("D: 选中 13 列表头而不是 16 列干扰项", parsedD.header.length === 13, parsedD.header.length);
check("D: 无错位告警", (parsedD.warnings || []).length === 0, parsedD.warnings);
check("D: 解析出 D3666", parsedD.rows.length === 1 && parsedD.rows[0].train === "D3666", parsedD.rows.map((r) => r.train));
const d3666 = parsedD.rows[0];
check("D: D3666 二等座=有", d3666 && d3666.seats["二等座"] === "有", d3666 && d3666.seats);
check("D: D3666 一等座=5", d3666 && d3666.seats["一等座"] === "5", d3666 && d3666.seats);
check("D: D3666 时间", d3666 && d3666.dep === "17:14" && d3666.arr === "17:53", d3666 && [d3666.dep, d3666.arr]);

/* ============================================================
 * E：页面还是上一次查询（路线对不上）→ 必须拒绝，而不是报错票
 * ========================================================== */
const E = dataTable([
  { no: "Z8006", dep: "20:24", arr: "22:02", dur: "01:38", stations: "广州白云肇庆",
    seats: ["--", "--", "--", "有", "--", "--", "--", "有"] }
], HEADERS_13);
resetBody();
body.appendChild(headerTable(HEADERS_13));
body.appendChild(E.table);

const planE = { date: "2026-11-06", from: { name: "广州南", code: "IZQ" }, to: { name: "肇庆东", code: "FCQ" } };
const parsedE = api.parseTable(planE);
check("E: 路线不符时不产出车次", parsedE.rows.length === 0, parsedE.rows.length);
check("E: 报错说明路线不符", !!parsedE.error && parsedE.error.indexOf("肇庆东") >= 0, parsedE.error);

/* ============================================================
 * F：完全找不到表头 → 显式报错
 * ========================================================== */
const F = dataTable([
  { no: "G1", dep: "06:30", arr: "12:00", dur: "05:30",
    seats: ["无", "3", "有", "--", "--", "--", "--", "--", "--"] }
]);
resetBody();
body.appendChild(F.table);

const parsedF = api.parseTable({});
check("F: 表头缺失时报错", !!parsedF.error && parsedF.error.indexOf("表头") >= 0, parsedF.error);
check("F: 表头缺失带 warning", (parsedF.warnings || []).indexOf("header_missing") >= 0, parsedF.warnings);

/* ============================================================
 * G：登录失效检测只认「可见」的提示框（防止隐藏模板误伤）
 * ========================================================== */
resetBody();
const hiddenDialog = El("div", { cls: "layui-layer-content", hidden: true });
hiddenDialog.appendChild(text("用户未登录"));
body.appendChild(hiddenDialog);
check("G: 隐藏的「用户未登录」不算掉线", api.confirmLoginBroken() === false, api.confirmLoginBroken());

resetBody();
const visibleDialog = El("div", { cls: "layui-layer-content" });
visibleDialog.appendChild(text("用户未登录"));
body.appendChild(visibleDialog);
check("G: 可见的「用户未登录」判定为掉线", api.confirmLoginBroken() === true, api.confirmLoginBroken());

resetBody();
const expiredDialog = El("div", { cls: "layui-layer-content" });
expiredDialog.appendChild(text("登录信息已过期，请重新登录"));
body.appendChild(expiredDialog);
check("G: 查询页认得「登录已过期」", api.loginExpired() === true, api.loginExpired());
check("G: 订单页也认得这个提示", api.confirmLoginBroken() === true, api.confirmLoginBroken());

resetBody();
const otherDialog = El("div", { cls: "layui-layer-content" });
otherDialog.appendChild(text("系统繁忙，请稍后再试"));
body.appendChild(otherDialog);
check("G: 普通错误提示不误判为掉线", api.confirmLoginBroken() === false, api.confirmLoginBroken());

const failed = checks.filter((c) => !c[1]);
console.log("\n共 " + checks.length + " 项，失败 " + failed.length);
process.exit(failed.length ? 1 : 0);
