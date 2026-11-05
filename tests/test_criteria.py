import unittest

from core.criteria import best_match, evaluate, seat_match, summarize
from core.config import Task
from core.models import (
    LEVEL_AVAILABLE,
    LEVEL_WAITLIST,
    Station,
    TrainRow,
)


def make_task(**overrides):
    params = dict(
        id="t1",
        from_station=Station("北京南", "VNP"),
        to_station=Station("上海虹桥", "AOH"),
        dates=["2030-01-01"],
        seats=["二等座"],
        trains=[],
    )
    params.update(overrides)
    return Task(**params)


def row(train, **seats):
    return TrainRow.from_payload({"train": train, "seats": seats})


class SeatMatchTest(unittest.TestCase):
    def test_min_count(self):
        info = TrainRow.from_payload({"train": "G1", "seats": {"二等座": "2"}}).seats["二等座"]
        self.assertEqual(seat_match(info, min_count=1), LEVEL_AVAILABLE)
        self.assertEqual(seat_match(info, min_count=2), LEVEL_AVAILABLE)
        self.assertEqual(seat_match(info, min_count=3), "")

    def test_literal_you_ignores_min_count(self):
        info = TrainRow.from_payload({"train": "G1", "seats": {"二等座": "有"}}).seats["二等座"]
        self.assertEqual(seat_match(info, min_count=5), LEVEL_AVAILABLE)

    def test_waitlist_opt_in(self):
        info = TrainRow.from_payload({"train": "G1", "seats": {"二等座": "候补"}}).seats["二等座"]
        self.assertEqual(seat_match(info, allow_waitlist=False), "")
        self.assertEqual(seat_match(info, allow_waitlist=True), LEVEL_WAITLIST)


class EvaluateTest(unittest.TestCase):
    def test_no_filter_matches_all(self):
        task = make_task()
        rows = [row("G1", **{"二等座": "有"}), row("D22", **{"二等座": "3"})]
        matches = evaluate(task, "2030-01-01", rows)
        self.assertEqual(len(matches), 2)

    def test_train_filter(self):
        task = make_task(trains=["G1"])
        rows = [row("G1", **{"二等座": "有"}), row("D22", **{"二等座": "3"})]
        matches = evaluate(task, "2030-01-01", rows)
        self.assertEqual([m.train for m in matches], ["G1"])

    def test_seat_filter(self):
        task = make_task(seats=["一等座"])
        rows = [row("G1", **{"二等座": "有", "一等座": "无"})]
        self.assertEqual(evaluate(task, "2030-01-01", rows), [])

    def test_no_hit_when_none(self):
        task = make_task()
        rows = [row("G1", **{"二等座": "无"}), row("G2", **{"二等座": "--"})]
        self.assertEqual(evaluate(task, "2030-01-01", rows), [])

    def test_available_beats_waitlist(self):
        task = make_task(allow_waitlist=True)
        rows = [
            row("G1", **{"二等座": "候补"}),
            row("G9", **{"二等座": "有"}),
        ]
        matches = evaluate(task, "2030-01-01", rows)
        self.assertEqual(matches[0].train, "G9")
        self.assertEqual(matches[0].level, LEVEL_AVAILABLE)

    def test_train_priority_order(self):
        task = make_task(trains=["G5", "G1"])
        rows = [row("G1", **{"二等座": "有"}), row("G5", **{"二等座": "有"})]
        matches = evaluate(task, "2030-01-01", rows)
        self.assertEqual([m.train for m in matches], ["G5", "G1"])

    def test_min_count_filters(self):
        task = make_task(min_count=3)
        rows = [row("G1", **{"二等座": "2"}), row("G2", **{"二等座": "5"})]
        matches = evaluate(task, "2030-01-01", rows)
        self.assertEqual([m.train for m in matches], ["G2"])

    def test_message_and_key(self):
        task = make_task()
        matches = evaluate(task, "2030-01-01", [row("G1", **{"二等座": "有"})])
        match = matches[0]
        self.assertIn("G1", match.message())
        self.assertEqual(match.key, "t1|2030-01-01|G1|二等座|available")

    def test_best_match_empty(self):
        self.assertIsNone(best_match([]))

    def test_summarize(self):
        rows = [row("G1", **{"二等座": "有", "一等座": "无"})]
        summary = summarize(rows)
        self.assertEqual(summary[0]["train"], "G1")
        self.assertEqual(summary[0]["hits"], {"二等座": "有"})


if __name__ == "__main__":
    unittest.main()
