import json
import threading
import time
import unittest
import urllib.parse

from core.config import NotifyConfig
from core.notify import Notifier, build_webhook_body, render


ALERT = {
    "ts": 1_700_000_000,
    "date": "2030-01-01",
    "train": "G1",
    "seat": "二等座",
    "level": "available",
    "message": "2030-01-01 G1 二等座：有票",
    "task_note": "回家",
    "extra": ["2030-01-01 G3 二等座：5 张"],
}


class RenderTest(unittest.TestCase):
    def test_render(self):
        title, message = render(ALERT)
        self.assertIn("G1", title)
        self.assertIn("2030-01-01", title)
        self.assertIn("有票", message)
        self.assertIn("回家", message)
        self.assertIn("G3", message)
        self.assertIn("登录", message)


class WebhookBodyTest(unittest.TestCase):
    def test_generic(self):
        body, ctype = build_webhook_body("generic", "标题", "正文", ALERT)
        self.assertIn("application/json", ctype)
        payload = json.loads(body)
        self.assertEqual(payload["title"], "标题")
        self.assertEqual(payload["alert"]["train"], "G1")

    def test_bark(self):
        body, _ = build_webhook_body("bark", "标题", "正文", ALERT)
        payload = json.loads(body)
        self.assertEqual(payload["title"], "标题")
        self.assertEqual(payload["group"], "12306")

    def test_serverchan(self):
        body, ctype = build_webhook_body("serverchan", "标题", "正文", ALERT)
        self.assertIn("urlencoded", ctype)
        query = urllib.parse.parse_qs(body.decode("utf-8"))
        self.assertEqual(query["title"], ["标题"])

    def test_dingtalk(self):
        body, _ = build_webhook_body("dingtalk", "标题", "正文", ALERT)
        payload = json.loads(body)
        self.assertEqual(payload["msgtype"], "text")
        self.assertIn("标题", payload["text"]["content"])

    def test_wecom(self):
        body, _ = build_webhook_body("wecom", "标题", "正文", ALERT)
        payload = json.loads(body)
        self.assertIn("正文", payload["text"]["content"])

    def test_feishu(self):
        body, _ = build_webhook_body("feishu", "标题", "正文", ALERT)
        payload = json.loads(body)
        self.assertEqual(payload["msg_type"], "text")

    def test_unknown_kind_falls_back(self):
        body, _ = build_webhook_body("whatever", "标题", "正文", ALERT)
        self.assertIn("title", json.loads(body))


class NotifierTest(unittest.TestCase):
    def make(self, **overrides):
        params = dict(console=False, desktop=False, dedupe_seconds=10)
        params.update(overrides)
        return NotifyConfig(**params)

    def test_dispatch_webhook(self):
        calls = []

        def poster(url, body, content_type, headers):
            calls.append((url, body, content_type, headers))
            return True, "HTTP 200"

        cfg = self.make(webhook={"enabled": True, "kind": "dingtalk", "url": "http://example/x", "headers": {"K": "v"}})
        notifier = Notifier(cfg, poster=poster)
        results = notifier.dispatch(ALERT)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "http://example/x")
        self.assertEqual(calls[0][3], {"K": "v"})
        self.assertTrue(any(r.channel.startswith("webhook") and r.ok for r in results))

    def test_dispatch_reports_failure(self):
        def poster(*args, **kwargs):
            return False, "HTTP 500"

        cfg = self.make(webhook={"enabled": True, "kind": "generic", "url": "http://example/x", "headers": {}})
        results = Notifier(cfg, poster=poster).dispatch(ALERT)
        self.assertTrue(any(r.channel.startswith("webhook") and not r.ok for r in results))

    def test_email_without_recipients(self):
        cfg = self.make(email={"enabled": True, "to": []})
        results = Notifier(cfg).dispatch(ALERT)
        email = [r for r in results if r.channel == "email"][0]
        self.assertFalse(email.ok)
        self.assertIn("收件人", email.detail)

    def test_describe(self):
        cfg = self.make(console=True, desktop=True)
        self.assertEqual(Notifier(cfg).describe(), ["console", "desktop"])

    def test_worker_thread_dispatches(self):
        seen = []

        def poster(url, body, content_type, headers):
            seen.append(url)
            return True, "ok"

        cfg = self.make(webhook={"enabled": True, "kind": "generic", "url": "http://example/x", "headers": {}})
        notifier = Notifier(cfg, poster=poster)
        notifier.start()
        try:
            notifier.enqueue(ALERT)
            deadline = time.time() + 3
            while time.time() < deadline and not seen:
                time.sleep(0.05)
        finally:
            notifier.stop()
        self.assertEqual(seen, ["http://example/x"])


if __name__ == "__main__":
    unittest.main()
