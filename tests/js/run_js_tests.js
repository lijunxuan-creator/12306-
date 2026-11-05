#!/usr/bin/env node
/**
 * 跑前端两部分的离线测试：
 *   1. 语法检查（看板内联脚本 + 用户脚本）
 *   2. 看板渲染（最小 DOM 桩）
 *   3. 用户脚本的表格解析（最小 DOM 桩）
 *
 * 用法：node tests/js/run_js_tests.js
 * 需要 node（仅测试用，运行本项目只需要 Python）。
 */

const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..", "..");
const results = [];

function run(name, args) {
  try {
    const output = execFileSync(process.execPath, args, { encoding: "utf8" });
    results.push([name, true]);
    process.stdout.write(output);
  } catch (err) {
    results.push([name, false]);
    process.stdout.write((err.stdout || "") + (err.stderr || ""));
  }
}

// 1. 语法检查
const html = fs.readFileSync(path.join(ROOT, "static", "index.html"), "utf8");
const inline = html.match(/<script>([\s\S]*?)<\/script>/);
if (inline) {
  const tmp = path.join(__dirname, "_inline_dashboard.js");
  fs.writeFileSync(tmp, inline[1], "utf8");
  run("看板语法检查", ["--check", tmp]);
  fs.unlinkSync(tmp);
} else {
  results.push(["看板语法检查", false]);
  console.error("index.html 里找不到内联脚本");
}
run("用户脚本语法检查", ["--check", path.join(ROOT, "userscript", "12306-monitor.user.js")]);

// 2. 行为测试
run("看板渲染", [path.join(__dirname, "test_dashboard.js")]);
run("用户脚本解析", [path.join(__dirname, "test_userscript.js")]);

const failed = results.filter((r) => !r[1]);
console.log("\n========== 前端测试汇总 ==========");
results.forEach(([name, ok]) => console.log((ok ? "  ✓ " : "  ✗ ") + name));
console.log("共 " + results.length + " 项，失败 " + failed.length);
process.exit(failed.length ? 1 : 0);
