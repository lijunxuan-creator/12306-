import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from core.config import Config, NotifyConfig, PollConfig, ServerConfig, Task
from core.engine import Engine
from core.models import Station
from core.server import MonitorHTTPServer
from core.store import Store


def make_config(**overrides):
    params = dict(
        tasks=[
            Task(
                id="t1",
                from_station=Station("北京南", "VNP"),
                to_station=Station("上海虹桥", "AOH"),
                dates=["2030-01-01"],
                seats=["二等座"],
            )
        ],
        server=ServerConfig(),
        poll=PollConfig(interval_seconds=0, jitter_seconds=0),
        notify=NotifyConfig(console=False, desktop=False),
    )
    params.update(overrides)
    return Config(**params)


class ServerBase(unittest.TestCase):
    """只提供起服务 + 请求工具，不含用例。"""

    token = None

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.dir.name) / "m.db"))
        self.engine = Engine(make_config(), self.store)
        self.httpd = MonitorHTTPServer(("127.0.0.1", 0), self.engine, token=self.token)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.dir.cleanup()

    def get(self, path, headers=None):
        request = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def post(self, path, payload=None, headers=None):
        body = json.dumps(payload or {}).encode("utf-8")
        all_headers = {"Content-Type": "application/json"}
        all_headers.update(headers or {})
        request = urllib.request.Request(
            self.base + path, data=body, headers=all_headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


class ServerTest(ServerBase):
    def test_dashboard(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("12306", body.decode("utf-8"))

    def test_health(self):
        status, body = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_state(self):
        status, body = self.get("/api/state")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload["tasks"]), 1)
        self.assertEqual(payload["tasks"][0]["id"], "t1")
        self.assertIn("stats", payload)

    def test_tasks_endpoint(self):
        status, body = self.get("/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["tasks"][0]["route"], "北京南→上海虹桥")

    def test_plan_and_report_flow(self):
        status, plan = self.post("/api/plan/next", {"client_id": "c1"})
        self.assertEqual(status, 200)
        self.assertFalse(plan["idle"])
        plan_id = plan["plan"]["id"]

        status, report = self.post(
            "/api/plan/%s/report" % plan_id,
            {"ok": True, "rows": [{"train": "G1", "seats": {"二等座": "有"}}]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["new_alerts"]), 1)

        status, body = self.get("/api/state")
        payload = json.loads(body)
        self.assertEqual(len(payload["alerts"]), 1)
        self.assertEqual(payload["stats"]["clients_online"], 1)

    def test_report_unknown_plan(self):
        status, body = self.post("/api/plan/deadbeef/report", {"ok": True, "rows": []})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])

    def test_bad_json_body(self):
        request = urllib.request.Request(
            self.base + "/api/plan/next",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=10)
            self.fail("应当返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_unknown_api(self):
        status, _ = self.get("/api/nope")
        self.assertEqual(status, 404)

    def test_alert_ack_and_clear(self):
        _, plan = self.post("/api/plan/next", {"client_id": "c1"})
        self.post(
            "/api/plan/%s/report" % plan["plan"]["id"],
            {"ok": True, "rows": [{"train": "G1", "seats": {"二等座": "有"}}]},
        )
        _, state = self.get("/api/state")
        alert_id = json.loads(state)["alerts"][0]["id"]
        _, body = self.post("/api/alerts/%d/ack" % alert_id)
        self.assertTrue(body["ok"])
        _, body = self.post("/api/alerts/clear")
        self.assertEqual(body["removed"], 1)

    def test_sse_stream(self):
        response = urllib.request.urlopen(self.base + "/api/events", timeout=10)
        try:
            lines = []
            for _ in range(8):
                line = response.readline()
                if not line:
                    break
                lines.append(line)
                if b"event: state" in line:
                    break
            self.assertTrue(any(b"event: state" in line for line in lines), lines)
        finally:
            response.close()

    def test_static_traversal_not_served(self):
        status, _ = self.get("/static/../../README.md")
        self.assertIn(status, (400, 404))


class TokenServerTest(ServerBase):
    token = "s3cr3t"

    def test_dashboard_needs_no_token(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("12306", body.decode("utf-8"))

    def test_health_needs_no_token(self):
        status, _ = self.get("/api/health")
        self.assertEqual(status, 200)

    def test_api_requires_token(self):
        status, _ = self.get("/api/state")
        self.assertEqual(status, 401)
        status, _ = self.get("/api/state", headers={"X-Token": "s3cr3t"})
        self.assertEqual(status, 200)
        status, _ = self.get("/api/state?token=s3cr3t")
        self.assertEqual(status, 200)

    def test_post_requires_token(self):
        status, _ = self.post("/api/plan/next", {"client_id": "c1"})
        self.assertEqual(status, 401)
        status, body = self.post(
            "/api/plan/next", {"client_id": "c1"}, headers={"X-Token": "s3cr3t"}
        )
        self.assertEqual(status, 200)
        self.assertIn("plan", body)


if __name__ == "__main__":
    unittest.main()
