import tempfile
import unittest
from pathlib import Path

from core.store import Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.dir.name) / "sub" / "monitor.db")
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_creates_parent_dirs(self):
        self.assertTrue(Path(self.path).exists())

    def test_should_alert_once(self):
        self.assertTrue(self.store.should_alert("k1", cooldown=300, now=1000))
        self.assertFalse(self.store.should_alert("k1", cooldown=300, now=1100))

    def test_should_alert_after_cooldown(self):
        self.assertTrue(self.store.should_alert("k1", cooldown=300, now=1000))
        self.assertFalse(self.store.should_alert("k1", cooldown=300, now=1200))
        self.assertTrue(self.store.should_alert("k1", cooldown=300, now=1400))

    def test_independent_keys(self):
        self.assertTrue(self.store.should_alert("a", 300, now=1000))
        self.assertTrue(self.store.should_alert("b", 300, now=1001))

    def test_alert_crud(self):
        alert_id = self.store.record_alert(
            {"ts": 1000, "key": "k", "task_id": "t1", "date": "2030-01-01",
             "train": "G1", "seat": "二等座", "level": "available", "message": "有票"}
        )
        self.assertGreater(alert_id, 0)
        alerts = self.store.list_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["train"], "G1")
        self.assertTrue(self.store.ack_alert(alert_id))
        self.assertFalse(self.store.ack_alert(99999))
        self.assertEqual(len(self.store.list_alerts(only_new=True)), 0)
        self.assertEqual(self.store.clear_alerts(), 1)
        self.assertEqual(self.store.list_alerts(), [])

    def test_clients(self):
        self.store.upsert_client("c1", {"page": "leftTicket"})
        self.store.upsert_client("c1", {"page": "other"})
        clients = self.store.list_clients()
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0]["info"]["page"], "other")
        self.assertTrue(clients[0]["online"])

    def test_stale_client_offline(self):
        self.store.upsert_client("c1")
        clients = self.store.list_clients(stale_after=-1)
        self.assertFalse(clients[0]["online"])

    def test_stats_and_prune(self):
        self.store.record_alert({"ts": 1000, "key": "old"})
        self.store.should_alert("k", 10, now=1000)
        self.assertEqual(self.store.stats()["alerts"], 1)
        removed = self.store.prune(days=1, now=10 * 86400 + 1000)
        self.assertEqual(removed, 1)
        self.assertEqual(self.store.stats()["alerts"], 0)


if __name__ == "__main__":
    unittest.main()
