"""12306 余票监控 —— 核心数据结构。

约定：本包只做「调度 / 判定 / 告警 / 展示」，不代登录、不代提交订单。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# 12306 页面表头里出现的席别顺序（用于兜底解析）
SEAT_NAMES: tuple = (
    "商务座",
    "优选一等座",
    "特等座",
    "一等座",
    "二等座",
    "高级软卧",
    "动卧",
    "软卧",
    "硬卧",
    "软座",
    "硬座",
    "无座",
)

STATUS_AVAILABLE = "available"
STATUS_WAITLIST = "waitlist"
STATUS_NONE = "none"
STATUS_UNKNOWN = "unknown"

LEVEL_AVAILABLE = "available"
LEVEL_WAITLIST = "waitlist"

_NUM_RE = re.compile(r"(\d+)")
_TRAIN_RE = re.compile(r"[^0-9A-Za-z]")


def normalize_train(value: str) -> str:
    """把车次规范成可比较形式：去空格/符号、转大写。"""
    return _TRAIN_RE.sub("", str(value or "")).upper()


@dataclass(frozen=True)
class Station:
    name: str
    code: str

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return "%s(%s)" % (self.name, self.code)

    def to_dict(self) -> Dict[str, str]:
        return {"name": self.name, "code": self.code}


@dataclass(frozen=True)
class SeatInfo:
    """单个席别的余票状态。"""

    text: str
    status: str
    count: Optional[int] = None

    @classmethod
    def parse(cls, text: object) -> "SeatInfo":
        raw = "" if text is None else str(text)
        compact = raw.replace(" ", "").replace("\u3000", "").strip()
        # 折扣票会在余票格里叠一个「折」角标，读出来是「6折」这种形式
        compact = compact.replace("折", "").strip()
        if compact in ("", "--", "-", "—", "/", "*", "－", "…"):
            return cls(raw.strip(), STATUS_UNKNOWN, None)
        if compact in ("无", "无票", "0"):
            return cls(raw.strip(), STATUS_NONE, 0)
        if "候补" in compact:
            return cls(raw.strip(), STATUS_WAITLIST, 0)
        if "有" in compact:
            return cls(raw.strip(), STATUS_AVAILABLE, None)
        match = _NUM_RE.search(compact)
        if match:
            count = int(match.group(1))
            status = STATUS_AVAILABLE if count > 0 else STATUS_NONE
            return cls(raw.strip(), status, count)
        return cls(raw.strip(), STATUS_UNKNOWN, None)

    @property
    def is_hit(self) -> bool:
        return self.status in (STATUS_AVAILABLE, STATUS_WAITLIST)

    def to_dict(self) -> Dict[str, object]:
        return {"text": self.text, "status": self.status, "count": self.count}


@dataclass
class TrainRow:
    """浏览器解析出的一列车（单程方向）。"""

    train: str
    dep: str = ""
    arr: str = ""
    duration: str = ""
    bookable: bool = True
    seats: Dict[str, SeatInfo] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Dict[str, object]) -> "TrainRow":
        seats_raw = payload.get("seats") or {}
        seats: Dict[str, SeatInfo] = {}
        if isinstance(seats_raw, dict):
            for name, text in seats_raw.items():
                seats[str(name)] = SeatInfo.parse(text)
        return cls(
            train=str(payload.get("train") or "").strip(),
            dep=str(payload.get("dep") or "").strip(),
            arr=str(payload.get("arr") or "").strip(),
            duration=str(payload.get("duration") or "").strip(),
            bookable=bool(payload.get("bookable", True)),
            seats=seats,
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "train": self.train,
            "dep": self.dep,
            "arr": self.arr,
            "duration": self.duration,
            "bookable": self.bookable,
            "seats": {k: v.to_dict() for k, v in self.seats.items()},
        }


@dataclass
class Match:
    """一次「命中」：某任务在某日期某车次某席别满足条件。"""

    task_id: str
    date: str
    train: str
    seat: str
    info: SeatInfo
    level: str
    dep: str = ""
    task_note: str = ""

    @property
    def key(self) -> str:
        return "|".join((self.task_id, self.date, self.train, self.seat, self.level))

    def message(self) -> str:
        if self.info.count is None:
            stock = "有票"
        else:
            stock = "%d 张" % self.info.count
        if self.level == LEVEL_WAITLIST:
            stock = "可候补"
        head = "%s %s %s" % (self.date, self.train, self.seat)
        return "%s：%s%s" % (head, stock, "") if not self.dep else "%s（%s 发车）：%s" % (
            head,
            self.dep,
            stock,
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "task_id": self.task_id,
            "date": self.date,
            "train": self.train,
            "seat": self.seat,
            "level": self.level,
            "dep": self.dep,
            "text": self.info.text,
            "count": self.info.count,
            "message": self.message(),
        }
