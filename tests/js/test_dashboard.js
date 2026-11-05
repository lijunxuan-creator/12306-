// 用最小 DOM 桩在 node 里跑一遍看板脚本，确认渲染不抛异常。
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HTML = path.join(__dirname, "..", "..", "static", "index.html");
const html = fs.readFileSync(HTML, "utf8");
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) {
  console.error("找不到内联脚本");
  process.exit(1);
}

function makeEl(id) {
  const node = {
    id,
    _text: "",
    _html: "",
    className: "",
    style: {},
    classList: {
      add(c) { node.className = (node.className + " " + c).trim(); },
      remove(c) {
        node.className = node.className.split(/\s+/).filter((x) => x && x !== c).join(" ");
      },
      contains(c) { return node.className.split(/\s+/).indexOf(c) >= 0; }
    },
    addEventListener() {},
    removeEventListener() {},
    appendChild() {},
    scrollIntoView() {}
  };
  Object.defineProperty(node, "textContent", {
    get() { return node._text; },
    set(v) { node._text = String(v); }
  });
  Object.defineProperty(node, "innerHTML", {
    get() { return node._html; },
    set(v) { node._html = String(v); }
  });
  return node;
}

const elements = {};
["dot", "stat-tasks", "stat-clients", "stat-alerts", "stat-cycles", "stat-uptime",
 "banner", "tasks", "detail", "alerts", "foot-meta", "btn-clear"].forEach((id) => {
  elements[id] = makeEl(id);
});

const listeners = {};
const document = {
  getElementById: (id) => elements[id] || null,
  body: {
    addEventListener(name, fn) { listeners[name] = fn; },
    removeEventListener() {}
  }
};

const STATE = {
  server_time: 1793775639.1,
  started_at: 1793775626.8,
  uptime_seconds: 12,
  stopped: false,
  stats: { plans_issued: 3, reports: 3, failed_reports: 1, alerts: 2, cycles: 3, clients_online: 1, inflight: 0 },
  clients: [{ client_id: "tab-x", online: true, age: 0.2, info: { page: "leftTicket" } }],
  config: { tasks: 2, enabled_tasks: 1, channels: ["console", "desktop"], warnings: [], dates: ["2026-11-10"] },
  tasks: [
    {
      id: "beijing-shanghai",
      enabled: true,
      route: "北京南→上海虹桥",
      note: "示例",
      dates: ["2026-11-10", "2026-11-11"],
      seats: ["二等座", "一等座"],
      trains: ["G1", "G3"],
      auto_jump: true,
      dates_state: [],
      last: {
        ts: 1793775639.04, route: "北京南→上海虹桥", date: "2026-11-10", cycle: 2, ok: true,
        error: "", row_count: 12, match_count: 1, elapsed: 4.2, warnings: ["header_missing"],
        rows: [{ train: "G1", dep: "06:30", arr: "12:00", hits: { 二等座: "有" } },
               { train: "G3", dep: "07:00", arr: "12:30", hits: {} }],
        new_alerts: [{ train: "G1" }]
      }
    },
    { id: "shenzhen-gz", enabled: false, route: "深圳北→广州南", dates: ["2026-11-10"], seats: ["二等座"], trains: [], last: null, dates_state: [] }
  ],
  alerts: [
    { id: 2, ts: 1793775639.04, date: "2026-11-10", train: "G1", seat: "二等座", level: "available",
      message: "2026-11-10 G1 二等座：有票", route: "北京南→上海虹桥" },
    { id: 1, ts: 1793775600.0, date: "2026-11-10", train: "G9", seat: "一等座", level: "waitlist",
      message: "2026-11-10 G9 一等座：可候补", route: "北京南→上海虹桥" }
  ],
  running: true,
  notify_channels: ["console", "desktop"]
};

class FakeEventSource {
  constructor(url) { this.url = url; }
  addEventListener() {}
  close() {}
}

const sandbox = {
  console,
  document,
  window: { Notification: undefined },
  location: { search: "?token=abc" },
  sessionStorage: { _d: {}, getItem(k) { return this._d[k] || null; }, setItem(k, v) { this._d[k] = String(v); } },
  URLSearchParams,
  fetch: () => Promise.resolve({ json: () => Promise.resolve(STATE) }),
  EventSource: FakeEventSource,
  setInterval: () => 0,
  setTimeout,
  clearTimeout,
  Date,
  JSON,
  Math,
  Object,
  Promise,
  Number,
  String,
  Array,
  Notification: { permission: "granted", requestPermission() {} }
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const errors = [];
try {
  vm.runInContext(m[1], sandbox, { filename: "dashboard.js" });
} catch (err) {
  errors.push("同步执行异常: " + err.stack);
}

setTimeout(() => {
  if (errors.length) {
    console.error(errors.join("\n"));
    process.exit(1);
  }
  const tasksHtml = elements["tasks"].innerHTML;
  const alertsHtml = elements["alerts"].innerHTML;
  const checks = [
    ["任务路由渲染", tasksHtml.indexOf("北京南→上海虹桥") >= 0],
    ["命中数量渲染", tasksHtml.indexOf("1 处命中") >= 0],
    ["停用标签渲染", tasksHtml.indexOf("已停用") >= 0],
    ["告警有票渲染", alertsHtml.indexOf("有票") >= 0],
    ["告警候补样式", alertsHtml.indexOf("alert waitlist") >= 0],
    ["明细表格渲染", elements["detail"].innerHTML.indexOf("二等座：有") >= 0],
    ["统计数字", elements["stat-clients"].textContent === "1"],
    ["在线横幅隐藏", elements["banner"].className.indexOf("hide") >= 0]
  ];
  let bad = 0;
  checks.forEach(([name, ok]) => {
    console.log((ok ? "  ✓ " : "  ✗ ") + name);
    if (!ok) bad++;
  });
  // token 写进 sessionStorage
  console.log((sandbox.sessionStorage.getItem("t12306_token") === "abc" ? "  ✓ " : "  ✗ ") + "token 记忆");
  if (sandbox.sessionStorage.getItem("t12306_token") !== "abc") bad++;
  process.exit(bad ? 1 : 0);
}, 120);
