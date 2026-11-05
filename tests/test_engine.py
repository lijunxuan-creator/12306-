import datetime
import tempfile
import unittest
from pathlib import Path

from core.config import Config, NotifyConfig, PollConfig, ServerConfig, Task
from core.engine import Engine
from core.models import Station
from core.store import Store

DATE_A = "2030-01-01"
DATE_B = "2030-01-02"


class Clock:
    def __init__(self, value=1_000_000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeNotifier:
    def __init__(self):
        self.alerts = []

    def enqueue(self, alert):
        self.alerts.append(alert)

    def describe(self):
        return ["fake"]


def make_task(**overrides):
    params = dict(
        id="t1",
        from_station=Station("北京南", "VNP"),
        to_station=Station("上海虹桥", "AOH"),
        dates=[DATE_A, DATE_B],
        seats=["二等座"],
    )
    params.update(overrides)
    return Task(**params)


def make_config(tasks=None, interval=8.0, jitter=0.0, dedupe=300.0, stop_at=None, ttl=90.0):
    return Config(
        tasks=tasks or [make_task()],
        server=ServerConfig(),
        poll=PollConfig(
            interval_seconds=interval, jitter_seconds=jitter, plan_ttl_seconds=ttl, stop_at=stop_at
        ),
        notify=NotifyConfig(console=False, desktop=False, dedupe_seconds=dedupe),
    )


def hit_payload(train="G1", seat_text="有"):
    return {
        "ok": True,
        "rows": [{"train": train, "dep": "06:30", "seats": {"二等座": seat_text}}],
    }


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.dir.name) / "m.db"))
        self.clock = Clock()
        self.notifier = FakeNotifier()

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def build(self, **kwargs):
        self.engine = Engine(
            make_config(**kwargs), self.store, notifier=self.notifier, clock=self.clock
        )
        return self.engine

    # ------------------------------------------------------------ 计划发放
    def test_next_plan(self):
        engine = self.build()
        result = engine.next_plan("c1")
        self.assertFalse(result["idle"])
        plan = result["plan"]
        self.assertEqual(plan["from"]["code"], "VNP")
        self.assertEqual(plan["to"]["code"], "AOH")
        self.assertIn(plan["date"], (DATE_A, DATE_B))
        self.assertEqual(plan["cycle"], 1)

    def test_cooldown_gives_idle(self):
        engine = self.build(interval=8.0, tasks=[make_task(dates=[DATE_A])])
        engine.next_plan("c1")
        second = engine.next_plan("c1")
        self.assertTrue(second["idle"])
        self.assertGreater(second["retry_after"], 0)

    def test_round_robin_across_dates(self):
        engine = self.build(interval=8.0, jitter=0.0)
        first = engine.next_plan("c1")["plan"]
        engine.submit_report(first["id"], hit_payload())
        self.clock.advance(8.0)

        second = engine.next_plan("c1")["plan"]
        self.assertNotEqual(first["date"], second["date"])
        engine.submit_report(second["id"], hit_payload())
        self.clock.advance(8.0)

        third = engine.next_plan("c1")["plan"]
        self.assertEqual(third["date"], first["date"])
        self.assertEqual(third["cycle"], 2)

    def test_force_bypasses_cooldown(self):
        engine = self.build(interval=60.0, tasks=[make_task(dates=[DATE_A])])
        plan = engine.next_plan("c1")["plan"]
        engine.submit_report(plan["id"], hit_payload())
        self.assertTrue(engine.next_plan("c1")["idle"])
        forced = engine.next_plan("c1", force=True)
        self.assertFalse(forced["idle"])

    def test_stop_at(self):
        # 时钟固定在 1970-01-12 附近，把停止时间设在更早
        engine = self.build(stop_at="1970-01-05T00:00:00")
        result = engine.next_plan("c1")
        self.assertTrue(result["idle"])
        self.assertTrue(result["stopped"])

    # ------------------------------------------------------------ 回报
    def test_report_hit_creates_alert(self):
        engine = self.build()
        plan = engine.next_plan("c1")["plan"]
        result = engine.submit_report(plan["id"], hit_payload())
        self.assertTrue(result["ok"])
        self.assertTrue(result["accepted"])
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(len(self.store.list_alerts()), 1)
        self.assertEqual(len(self.notifier.alerts), 1)
        self.assertEqual(result["actions"], [])

    def test_report_no_hit(self):
        engine = self.build()
        plan = engine.next_plan("c1")["plan"]
        result = engine.submit_report(
            plan["id"], {"ok": True, "rows": [{"train": "G1", "seats": {"二等座": "无"}}]}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["matches"], [])
        self.assertEqual(self.store.list_alerts(), [])

    def test_dedupe_within_cooldown(self):
        engine = self.build(interval=0.0, dedupe=300.0, tasks=[make_task(dates=[DATE_A])])
        plan1 = engine.next_plan("c1")["plan"]
        engine.submit_report(plan1["id"], hit_payload())
        self.clock.advance(10)
        plan2 = engine.next_plan("c1")["plan"]
        result = engine.submit_report(plan2["id"], hit_payload())
        self.assertEqual(result["new_alerts"], [])
        self.assertEqual(len(self.notifier.alerts), 1)

    def test_alert_again_after_cooldown(self):
        engine = self.build(interval=0.0, dedupe=60.0, tasks=[make_task(dates=[DATE_A])])
        plan1 = engine.next_plan("c1")["plan"]
        engine.submit_report(plan1["id"], hit_payload())
        self.clock.advance(61)
        plan2 = engine.next_plan("c1")["plan"]
        result = engine.submit_report(plan2["id"], hit_payload())
        self.assertEqual(len(result["new_alerts"]), 1)
        self.assertEqual(len(self.notifier.alerts), 2)

    def test_failed_report_recorded(self):
        engine = self.build()
        plan = engine.next_plan("c1")["plan"]
        result = engine.submit_report(plan["id"], {"ok": False, "error": "等待结果超时"})
        self.assertTrue(result["ok"])
        self.assertFalse(result["accepted"])
        self.assertIn("超时", result["error"])
        self.assertEqual(engine._stats["failed_reports"], 1)

    def test_empty_rows_marks_failed(self):
        engine = self.build()
        plan = engine.next_plan("c1")["plan"]
        result = engine.submit_report(plan["id"], {"ok": True, "rows": []})
        self.assertFalse(result["accepted"])

    def test_expired_plan(self):
        engine = self.build(ttl=60.0)
        plan = engine.next_plan("c1")["plan"]
        self.clock.advance(120)
        result = engine.submit_report(plan["id"], hit_payload())
        self.assertFalse(result["ok"])
        self.assertIn("过期", result["error"])

    def test_unknown_plan(self):
        engine = self.build()
        result = engine.submit_report("nope", hit_payload())
        self.assertFalse(result["ok"])

    # ------------------------------------------------------------ 自动跳转
    def test_auto_jump_action(self):
        engine = self.build(tasks=[make_task(auto_jump=True, passengers=["张三"])])
        plan = engine.next_plan("c1")["plan"]
        result = engine.submit_report(plan["id"], hit_payload())
        self.assertEqual(len(result["actions"]), 1)
        action = result["actions"][0]
        self.assertEqual(action["type"], "jump_booking")
        self.assertEqual(action["train"], "G1")
        self.assertEqual(action["seat"], "二等座")
        self.assertEqual(action["passengers"], ["张三"])

    def test_auto_jump_skips_waitlist(self):
        engine = self.build(tasks=[make_task(auto_jump=True, allow_waitlist=True)])
        plan = engine.next_plan("c1")["plan"]
        result = engine.submit_report(plan["id"], hit_payload(seat_text="候补"))
        self.assertEqual(len(result["new_alerts"]), 1)
        self.assertEqual(result["actions"], [])

    def test_auto_submit_flag_in_action(self):
        engine = self.build(tasks=[make_task(auto_jump=True, auto_submit=True)])
        plan = engine.next_plan("c1")["plan"]
        result = engine.submit_report(plan["id"], hit_payload())
        self.assertEqual(len(result["actions"]), 1)
        self.assertTrue(result["actions"][0]["submit"])

    def test_auto_submit_off_by_default(self):
        engine = self.build(tasks=[make_task(auto_jump=True)])
        plan = engine.next_plan("c1")["plan"]
        result = engine.submit_report(plan["id"], hit_payload())
        self.assertFalse(result["actions"][0]["submit"])

    # ------------------------------------------------------------ 开售加速
    def test_burst_window_speeds_up(self):
        now = int(self.clock.value)
        burst = {
            "at": datetime.datetime.fromtimestamp(now - 5).isoformat(timespec="seconds"),
            "window_seconds": 60,
            "interval_seconds": 1.0,
            "jitter_seconds": 0.0,
        }
        cfg = make_config()
        cfg.poll.burst = burst
        engine = Engine(cfg, self.store, notifier=self.notifier, clock=self.clock)
        plan = engine.next_plan("c1")["plan"]
        self.assertAlmostEqual(engine._due[(plan["task_id"], plan["date"])], now + 1.0, places=3)

    def test_outside_burst_uses_normal_interval(self):
        now = int(self.clock.value)
        burst = {
            "at": datetime.datetime.fromtimestamp(now - 3600).isoformat(timespec="seconds"),
            "window_seconds": 60,
            "interval_seconds": 1.0,
            "jitter_seconds": 0.0,
        }
        cfg = make_config(interval=8.0, jitter=0.0)
        cfg.poll.burst = burst
        engine = Engine(cfg, self.store, notifier=self.notifier, clock=self.clock)
        plan = engine.next_plan("c1")["plan"]
        self.assertAlmostEqual(engine._due[(plan["task_id"], plan["date"])], now + 8.0, places=3)

    def test_jump_action_survives_dedupe(self):
        """告警去重只影响通知，不该影响自动跳转。"""
        engine = self.build(interval=0.0, dedupe=300.0, tasks=[
            make_task(dates=[DATE_A], auto_jump=True)
        ])
        plan1 = engine.next_plan("c1")["plan"]
        first = engine.submit_report(plan1["id"], hit_payload())
        self.assertEqual(len(first["new_alerts"]), 1)
        self.assertEqual(len(first["actions"]), 1)

        self.clock.advance(5)
        plan2 = engine.next_plan("c1")["plan"]
        second = engine.submit_report(plan2["id"], hit_payload())
        self.assertEqual(second["new_alerts"], [])          # 通知被去重
        self.assertEqual(len(second["actions"]), 1)         # 跳转照旧

    # ------------------------------------------------------------ 状态与事件
    def test_state_shape(self):
        engine = self.build()
        plan = engine.next_plan("c1")["plan"]
        engine.submit_report(plan["id"], hit_payload())
        state = engine.state()
        self.assertEqual(len(state["tasks"]), 1)
        self.assertTrue(state["tasks"][0]["dates_state"])
        self.assertEqual(len(state["alerts"]), 1)
        self.assertIn("stats", state)
        self.assertEqual(state["stats"]["clients_online"], 1)

    def test_broadcast_events(self):
        engine = self.build()
        channel = engine.subscribe()
        plan = engine.next_plan("c1")["plan"]
        engine.submit_report(plan["id"], hit_payload())
        events = []
        while not channel.empty():
            events.append(channel.get_nowait()["event"])
        self.assertIn("alert", events)
        self.assertIn("report", events)
        engine.unsubscribe(channel)

    def test_report_twice_same_plan(self):
        engine = self.build()
        plan = engine.next_plan("c1")["plan"]
        self.assertTrue(engine.submit_report(plan["id"], hit_payload())["ok"])
        second = engine.submit_report(plan["id"], hit_payload())
        self.assertFalse(second["ok"])

    def test_snapshot_stored(self):
        engine = self.build()
        plan = engine.next_plan("c1")["plan"]
        engine.submit_report(plan["id"], hit_payload())
        snapshot = engine._last[(plan["task_id"], plan["date"])]
        self.assertEqual(snapshot["row_count"], 1)
        self.assertEqual(snapshot["cycle"], 1)


if __name__ == "__main__":
    unittest.main()
