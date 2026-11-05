"""命中判定：把一次查询结果按任务条件筛选、排序。

纯函数，方便单测：evaluate(task, date, rows) -> [Match]
"""

from __future__ import annotations

__all__ = ["evaluate", "seat_match", "best_match", "summarize"]

from typing import Dict, Iterable, List

from .models import (
    LEVEL_AVAILABLE,
    LEVEL_WAITLIST,
    STATUS_AVAILABLE,
    STATUS_WAITLIST,
    Match,
    TrainRow,
    normalize_train,
)

_LEVEL_RANK = {LEVEL_AVAILABLE: 0, LEVEL_WAITLIST: 1}


def seat_match(
    info, min_count: int = 1, allow_waitlist: bool = False
) -> str:
    """返回命中等级；不命中返回空串。"""
    if info is None:
        return ""
    if info.status == STATUS_AVAILABLE:
        if min_count <= 1 or info.count is None:
            return LEVEL_AVAILABLE
        return LEVEL_AVAILABLE if info.count >= min_count else ""
    if info.status == STATUS_WAITLIST and allow_waitlist:
        return LEVEL_WAITLIST
    return ""


def evaluate(task, date: str, rows: Iterable[TrainRow]) -> List[Match]:
    """按任务条件筛选并排序（有票优先、车次优先级、发车时间、席别顺序）。"""
    wanted: Dict[str, int] = {normalize_train(t): i for i, t in enumerate(task.trains)}
    seat_order = {name: i for i, name in enumerate(task.seats)}
    matches: List[Match] = []

    for row in rows:
        train_no = normalize_train(row.train)
        if not train_no:
            continue
        if wanted and train_no not in wanted:
            continue
        for seat_name in task.seats:
            info = row.seats.get(seat_name)
            level = seat_match(info, task.min_count, task.allow_waitlist)
            if not level:
                continue
            matches.append(
                Match(
                    task_id=task.id,
                    date=date,
                    train=row.train,
                    seat=seat_name,
                    info=info,
                    level=level,
                    dep=row.dep,
                    task_note=getattr(task, "note", ""),
                )
            )

    matches.sort(
        key=lambda m: (
            _LEVEL_RANK.get(m.level, 9),
            wanted.get(normalize_train(m.train), 999),
            m.dep or "99:99",
            seat_order.get(m.seat, 999),
        )
    )
    return matches


def best_match(matches: List[Match]) -> Match | None:
    return matches[0] if matches else None


def summarize(rows: Iterable[TrainRow], limit: int = 40) -> List[dict]:
    """给看板用的简要快照。"""
    out = []
    for row in rows:
        if not normalize_train(row.train):
            continue
        hits = {
            name: info.text
            for name, info in row.seats.items()
            if info.status in (STATUS_AVAILABLE, STATUS_WAITLIST)
        }
        out.append(
            {
                "train": row.train,
                "dep": row.dep,
                "arr": row.arr,
                "duration": row.duration,
                "hits": hits,
            }
        )
        if len(out) >= limit:
            break
    return out
