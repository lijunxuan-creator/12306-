"""本地 HTTP 服务：REST + SSE + 看板静态页。

只监听 127.0.0.1（可在配置里改），可选 X-Token 保护。
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import queue
import re
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

from .engine import Engine

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SCRIPT_DIR = Path(__file__).resolve().parent.parent / "userscript"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

_PLAN_REPORT = re.compile(r"^/api/plan/([0-9a-zA-Z_-]+)/report$")
_ALERT_ACK = re.compile(r"^/api/alerts/(\d+)/ack$")


class MonitorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, engine: Engine, token: Optional[str] = None):
        self.engine = engine
        self.token = token
        self.log = logging.getLogger("server")
        super().__init__(address, MonitorHandler)

    @property
    def url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        display = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        return "http://%s:%d/" % (display, port)


class MonitorHandler(BaseHTTPRequestHandler):
    server_version = "T12306Monitor/1.0"
    sys_version = ""

    # ------------------------------------------------------------ 基础设施
    def log_message(self, fmt: str, *args) -> None:  # 静默默认访问日志
        logging.getLogger("http").debug("%s - %s", self.address_string(), fmt % args)

    @property
    def engine(self) -> Engine:
        return self.server.engine  # type: ignore[attr-defined]

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self._send_json({"ok": False, "error": "文件不存在"}, 404)
            return
        content_type = _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = getattr(self.server, "token", None)
        if not token:
            return True
        provided = self.headers.get("X-Token")
        if not provided:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            provided = (query.get("token") or [""])[0]
        return provided == token

    def _reject(self) -> None:
        self._send_json({"ok": False, "error": "token 无效"}, 401)

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > 4 * 1024 * 1024:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    # ------------------------------------------------------------ 路由
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                self._send_file(STATIC_DIR / "index.html")
                return
            if path.startswith("/static/"):
                rel = posixpath.normpath(path[len("/static/"):]).lstrip("/")
                target = (STATIC_DIR / rel).resolve()
                if not str(target).startswith(str(STATIC_DIR.resolve())):
                    self._send_json({"ok": False, "error": "非法路径"}, 400)
                    return
                self._send_file(target)
                return
            if path == "/api/health":
                self._send_json({"ok": True, "time": time.time(), "version": "1.1.0"})
                return
            if path == "/userscript/12306-monitor.user.js":
                # 让 Tampermonkey 的 @updateURL 能从这里拉新版本，改脚本后不必重新粘贴
                self._send_file(SCRIPT_DIR / "12306-monitor.user.js")
                return
            if path == "/api/events":
                if not self._authorized():
                    self._reject()
                    return
                self._sse_stream()
                return
            if path.startswith("/api/"):
                if not self._authorized():
                    self._reject()
                    return
                if path == "/api/state":
                    self._send_json(self.engine.state())
                    return
                if path == "/api/tasks":
                    self._send_json({"ok": True, "tasks": [t.to_dict() for t in self.engine.config.enabled_tasks()]})
                    return
                self._send_json({"ok": False, "error": "未知接口 %s" % path}, 404)
                return
            self._send_json({"ok": False, "error": "未找到 %s" % path}, 404)
        except Exception as exc:  # pragma: no cover - 兜底
            logging.getLogger("server").exception("GET %s 失败", path)
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if not path.startswith("/api/"):
                self._send_json({"ok": False, "error": "未找到 %s" % path}, 404)
                return
            if not self._authorized():
                self._reject()
                return

            payload = self._read_json()

            if path == "/api/plan/next":
                client_id = str(payload.get("client_id") or "anonymous")
                info = {
                    "page": payload.get("page"),
                    "ua": str(self.headers.get("User-Agent") or "")[:120],
                }
                result = self.engine.next_plan(
                    client_id, force=bool(payload.get("force")), info=info
                )
                self._send_json(result)
                return

            if path == "/api/alerts/clear":
                removed = self.engine.store.clear_alerts()
                self._send_json({"ok": True, "removed": removed})
                return

            if path == "/api/debug":
                payload["ts"] = time.time()
                self.engine.record_debug(payload)
                self._send_json({"ok": True})
                return

            match = _PLAN_REPORT.match(path)
            if match:
                self._send_json(self.engine.submit_report(match.group(1), payload))
                return

            match = _ALERT_ACK.match(path)
            if match:
                ok = self.engine.store.ack_alert(int(match.group(1)))
                self._send_json({"ok": ok})
                return

            self._send_json({"ok": False, "error": "未知接口 %s" % path}, 404)
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
        except Exception as exc:  # pragma: no cover - 兜底
            logging.getLogger("server").exception("POST %s 失败", path)
            self._send_json({"ok": False, "error": str(exc)}, 500)

    # ------------------------------------------------------------ SSE
    def _sse_stream(self) -> None:
        self.close_connection = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()

        channel = self.engine.subscribe()
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            initial = json.dumps(self.engine.state(alert_limit=20), ensure_ascii=False)
            self.wfile.write(("event: state\ndata: %s\n\n" % initial).encode("utf-8"))
            self.wfile.flush()

            last_ping = time.time()
            while True:
                try:
                    event = channel.get(timeout=5.0)
                except queue.Empty:
                    if time.time() - last_ping >= 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_ping = time.time()
                    continue
                chunk = "event: %s\ndata: %s\n\n" % (
                    event["event"],
                    json.dumps(event["data"], ensure_ascii=False),
                )
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
                last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.engine.unsubscribe(channel)


def serve(engine: Engine, host: str, port: int, token: Optional[str] = None) -> MonitorHTTPServer:
    httpd = MonitorHTTPServer((host, port), engine, token=token)
    logging.getLogger("server").info("看板地址 %s", httpd.url)
    return httpd
