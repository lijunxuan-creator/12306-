"""配置加载与校验。

配置文件是严格 JSON（不用注释，避免和 Webhook URL 里的 `//` 冲突），
可用的中文说明见 config.example.json 与 README。
"""

from __future__ import annotations

import datetime
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .models import SEAT_NAMES, Station
from .stations import StationIndex, get_index

PRESALE_DAYS = 15

_SEAT_ALIASES = {
    "二等": "二等座",
    "一等": "一等座",
    "商务": "商务座",
    "特等": "特等座",
    "软卧": "软卧",
    "硬卧": "硬卧",
    "软座": "软座",
    "硬座": "硬座",
    "无座": "无座",
    "站票": "无座",
    "卧铺": "硬卧",
    "动卧": "动卧",
    "高铁": "二等座",
}


class ConfigError(Exception):
    """配置有问题（会连带给出修改建议）。"""


def normalize_seat(text: str) -> str:
    raw = str(text or "").strip()
    if raw in SEAT_NAMES:
        return raw
    if raw in _SEAT_ALIASES:
        return _SEAT_ALIASES[raw]
    for name in SEAT_NAMES:
        if name in raw:
            return name
    return raw


@dataclass
class Task:
    id: str
    from_station: Station
    to_station: Station
    dates: List[str]
    seats: List[str]
    trains: List[str] = field(default_factory=list)
    min_count: int = 1
    allow_waitlist: bool = False
    auto_jump: bool = False
    auto_submit: bool = False
    passengers: List[str] = field(default_factory=list)
    note: str = ""
    enabled: bool = True

    @property
    def route(self) -> str:
        return "%s→%s" % (self.from_station.name, self.to_station.name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "enabled": self.enabled,
            "route": self.route,
            "from": self.from_station.to_dict(),
            "to": self.to_station.to_dict(),
            "dates": list(self.dates),
            "seats": list(self.seats),
            "trains": list(self.trains),
            "min_count": self.min_count,
            "allow_waitlist": self.allow_waitlist,
            "auto_jump": self.auto_jump,
            "auto_submit": self.auto_submit,
            "passengers": list(self.passengers),
            "note": self.note,
        }


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    token: Optional[str] = None


@dataclass
class PollConfig:
    interval_seconds: float = 8.0
    jitter_seconds: float = 3.0
    plan_ttl_seconds: float = 90.0
    stop_at: Optional[str] = None
    # 开售瞬间加速：{"at": "...", "window_seconds": 90,
    #                "interval_seconds": 1.0, "jitter_seconds": 0.2}
    burst: Optional[Dict[str, Any]] = None


@dataclass
class NotifyConfig:
    console: bool = True
    desktop: bool = True
    sound: bool = True
    dedupe_seconds: float = 300.0
    webhook: Dict[str, Any] = field(
        default_factory=lambda: {"enabled": False, "kind": "generic", "url": "", "headers": {}}
    )
    email: Dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": False,
            "smtp_host": "",
            "smtp_port": 465,
            "ssl": True,
            "user": "",
            "password": "",
            "to": [],
        }
    )


@dataclass
class Config:
    tasks: List[Task]
    server: ServerConfig
    poll: PollConfig
    notify: NotifyConfig
    warnings: List[str] = field(default_factory=list)
    path: str = ""

    def enabled_tasks(self) -> List[Task]:
        return [task for task in self.tasks if task.enabled]

    def find_task(self, task_id: str) -> Optional[Task]:
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def summary(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "tasks": len(self.tasks),
            "enabled_tasks": len(self.enabled_tasks()),
            "dates": sorted({date for task in self.tasks for date in task.dates}),
            "interval_seconds": self.poll.interval_seconds,
            "jitter_seconds": self.poll.jitter_seconds,
            "stop_at": self.poll.stop_at,
            "burst": self.poll.burst,
            "channels": [
                name
                for name, on in (
                    ("console", self.notify.console),
                    ("desktop", self.notify.desktop),
                    ("webhook", bool(self.notify.webhook.get("enabled"))),
                    ("email", bool(self.notify.email.get("enabled"))),
                )
                if on
            ],
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------- helpers
def _parse_date(value: Any, where: str) -> str:
    text = str(value or "").strip()
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        raise ConfigError("%s 的日期 %r 不是 YYYY-MM-DD 格式" % (where, value)) from None
    return text


def _resolve_station(
    index: StationIndex, value: Any, where: str, warnings: List[str]
) -> Station:
    station = index.resolve(str(value or ""))
    if station is None:
        hint = "、".join(index.suggestions(str(value or ""))) or "（无相近站名）"
        raise ConfigError(
            "%s 的车站 %r 无法识别。相近写法：%s" % (where, value, hint)
        )
    return station


def _as_list(value: Any, where: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ConfigError("%s 应该是字符串数组" % where)


def _number(raw: Dict[str, Any], key: str, default: float, where: str) -> float:
    """取数字配置项。注意不能用 `value or default`，否则显式写 0 会被当成缺省。"""
    if key not in raw or raw[key] is None:
        return default
    try:
        return float(raw[key])
    except (TypeError, ValueError):
        raise ConfigError("%s 必须是数字，当前是 %r" % (where, raw[key])) from None


def _task_from_dict(
    raw: Dict[str, Any],
    position: int,
    index: StationIndex,
    warnings: List[str],
    seen_ids: set,
) -> Task:
    where = "tasks[%d]" % position
    if not isinstance(raw, dict):
        raise ConfigError("%s 必须是对象" % where)

    task_id = str(raw.get("id") or "task%d" % (position + 1)).strip()
    if task_id in seen_ids:
        raise ConfigError("%s 的 id %r 重复了" % (where, task_id))
    seen_ids.add(task_id)

    if "from" not in raw or "to" not in raw:
        raise ConfigError("%s 缺少 from / to" % where)
    from_station = _resolve_station(index, raw["from"], where + ".from", warnings)
    to_station = _resolve_station(index, raw["to"], where + ".to", warnings)
    if from_station.code == to_station.code:
        raise ConfigError("%s 的出发站和到达站相同：%s" % (where, from_station.name))

    dates = _as_list(raw.get("dates"), where + ".dates")
    if not dates:
        raise ConfigError("%s.dates 不能为空（如 [\"2030-01-01\"]）" % where)
    today = datetime.date.today()
    checked: List[str] = []
    for date in dates:
        date = _parse_date(date, where + ".dates")
        try:
            offset = (datetime.date.fromisoformat(date) - today).days
        except ValueError:  # pragma: no cover - 已被 _parse_date 拦住
            offset = 0
        if offset < 0:
            warnings.append("%s 的日期 %s 已经过去，该日期不会被查询" % (where, date))
        elif offset > PRESALE_DAYS:
            warnings.append(
                "%s 的日期 %s 距今天 %d 天，超出 12306 预售期（%d 天），开售前查不到票"
                % (where, date, offset, PRESALE_DAYS)
            )
        if date not in checked:
            checked.append(date)
    dates = checked

    seats_raw = _as_list(raw.get("seats"), where + ".seats")
    if not seats_raw:
        raise ConfigError(
            "%s.seats 不能为空，可用值：%s" % (where, " / ".join(SEAT_NAMES))
        )
    seats: List[str] = []
    for seat in seats_raw:
        name = normalize_seat(seat)
        if name not in SEAT_NAMES:
            warnings.append("%s 的席别 %r 不在常见列表里，仍按原样匹配" % (where, seat))
        if name not in seats:
            seats.append(name)

    trains = [str(item).upper().replace(" ", "") for item in _as_list(raw.get("trains"), where + ".trains")]
    if not trains:
        warnings.append("%s 未指定 trains，将匹配该区间全部车次" % where)

    min_count = raw.get("min_count", 1)
    try:
        min_count = int(min_count)
    except (TypeError, ValueError):
        raise ConfigError("%s.min_count 必须是整数" % where) from None
    if min_count < 1:
        raise ConfigError("%s.min_count 至少为 1" % where)

    return Task(
        id=task_id,
        enabled=bool(raw.get("enabled", True)),
        from_station=from_station,
        to_station=to_station,
        dates=dates,
        seats=seats,
        trains=trains,
        min_count=min_count,
        allow_waitlist=bool(raw.get("allow_waitlist", False)),
        auto_jump=bool(raw.get("auto_jump", False)),
        auto_submit=bool(raw.get("auto_submit", False)),
        passengers=_as_list(raw.get("passengers"), where + ".passengers"),
        note=str(raw.get("note") or ""),
    )


def load(path: str, index: Optional[StationIndex] = None) -> Config:
    if not os.path.exists(path):
        raise ConfigError("找不到配置文件：%s（可复制 config.example.json 一份）" % path)
    with open(path, "r", encoding="utf-8-sig") as fp:
        try:
            raw = json.load(fp)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                "配置文件 JSON 解析失败：第 %d 行第 %d 列 %s" % (exc.lineno, exc.colno, exc.msg)
            ) from None
    if not isinstance(raw, dict):
        raise ConfigError("配置文件最外层必须是对象 { ... }")

    index = index or get_index()
    warnings: List[str] = []

    tasks_raw = raw.get("tasks")
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise ConfigError("配置里必须有非空的 tasks 数组")
    seen_ids: set = set()
    tasks = [
        _task_from_dict(item, i, index, warnings, seen_ids)
        for i, item in enumerate(tasks_raw)
    ]
    if not any(task.enabled for task in tasks):
        raise ConfigError("所有任务都是 enabled=false，没有可监控的内容")

    server_raw = raw.get("server") or {}
    host = str(server_raw.get("host") or "127.0.0.1")
    try:
        port = int(server_raw.get("port") or 8765)
    except (TypeError, ValueError):
        raise ConfigError("server.port 必须是整数") from None
    token = server_raw.get("token") or None
    server = ServerConfig(host=host, port=port, token=str(token) if token else None)

    poll_raw = raw.get("poll") or {}
    interval = _number(poll_raw, "interval_seconds", 8.0, "poll.interval_seconds")
    jitter = _number(poll_raw, "jitter_seconds", 3.0, "poll.jitter_seconds")
    ttl = _number(poll_raw, "plan_ttl_seconds", 90.0, "poll.plan_ttl_seconds")
    if interval < 5:
        warnings.append(
            "poll.interval_seconds=%.1f 偏小，12306 可能弹滑块验证；建议 ≥ 8 秒" % interval
        )
    if interval + jitter < 3:
        raise ConfigError("interval_seconds + jitter_seconds 至少 3 秒")
    stop_at = poll_raw.get("stop_at") or None
    if stop_at:
        try:
            datetime.datetime.fromisoformat(str(stop_at))
        except ValueError:
            raise ConfigError("poll.stop_at 需要形如 2030-01-01T22:00:00") from None
        stop_at = str(stop_at)

    # 开售瞬间加速窗口
    burst = None
    burst_raw = poll_raw.get("burst")
    if isinstance(burst_raw, dict) and burst_raw.get("at"):
        try:
            datetime.datetime.fromisoformat(str(burst_raw["at"]))
        except ValueError:
            raise ConfigError("poll.burst.at 需要形如 2030-01-01T12:59:50") from None
        burst = {
            "at": str(burst_raw["at"]),
            "window_seconds": _number(burst_raw, "window_seconds", 120.0, "poll.burst.window_seconds"),
            "interval_seconds": _number(burst_raw, "interval_seconds", 0.5, "poll.burst.interval_seconds"),
            "jitter_seconds": _number(burst_raw, "jitter_seconds", 0.2, "poll.burst.jitter_seconds"),
        }
        if burst["interval_seconds"] < 0.3:
            warnings.append(
                "poll.burst.interval_seconds < 0.3，容易触发滑块；0.3~0.5 即可，再快瓶颈也在页面往返"
            )
        if burst["window_seconds"] < 10:
            warnings.append("poll.burst.window_seconds 太短，可能盖不住开售时刻")
    poll = PollConfig(
        interval_seconds=interval,
        jitter_seconds=jitter,
        plan_ttl_seconds=ttl,
        stop_at=stop_at,
        burst=burst,
    )

    notify_raw = raw.get("notify") or {}
    notify = NotifyConfig(
        console=bool(notify_raw.get("console", True)),
        desktop=bool(notify_raw.get("desktop", True)),
        sound=bool(notify_raw.get("sound", True)),
        dedupe_seconds=_number(notify_raw, "dedupe_seconds", 300.0, "notify.dedupe_seconds"),
        webhook=dict(NotifyConfig().webhook, **(notify_raw.get("webhook") or {})),
        email=dict(NotifyConfig().email, **(notify_raw.get("email") or {})),
    )
    if notify.webhook.get("enabled") and not notify.webhook.get("url"):
        raise ConfigError("notify.webhook.enabled=true 但没填 url")

    return Config(tasks=tasks, server=server, poll=poll, notify=notify, warnings=warnings, path=path)
