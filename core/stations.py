"""车站字典：站名 / 电报码 / 拼音 之间的解析。

数据来自 12306 官方 `station_name.js`，由 `python run.py fetch-stations` 生成。
运行时不联网即可使用。
"""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

from .models import Station

DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data" / "stations.json"


class StationIndex:
    def __init__(self, data_path: Optional[Path] = None) -> None:
        self.path = Path(data_path) if data_path else DEFAULT_DATA
        self.generated_at = ""
        self._by_code: Dict[str, Station] = {}
        self._by_name: Dict[str, Station] = {}
        self._by_alias: Dict[str, Station] = {}
        self._load()

    # ---------------------------------------------------------------- loading
    def _load(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(
                "找不到车站字典 %s，请先运行：python run.py fetch-stations" % self.path
            )
        with open(self.path, "r", encoding="utf-8") as fp:
            payload = json.load(fp)
        self.generated_at = payload.get("generated_at", "")
        for row in payload.get("stations", []):
            name, code = row[0], row[1]
            pinyin = row[2] if len(row) > 2 else ""
            abbr = row[3] if len(row) > 3 else ""
            station = Station(name=name, code=str(code).upper())
            self._by_code[station.code] = station
            self._by_name[name] = station
            for alias in (pinyin, abbr):
                if alias:
                    self._by_alias.setdefault(alias.lower(), station)

    @property
    def count(self) -> int:
        return len(self._by_code)

    # ---------------------------------------------------------------- lookup
    def resolve(self, text: str) -> Optional[Station]:
        """把用户写的站名解析成 (name, code)。支持站名、电报码、拼音。"""
        if not text:
            return None
        raw = str(text).strip()
        if not raw:
            return None

        if raw in self._by_name:
            return self._by_name[raw]

        upper = raw.upper()
        if upper in self._by_code:
            return self._by_code[upper]

        lowered = raw.lower()
        if lowered in self._by_alias:
            return self._by_alias[lowered]

        # 「北京南站」这类多写了一个「站」的写法
        if raw.endswith("站") and len(raw) > 2:
            trimmed = raw[:-1]
            if trimmed in self._by_name:
                return self._by_name[trimmed]
            if trimmed.upper() in self._by_code:
                return self._by_code[trimmed.upper()]

        return None

    def suggestions(self, text: str, limit: int = 5) -> List[str]:
        pool = list(self._by_name)
        return difflib.get_close_matches(str(text or "").strip(), pool, n=limit, cutoff=0.3)

    def search(self, keyword: str, limit: int = 20) -> List[Station]:
        key = str(keyword or "").strip()
        if not key:
            return []
        lowered = key.lower()
        result: List[Station] = []
        seen = set()
        for station in self._by_code.values():
            if key in station.name or lowered in station.code.lower():
                if station.code not in seen:
                    seen.add(station.code)
                    result.append(station)
        for alias, station in self._by_alias.items():
            if lowered in alias and station.code not in seen:
                seen.add(station.code)
                result.append(station)
        result.sort(key=lambda s: (len(s.name), s.name))
        return result[:limit]


_INDEX: Optional[StationIndex] = None


def get_index(data_path: Optional[Path] = None) -> StationIndex:
    """进程内单例，避免每次请求都读 12 万字节 JSON。"""
    global _INDEX
    if _INDEX is None:
        _INDEX = StationIndex(data_path or DEFAULT_DATA)
    return _INDEX


def fetch_and_save(target: Optional[Path] = None, timeout: int = 30) -> Path:
    """从 12306 拉取最新 station_name.js 并写入 JSON。"""
    import datetime
    import re
    import ssl
    import urllib.request

    url = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
    target = Path(target) if target else DEFAULT_DATA
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://kyfw.12306.cn/"}
    )
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        text = response.read().decode("utf-8", "replace")

    match = re.search(r"station_names\s*=\s*'(.*?)';", text, re.S)
    if not match:
        raise RuntimeError("station_name.js 结构已变化，未找到 station_names 变量")

    rows: List[List[str]] = []
    seen = set()
    for record in match.group(1).split("@"):
        parts = record.split("|")
        if len(parts) < 5:
            continue
        abbr, name, code, pinyin = parts[0], parts[1], parts[2], parts[3]
        if not code or code in seen:
            continue
        seen.add(code)
        rows.append([name, code, pinyin, abbr])

    if len(rows) < 1000:
        raise RuntimeError("解析到的车站数量异常（%d），已中止写入" % len(rows))

    payload = {
        "source": url,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "schema": ["name", "code", "pinyin", "abbr"],
        "count": len(rows),
        "stations": rows,
    }
    os.makedirs(str(target.parent), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, separators=(",", ":"))

    global _INDEX
    _INDEX = None
    return target
