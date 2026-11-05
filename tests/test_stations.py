import unittest

from core.stations import get_index


class StationIndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = get_index()

    def test_loaded(self):
        self.assertGreater(self.index.count, 1000)

    def test_by_name(self):
        station = self.index.resolve("北京南")
        self.assertIsNotNone(station)
        self.assertEqual(station.code, "VNP")

    def test_by_code(self):
        station = self.index.resolve("AOH")
        self.assertEqual(station.name, "上海虹桥")

    def test_by_pinyin(self):
        station = self.index.resolve("shanghaihongqiao")
        self.assertEqual(station.name, "上海虹桥")

    def test_by_abbr(self):
        station = self.index.resolve("bjn")
        self.assertEqual(station.name, "北京南")

    def test_trailing_zhan(self):
        station = self.index.resolve("北京南站")
        self.assertEqual(station.code, "VNP")

    def test_unknown(self):
        self.assertIsNone(self.index.resolve("不存在的车站"))
        self.assertIsNone(self.index.resolve(""))

    def test_suggestions(self):
        self.assertIn("北京南", " ".join(self.index.suggestions("北京难")))

    def test_search(self):
        names = [s.name for s in self.index.search("虹桥")]
        self.assertIn("上海虹桥", names)

    def test_exact_name_beats_prefix(self):
        # 北京 与 北京南 都存在，精确匹配必须命中「北京」
        self.assertEqual(self.index.resolve("北京").name, "北京")


if __name__ == "__main__":
    unittest.main()
