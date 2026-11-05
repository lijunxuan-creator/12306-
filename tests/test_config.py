import json
import tempfile
import unittest
from pathlib import Path

from core import config as config_mod


def write_config(payload) -> str:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump(payload, handle, ensure_ascii=False)
    handle.close()
    return handle.name


BASE_TASK = {
    "id": "t1",
    "from": "北京南",
    "to": "上海虹桥",
    "dates": ["2030-01-01"],
    "seats": ["二等座"],
}


class ConfigLoadTest(unittest.TestCase):
    def tearDown(self):
        for path in getattr(self, "_paths", []):
            Path(path).unlink(missing_ok=True)

    def load(self, payload):
        path = write_config(payload)
        self._paths = getattr(self, "_paths", []) + [path]
        return config_mod.load(path)

    def test_minimal_valid(self):
        cfg = self.load({"tasks": [BASE_TASK]})
        self.assertEqual(len(cfg.tasks), 1)
        task = cfg.tasks[0]
        self.assertEqual(task.from_station.code, "VNP")
        self.assertEqual(task.to_station.name, "上海虹桥")
        self.assertEqual(task.seats, ["二等座"])
        self.assertEqual(cfg.server.port, 8765)
        self.assertTrue(cfg.notify.console)

    def test_unknown_station_message(self):
        bad = dict(BASE_TASK, to="北京难")
        with self.assertRaises(config_mod.ConfigError) as ctx:
            self.load({"tasks": [bad]})
        self.assertIn("北京难", str(ctx.exception))
        self.assertIn("北京南", str(ctx.exception))  # 给出相近建议

    def test_same_station_rejected(self):
        bad = dict(BASE_TASK, to="北京南")
        with self.assertRaises(config_mod.ConfigError):
            self.load({"tasks": [bad]})

    def test_bad_date(self):
        bad = dict(BASE_TASK, dates=["2030/01/01"])
        with self.assertRaises(config_mod.ConfigError):
            self.load({"tasks": [bad]})

    def test_empty_tasks(self):
        with self.assertRaises(config_mod.ConfigError):
            self.load({"tasks": []})

    def test_duplicate_ids(self):
        with self.assertRaises(config_mod.ConfigError):
            self.load({"tasks": [BASE_TASK, dict(BASE_TASK)]})

    def test_missing_seats(self):
        bad = {k: v for k, v in BASE_TASK.items() if k != "seats"}
        with self.assertRaises(config_mod.ConfigError):
            self.load({"tasks": [bad]})

    def test_seat_aliases(self):
        cfg = self.load({"tasks": [dict(BASE_TASK, seats=["二等", "硬卧"])]})
        self.assertEqual(cfg.tasks[0].seats, ["二等座", "硬卧"])

    def test_presale_warning(self):
        cfg = self.load({"tasks": [dict(BASE_TASK, dates=["2099-01-01"])]})
        self.assertTrue(any("预售期" in w for w in cfg.warnings))

    def test_all_disabled_rejected(self):
        with self.assertRaises(config_mod.ConfigError):
            self.load({"tasks": [dict(BASE_TASK, enabled=False)]})

    def test_small_interval_warns(self):
        # interval+jitter < 3 直接报错
        with self.assertRaises(config_mod.ConfigError):
            self.load({"tasks": [BASE_TASK], "poll": {"interval_seconds": 1, "jitter_seconds": 0}})
        # interval < 5 只给提醒
        cfg = self.load({"tasks": [BASE_TASK], "poll": {"interval_seconds": 4}})
        self.assertTrue(any("滑块" in w for w in cfg.warnings))

    def test_bad_stop_at(self):
        with self.assertRaises(config_mod.ConfigError):
            self.load({"tasks": [BASE_TASK], "poll": {"stop_at": "明天"}})

    def test_webhook_requires_url(self):
        with self.assertRaises(config_mod.ConfigError):
            self.load({"tasks": [BASE_TASK], "notify": {"webhook": {"enabled": True}}})

    def test_summary(self):
        cfg = self.load({"tasks": [BASE_TASK]})
        summary = cfg.summary()
        self.assertEqual(summary["enabled_tasks"], 1)
        self.assertIn("console", summary["channels"])

    def test_auto_submit_default_off(self):
        cfg = self.load({"tasks": [BASE_TASK]})
        self.assertFalse(cfg.tasks[0].auto_submit)

    def test_auto_submit_enabled(self):
        cfg = self.load({"tasks": [dict(BASE_TASK, auto_jump=True, auto_submit=True)]})
        self.assertTrue(cfg.tasks[0].auto_submit)
        self.assertTrue(cfg.tasks[0].to_dict()["auto_submit"])

    def test_burst_parsed(self):
        cfg = self.load({
            "tasks": [BASE_TASK],
            "poll": {"burst": {"at": "2030-01-01T12:59:50", "window_seconds": 60,
                               "interval_seconds": 1.0, "jitter_seconds": 0}},
        })
        self.assertEqual(cfg.poll.burst["at"], "2030-01-01T12:59:50")
        self.assertEqual(cfg.poll.burst["window_seconds"], 60.0)
        self.assertEqual(cfg.poll.burst["interval_seconds"], 1.0)

    def test_burst_bad_time(self):
        with self.assertRaises(config_mod.ConfigError):
            self.load({"tasks": [BASE_TASK], "poll": {"burst": {"at": "中午十二点"}}})

    def test_burst_too_fast_warns(self):
        cfg = self.load({
            "tasks": [BASE_TASK],
            "poll": {"burst": {"at": "2030-01-01T12:59:50", "interval_seconds": 0.2}},
        })
        self.assertTrue(any("滑块" in w for w in cfg.warnings))


if __name__ == "__main__":
    unittest.main()
