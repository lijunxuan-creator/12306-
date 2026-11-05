"""告警通知：控制台 / Windows 桌面 / Webhook / 邮件。

- `dispatch()` 是同步的，便于测试；
- `enqueue()` + 后台线程用于运行时，避免慢网络拖住查询循环。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import smtplib
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.header import Header
from email.mime.text import MIMEText
from typing import Any, Callable, Dict, List, Optional

from .config import NotifyConfig


@dataclass
class NotifyResult:
    channel: str
    ok: bool
    detail: str = ""


# ------------------------------------------------------------------ 文本渲染
def render(alert: Dict[str, Any]) -> tuple:
    """把告警渲染成 (标题, 正文)。"""
    title = "12306 有票：%s %s" % (alert.get("date", ""), alert.get("train", ""))
    lines = [alert.get("message", "")]
    for extra in alert.get("extra") or []:
        lines.append("· %s" % extra)
    if alert.get("task_note"):
        lines.append("备注：%s" % alert["task_note"])
    lines.append("时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(alert.get("ts") or time.time())))
    lines.append("请尽快在浏览器中完成下单（需本人登录、验证码、支付）。")
    return title, "\n".join(str(line) for line in lines if line)


# ------------------------------------------------------------------ Webhook
def build_webhook_body(kind: str, title: str, message: str, alert: Dict[str, Any]):
    """返回 (body_bytes, content_type)。各家机器人 payload 形状不同，单独适配。"""
    kind = (kind or "generic").lower()
    if kind == "bark":
        payload = {
            "title": title,
            "body": message,
            "group": "12306",
            "level": "timeSensitive",
            "isArchive": 1,
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json"
    if kind == "serverchan":
        payload = {"title": title, "desp": message.replace("\n", "\n\n")}
        return urllib.parse.urlencode(payload).encode("utf-8"), (
            "application/x-www-form-urlencoded"
        )
    if kind == "dingtalk":
        payload = {"msgtype": "text", "text": {"content": "%s\n%s" % (title, message)}}
        return json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json"
    if kind == "wecom":
        payload = {"msgtype": "text", "text": {"content": "%s\n%s" % (title, message)}}
        return json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json"
    if kind == "feishu":
        payload = {"msg_type": "text", "content": {"text": "%s\n%s" % (title, message)}}
        return json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json"
    payload = {"title": title, "message": message, "alert": alert}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json"


def http_post(
    url: str,
    body: bytes,
    content_type: str = "application/json",
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 10.0,
) -> tuple:
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", content_type)
    request.add_header("User-Agent", "12306-monitor/1.0")
    for key, value in (headers or {}).items():
        request.add_header(str(key), str(value))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, "HTTP %d" % response.status
    except urllib.error.HTTPError as exc:
        return False, "HTTP %d" % exc.code
    except Exception as exc:  # 网络类错误统一降级
        return False, "%s: %s" % (type(exc).__name__, exc)


# ------------------------------------------------------------------ 桌面
_TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $template.GetElementsByTagName('text')
$texts.Item(0).AppendChild($template.CreateTextNode($env:T12306_TITLE)) | Out-Null
$texts.Item(1).AppendChild($template.CreateTextNode($env:T12306_BODY)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('12306 monitor').Show($toast)
"""


def desktop_notify(title: str, message: str, sound: bool = True) -> tuple:
    """Windows 弹 Toast；其它平台尝试 notify-send / osascript；失败退回响铃。"""
    if os.name == "nt":
        env = dict(os.environ)
        env["T12306_TITLE"] = title
        env["T12306_BODY"] = message[:200]
        for exe in ("powershell.exe", "pwsh.exe"):
            try:
                proc = subprocess.run(
                    [exe, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _TOAST_PS],
                    capture_output=True,
                    timeout=15,
                    env=env,
                )
                if proc.returncode == 0:
                    if sound:
                        beep()
                    return True, "toast"
            except (FileNotFoundError, subprocess.SubprocessError, OSError):
                continue
        if sound:
            beep()
        return False, "toast 失败，已改为响铃"
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["osascript", "-e", 'display notification %s with title %s'
                 % (json.dumps(message), json.dumps(title))],
                capture_output=True, timeout=10,
            )
            return True, "osascript"
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            pass
    try:
        subprocess.run(["notify-send", title, message], capture_output=True, timeout=10)
        return True, "notify-send"
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return False, "当前平台没有可用的桌面通知"


def beep() -> None:
    try:
        if os.name == "nt":
            import winsound

            for freq in (1200, 1600, 2000):
                winsound.Beep(freq, 180)
        else:  # pragma: no cover - 非 Windows
            sys.stdout.write("\a")
            sys.stdout.flush()
    except Exception:
        pass


# ------------------------------------------------------------------ 通知器
class Notifier:
    def __init__(
        self,
        cfg: NotifyConfig,
        logger: Optional[logging.Logger] = None,
        poster: Callable[..., tuple] = http_post,
    ) -> None:
        self.cfg = cfg
        self.log = logger or logging.getLogger("notify")
        self._post = poster
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=500)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_results: List[NotifyResult] = []

    # -------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="notifier", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                alert = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.dispatch(alert)
            except Exception:  # 通知失败不能影响监控
                self.log.exception("通知分发失败")

    def enqueue(self, alert: Dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(alert)
        except queue.Full:
            self.log.warning("通知队列已满，丢弃一条告警")

    # -------------------------------------------------------------- 分发
    def dispatch(self, alert: Dict[str, Any]) -> List[NotifyResult]:
        title, message = render(alert)
        results: List[NotifyResult] = []

        if self.cfg.console:
            try:
                print("\n" + "=" * 68, flush=True)
                print("【%s】" % title, flush=True)
                print(message, flush=True)
                print("=" * 68, flush=True)
                results.append(NotifyResult("console", True))
            except Exception as exc:  # pragma: no cover
                results.append(NotifyResult("console", False, str(exc)))

        if self.cfg.desktop:
            ok, detail = desktop_notify(title, message, self.cfg.sound)
            results.append(NotifyResult("desktop", ok, detail))

        if self.cfg.webhook.get("enabled"):
            kind = str(self.cfg.webhook.get("kind") or "generic")
            body, content_type = build_webhook_body(kind, title, message, alert)
            ok, detail = self._post(
                str(self.cfg.webhook.get("url")),
                body,
                content_type,
                self.cfg.webhook.get("headers") or {},
            )
            results.append(NotifyResult("webhook:%s" % kind, ok, detail))
            if not ok:
                self.log.warning("Webhook 推送失败：%s", detail)

        if self.cfg.email.get("enabled"):
            ok, detail = self._send_email(title, message)
            results.append(NotifyResult("email", ok, detail))
            if not ok:
                self.log.warning("邮件发送失败：%s", detail)

        self.last_results = results
        return results

    def _send_email(self, title: str, message: str) -> tuple:
        cfg = self.cfg.email
        recipients = cfg.get("to") or []
        if isinstance(recipients, str):
            recipients = [recipients]
        if not recipients:
            return False, "没有收件人"
        msg = MIMEText(message, "plain", "utf-8")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = cfg.get("user") or ""
        msg["To"] = ", ".join(recipients)
        host = cfg.get("smtp_host")
        port = int(cfg.get("smtp_port") or 465)
        context = ssl.create_default_context()
        try:
            if cfg.get("ssl", True):
                server = smtplib.SMTP_SSL(host, port, timeout=15, context=context)
            else:
                server = smtplib.SMTP(host, port, timeout=15)
                server.starttls(context=context)
            with server:
                if cfg.get("user"):
                    server.login(cfg["user"], cfg.get("password") or "")
                server.sendmail(cfg.get("user") or "", recipients, msg.as_string())
            return True, "ok"
        except Exception as exc:
            return False, "%s: %s" % (type(exc).__name__, exc)

    def describe(self) -> List[str]:
        channels = []
        if self.cfg.console:
            channels.append("console")
        if self.cfg.desktop:
            channels.append("desktop")
        if self.cfg.webhook.get("enabled"):
            channels.append("webhook")
        if self.cfg.email.get("enabled"):
            channels.append("email")
        return channels
