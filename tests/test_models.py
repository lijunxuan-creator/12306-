import unittest

from core.models import (
    STATUS_AVAILABLE,
    STATUS_NONE,
    STATUS_UNKNOWN,
    STATUS_WAITLIST,
    SeatInfo,
    TrainRow,
    normalize_train,
)


class SeatParseTest(unittest.TestCase):
    def test_available_literal(self):
        info = SeatInfo.parse("有")
        self.assertEqual(info.status, STATUS_AVAILABLE)
        self.assertIsNone(info.count)
        self.assertTrue(info.is_hit)

    def test_available_count(self):
        info = SeatInfo.parse("12")
        self.assertEqual(info.status, STATUS_AVAILABLE)
        self.assertEqual(info.count, 12)

    def test_zero_means_none(self):
        info = SeatInfo.parse("0")
        self.assertEqual(info.status, STATUS_NONE)
        self.assertFalse(info.is_hit)

    def test_none(self):
        self.assertEqual(SeatInfo.parse("无").status, STATUS_NONE)
        self.assertEqual(SeatInfo.parse("无票").status, STATUS_NONE)

    def test_waitlist(self):
        info = SeatInfo.parse("候补")
        self.assertEqual(info.status, STATUS_WAITLIST)
        self.assertTrue(info.is_hit)

    def test_unknown_tokens(self):
        for text in ("", "--", "-", "—", "*", None):
            self.assertEqual(SeatInfo.parse(text).status, STATUS_UNKNOWN, text)

    def test_noisy_text(self):
        info = SeatInfo.parse(" 有 \n")
        self.assertEqual(info.status, STATUS_AVAILABLE)
        info = SeatInfo.parse("剩余 8 张")
        self.assertEqual((info.status, info.count), (STATUS_AVAILABLE, 8))

    def test_discount_badge(self):
        # 折扣票的角标会和数字一起被读出来：「6折」
        info = SeatInfo.parse("6折")
        self.assertEqual((info.status, info.count), (STATUS_AVAILABLE, 6))
        self.assertEqual(SeatInfo.parse("有折").status, STATUS_AVAILABLE)
        self.assertEqual(SeatInfo.parse("折").status, STATUS_UNKNOWN)


class TrainRowTest(unittest.TestCase):
    def test_from_payload(self):
        row = TrainRow.from_payload(
            {
                "train": "G1",
                "dep": "06:30",
                "arr": "12:00",
                "duration": "05:30",
                "seats": {"二等座": "有", "一等座": "3", "商务座": "无"},
            }
        )
        self.assertEqual(row.train, "G1")
        self.assertEqual(row.seats["二等座"].status, STATUS_AVAILABLE)
        self.assertEqual(row.seats["商务座"].status, STATUS_NONE)

    def test_payload_missing_fields(self):
        row = TrainRow.from_payload({"train": "D22"})
        self.assertEqual(row.train, "D22")
        self.assertEqual(row.seats, {})

    def test_normalize_train(self):
        self.assertEqual(normalize_train(" g1 "), "G1")
        self.assertEqual(normalize_train("G-1"), "G1")


if __name__ == "__main__":
    unittest.main()
