#!/usr/bin/env python3
"""12306 余票监控 —— 命令行入口。

用法::

    python run.py serve                     启动服务 + 看板
    python run.py doctor                    检查配置、车站、端口
    python run.py fetch-stations            更新车站字典
    python run.py stations --query 北京南    查车站电报码

只依赖 Python 3.9+ 标准库。
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import config as config_mod  # noqa: E402
from core.engine import Engine  # noqa: E402
from core.notify import Notifier  # noqa: E402
from core.server import MonitorHTTPServer  # noqa: E402
from core.stations import fetch_and_save, get_index  # noqa: E402
from core.store import Store  # noqa: E402

DEFAULT_CONFIG = ROOT / "config.json"


def _fix_console() -> None:
    """Windows 控制台编码兜底，避免个别字符把程序打崩。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def setup_logging(verbose: bool, log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    console.setLevel(level)
    root.addHandler(console)

    file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s")
    )
    file_handler.setLevel(level)
    root.addHandler(file_handler)
    return logging.getLogger("main")


def load_config(path: Path):
    return config_mod.load(str(path))


def ensure_config(path: Path) -> Path:
    """首次运行时从示例配置生成 config.json，避免直接报「找不到配置」。"""
    if path.exists():
        return path
    example = ROOT / "config.example.json"
    if example.exists():
        import shutil

        shutil.copyfile(str(example), str(path))
        print("首次运行：已根据 config.example.json 生成 %s" % path)
        print("请修改其中的车站、日期、席别，然后再启动。\n")
    return path


# --------------------------------------------------------------------- serve
def cmd_serve(args: argparse.Namespace) -> int:
    _fix_console()
    config_path = Path(args.config).resolve()
    ensure_config(config_path)
    try:
        cfg = load_config(config_path)
    except config_mod.ConfigError as exc:
        print("配置有误：%s" % exc, file=sys.stderr)
        return 2

    log = setup_logging(args.verbose, ROOT / "logs" / "monitor.log")
    for warning in cfg.warnings:
        log.warning("配置提醒：%s", warning)

    db_path = Path(args.db).resolve() if args.db else config_path.parent / "data" / "monitor.db"
    store = Store(str(db_path))
    store.prune(days=7)

    notifier = Notifier(cfg.notify, logger=logging.getLogger("notify"))
    notifier.start()

    engine = Engine(cfg, store, notifier=notifier, logger=logging.getLogger("engine"))
    engine.start()

    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    try:
        httpd = MonitorHTTPServer((host, port), engine, token=cfg.server.token)
    except OSError as exc:
        print("端口 %s:%d 无法监听（%s）。换端口：python run.py serve --port 8799" % (host, port, exc),
              file=sys.stderr)
        return 3

    url = httpd.url + ("?token=" + cfg.server.token if cfg.server.token else "")
    print()
    print("=" * 66)
    print("  12306 余票监控已启动")
    print("  看板      %s" % url)
    print("  任务      %d 个（启用 %d）" % (len(cfg.tasks), len(cfg.enabled_tasks())))
    print("  通知渠道  %s" % "、".join(notifier.describe() or ["仅控制台"]))
    print("  查询间隔  %.1fs + 抖动 %.1fs" % (cfg.poll.interval_seconds, cfg.poll.jitter_seconds))
    print("  数据文件  %s" % db_path)
    print("-" * 66)
    print("  下一步：浏览器打开 https://kyfw.12306.cn/otn/leftTicket/init 并登录，")
    print("         在 Tampermonkey 中启用 userscript/12306-monitor.user.js")
    print("  停止：Ctrl+C")
    print("=" * 66)
    print()

    if args.open and not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        engine.stop()
        notifier.stop()
        httpd.shutdown()
        httpd.server_close()
        store.close()
    return 0


# -------------------------------------------------------------------- doctor
def cmd_doctor(args: argparse.Namespace) -> int:
    _fix_console()
    config_path = Path(args.config).resolve()
    ensure_config(config_path)
    ok = True
    print("配置文件：%s" % config_path)

    try:
        index = get_index()
        print("车站字典：%s（%d 个车站，生成于 %s）" % (index.path.name, index.count, index.generated_at))
    except FileNotFoundError as exc:
        print("✗ %s" % exc)
        return 2

    try:
        cfg = load_config(config_path)
    except config_mod.ConfigError as exc:
        print("✗ 配置有误：%s" % exc)
        return 2

    print("\n任务：")
    for task in cfg.tasks:
        flag = "启用" if task.enabled else "停用"
        print(
            "  ▸ %-10s %-14s %s  日期 %s  席别 %s  车次 %s%s"
            % (
                task.id,
                flag,
                task.route,
                ",".join(task.dates),
                "/".join(task.seats),
                "/".join(task.trains) or "全部",
                "  [命中自动跳预订]" if task.auto_jump else "",
            )
        )

    print("\n预约检查：")
    print("  查询间隔 %.1fs（抖动 %.1fs）" % (cfg.poll.interval_seconds, cfg.poll.jitter_seconds))
    if cfg.poll.burst:
        print(
            "  开售加速 %s 起 %.0f 秒内，每 %.1fs 查一次"
            % (
                cfg.poll.burst.get("at"),
                float(cfg.poll.burst.get("window_seconds") or 0),
                float(cfg.poll.burst.get("interval_seconds") or 0),
            )
        )
    else:
        print("  开售加速 未开启（python run.py burst --at 2026-11-05T12:59:50）")
    print("  通知渠道 %s" % "、".join(
        [name for name, on in (("console", cfg.notify.console), ("desktop", cfg.notify.desktop),
                               ("webhook", bool(cfg.notify.webhook.get("enabled"))),
                               ("email", bool(cfg.notify.email.get("enabled")))) if on]
    ))
    print("  去重窗口 %.0fs" % cfg.notify.dedupe_seconds)

    if cfg.warnings:
        print("\n提醒：")
        for warning in cfg.warnings:
            print("  ⚠ %s" % warning)

    host, port = cfg.server.host, cfg.server.port
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
        print("\n端口检查：%s:%d 可用" % (host, port))
    except OSError as exc:
        ok = False
        print("\n端口检查：✗ %s:%d 被占用（%s）" % (host, port, exc))
    finally:
        probe.close()

    print("\n结论：%s" % ("配置可用，可以 python run.py serve" if ok else "存在问题，请按上面提示修改"))
    return 0 if ok else 1


# ----------------------------------------------------------- fetch-stations
def cmd_fetch_stations(args: argparse.Namespace) -> int:
    _fix_console()
    from core.stations import DEFAULT_DATA

    target = Path(args.output).resolve() if args.output else DEFAULT_DATA
    print("正在从 12306 拉取车站字典…")
    try:
        path = fetch_and_save(target)
    except Exception as exc:
        print("✗ 拉取失败：%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    size = os.path.getsize(path) / 1024
    print("✓ 已写入 %s（%.0f KB）" % (path, size))
    return 0


# ------------------------------------------------------------------- burst
def cmd_burst(args: argparse.Namespace) -> int:
    """写/清 config.json 里的 poll.burst（开售加速窗口）。"""
    _fix_console()
    path = Path(args.config).resolve()
    ensure_config(path)
    with open(path, "r", encoding="utf-8-sig") as fp:
        data = json.load(fp)
    poll = data.setdefault("poll", {})

    if args.off:
        poll["burst"] = None
        print("已关闭开售加速窗口（poll.burst = null）")
    else:
        if not args.at:
            print("需要 --at，例如：--at 2026-11-05T12:59:50", file=sys.stderr)
            return 2
        try:
            datetime.datetime.fromisoformat(args.at)
        except ValueError:
            print("--at 需要形如 2026-11-05T12:59:50", file=sys.stderr)
            return 2
        poll["burst"] = {
            "at": args.at,
            "window_seconds": args.window,
            "interval_seconds": args.interval,
            "jitter_seconds": args.jitter,
        }
        print("已开启开售加速：%s 起 %g 秒内，每 %.1f 秒查一次"
              % (args.at, args.window, args.interval))

    with open(path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    print("已写入 %s，重启 serve 生效" % path)
    return 0


# ---------------------------------------------------------------- stations
def cmd_stations(args: argparse.Namespace) -> int:
    _fix_console()
    index = get_index()
    if args.query:
        station = index.resolve(args.query)
        if station:
            print("%s  →  电报码 %s" % (station.name, station.code))
        else:
            print("没找到「%s」。相近：%s" % (args.query, "、".join(index.suggestions(args.query)) or "无"))
            return 1
        return 0
    for station in index.search(args.search or "", limit=args.limit):
        print("%-10s %s" % (station.name, station.code))
    return 0


# -------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py", description="12306 余票监控（查询在你的浏览器里完成）"
    )
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动监控服务与看板")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--db", default=None, help="SQLite 文件路径")
    serve.add_argument("--open", action="store_true", help="启动后自动打开看板")
    serve.add_argument("--no-open", action="store_true", help=argparse.SUPPRESS)
    serve.set_defaults(func=cmd_serve)

    doctor = sub.add_parser("doctor", help="检查配置是否可用")
    doctor.set_defaults(func=cmd_doctor)

    fetch = sub.add_parser("fetch-stations", help="从 12306 更新车站字典")
    fetch.add_argument("--output", default=None)
    fetch.set_defaults(func=cmd_fetch_stations)

    stations = sub.add_parser("stations", help="查询车站电报码")
    stations.add_argument("--query", default=None, help="站名/拼音/电报码")
    stations.add_argument("--search", default=None, help="模糊搜索")
    stations.add_argument("--limit", type=int, default=20)
    stations.set_defaults(func=cmd_stations)

    burst = sub.add_parser("burst", help="设置开售加速窗口（--off 关闭）")
    burst.add_argument("--at", default=None, help="开售时刻，如 2026-11-05T12:59:50")
    burst.add_argument("--window", type=float, default=120, help="窗口秒数，默认 120")
    burst.add_argument("--interval", type=float, default=0.5, help="窗口内查询间隔，默认 0.5")
    burst.add_argument("--jitter", type=float, default=0.2, help="窗口内抖动，默认 0.2")
    burst.add_argument("--off", action="store_true", help="关闭加速窗口")
    burst.set_defaults(func=cmd_burst)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
