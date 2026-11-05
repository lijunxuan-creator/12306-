"""调度引擎：发放查询计划、接收浏览器回报、判命中、发告警、推送到看板。

设计要点
--------
12306 的余票查询接口返回的是加密数据，服务端不逆向它；
由**浏览器里的用户脚本**用页面自身的加密/渲染流程完成查询与解析，
把「车次 → 各席别余票文本」回报给本引擎，引擎只做判定与编排。
"""

from __future__ import annotations

import datetime
import logging
import queue
import random
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import criteria
from .config import Config, Task
from .models import LEVEL_AVAILABLE, Match, TrainRow
from .store import Store


@dataclass
class Plan:
    id: str
    task_id: str
    date: str
    created: float
    cycles: int = 1


class Engine:
    def __init__(
        self,
        config: Config,
        store: Store,
        notifier=None,
        clock=time.time,
        rng: Optional[random.Random] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self.store = store
        self.notifier = notifier
        self.log = logger or logging.getLogger("engine")
        self._now = clock
        self._rng = rng or random.Random()
        self._lock = threading.RLock()

        self._due: Dict[tuple, float] = {}
        self._inflight: Dict[tuple, str] = {}
        self._plans: Dict[str, Plan] = {}
        self._last: Dict[tuple, Dict[str, Any]] = {}
        self._subscribers: List["queue.Queue[Dict[str, Any]]"] = []
        self._debug: Optional[Dict[str, Any]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.started_at = self._now()

        self._stats: Dict[str, Any] = {
            "plans_issued": 0,
            "reports": 0,
            "failed_reports": 0,
            "alerts": 0,
            "cycles": 0,
            "last_activity": None,
        }

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._tick_loop, name="engine-tick", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def _tick_loop(self) -> None:
        last_status = 0.0
        while not self._stop.is_set():
            now = self._now()
            try:
                self._expire(now)
                if now - last_status >= 5:
                    last_status = now
                    self.broadcast("status", self.status_snapshot())
            except Exception:  # pragma: no cover - 后台线程不能死
                self.log.exception("tick 异常")
            self._stop.wait(1.0)

    # ------------------------------------------------------------ 计划发放
    def next_plan(
        self, client_id: str, force: bool = False, info: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        with self._lock:
            now = self._now()
            self._expire(now)

            stop_at = self._parse_stop_at()
            if stop_at is not None and now >= stop_at:
                return {"idle": True, "stopped": True, "reason": "已到配置的 stop_at 停止时间"}

            due_candidates: List[tuple] = []
            soonest: Optional[float] = None
            for task in self.config.enabled_tasks():
                for date in task.dates:
                    key = (task.id, date)
                    if key in self._inflight:
                        continue
                    due = self._due.get(key, 0.0)
                    if force or due <= now:
                        due_candidates.append((due, task, date))
                    else:
                        wait = due - now
                        soonest = wait if soonest is None else min(soonest, wait)

            if not due_candidates:
                return {
                    "idle": True,
                    "retry_after": round(max(0.5, soonest if soonest is not None else 2.0), 1),
                    "reason": "冷却中",
                }

            due_candidates.sort(key=lambda item: item[0])
            _, task, date = due_candidates[0]
            key = (task.id, date)
            plan_id = uuid.uuid4().hex[:16]
            previous = self._last.get(key)
            cycles = int(previous.get("cycle") or 1) + 1 if previous else 1

            plan = Plan(id=plan_id, task_id=task.id, date=date, created=now, cycles=cycles)
            self._plans[plan_id] = plan
            self._inflight[key] = plan_id
            interval, jitter = self._effective_interval(now)
            self._due[key] = now + interval + self._rng.uniform(0, jitter)
            self._stats["plans_issued"] += 1
            self._stats["last_activity"] = now

            if client_id:
                self.store.upsert_client(
                    client_id,
                    dict(info or {}, last_plan=plan_id, task_id=task.id, date=date),
                )

            return {
                "idle": False,
                "plan": {
                    "id": plan.id,
                    "task_id": task.id,
                    "date": date,
                    "route": task.route,
                    "from": task.from_station.to_dict(),
                    "to": task.to_station.to_dict(),
                    "seats": list(task.seats),
                    "trains": list(task.trains),
                    "min_count": task.min_count,
                    "allow_waitlist": task.allow_waitlist,
                    "auto_jump": task.auto_jump,
                    "passengers": list(task.passengers),
                    "note": task.note,
                    "cycle": cycles,
                    "ttl": self.config.poll.plan_ttl_seconds,
                },
            }

    # ------------------------------------------------------------ 回报处理
    def submit_report(self, plan_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {"ok": False, "error": "回报内容必须是 JSON 对象"}
        now = self._now()
        with self._lock:
            plan = self._plans.pop(plan_id, None)
            if plan is None:
                return {"ok": False, "error": "计划不存在或已过期，请重新领取"}
            key = (plan.task_id, plan.date)
            if self._inflight.get(key) == plan_id:
                self._inflight.pop(key, None)
            if now - plan.created > self.config.poll.plan_ttl_seconds:
                self._due[key] = now
                self.log.warning("计划 %s 回报太晚（%.0fs），已丢弃", plan_id, now - plan.created)
                return {"ok": False, "error": "计划已过期（浏览器端可能卡住了），已丢弃本次结果"}
            task = self.config.find_task(plan.task_id)
            if task is None:
                return {"ok": False, "error": "任务已被删除"}
            self._stats["reports"] += 1
            self._stats["last_activity"] = now

        accepted = bool(payload.get("ok", True))
        error = str(payload.get("error") or "")
        rows: List[TrainRow] = []
        raw_rows = payload.get("rows") or []
        if accepted:
            if not isinstance(raw_rows, list):
                accepted, error = False, "rows 字段不是数组"
            else:
                for item in raw_rows[:300]:
                    if not isinstance(item, dict):
                        continue
                    row = TrainRow.from_payload(item)
                    if row.train:
                        rows.append(row)
                if not rows:
                    accepted = False
                    error = error or "页面未解析出任何车次（可能无票、被限流或页面结构变化）"

        matches = criteria.evaluate(task, plan.date, rows) if rows else []

        fresh: List[Match] = []
        dedupe = float(self.config.notify.dedupe_seconds)
        for match in matches:
            if self.store.should_alert(match.key, dedupe, now=now):
                fresh.append(match)

        alert: Optional[Dict[str, Any]] = None
        if fresh:
            primary = fresh[0]
            message = "；".join(m.message() for m in fresh[:3])
            alert = {
                "ts": now,
                "key": "|".join(m.key for m in fresh),
                "task_id": task.id,
                "task_note": task.note,
                "route": task.route,
                "date": plan.date,
                "train": primary.train,
                "seat": primary.seat,
                "level": primary.level,
                "message": message,
                "extra": [m.message() for m in fresh[3:]],
                "matches": [m.to_dict() for m in fresh],
            }
            alert["id"] = self.store.record_alert(alert)
            with self._lock:
                self._stats["alerts"] += 1
            if self.notifier is not None:
                self.notifier.enqueue(alert)
            self.broadcast("alert", alert)

        if not accepted:
            with self._lock:
                self._stats["failed_reports"] += 1
                interval, _ = self._effective_interval(now)
                self._due[key] = now + max(1.0, interval / 2)

        actions: List[Dict[str, Any]] = []
        # 跳转只看「当前有没有票」，不受告警去重影响：
        # 否则命中一次后 3 分钟内即使还有票，也不会再把页面推到订单页。
        if task.auto_jump and matches:
            best = next((m for m in matches if m.level == LEVEL_AVAILABLE), None)
            if best is not None:
                actions.append(
                    {
                        "type": "jump_booking",
                        "task_id": task.id,
                        "date": plan.date,
                        "train": best.train,
                        "seat": best.seat,
                        "passengers": list(task.passengers),
                        "submit": bool(task.auto_submit),
                        "shortcut": "%s %s %s" % (plan.date, best.train, best.seat),
                    }
                )

        snapshot = {
            "ts": now,
            "task_id": task.id,
            "task_note": task.note,
            "route": task.route,
            "date": plan.date,
            "cycle": plan.cycles,
            "ok": accepted,
            "error": error,
            "rows": criteria.summarize(rows),
            "row_count": len(rows),
            "match_count": len(matches),
            "new_alerts": [m.to_dict() for m in fresh],
            "header": payload.get("header") if isinstance(payload.get("header"), list) else [],
            "warnings": payload.get("warnings") if isinstance(payload.get("warnings"), list) else [],
            "elapsed": round(now - plan.created, 1),
        }
        with self._lock:
            self._last[key] = snapshot
            self._stats["cycles"] += 1
        self.broadcast("report", snapshot)

        return {
            "ok": True,
            "accepted": accepted,
            "error": error,
            "matches": [m.to_dict() for m in matches[:20]],
            "new_alerts": [m.to_dict() for m in fresh[:20]],
            "actions": actions,
            "next_in": round(self._effective_interval(now)[0], 1),
        }

    # ------------------------------------------------------------ 内部工具
    def _effective_interval(self, now: float) -> tuple:
        """平时用 poll.interval_seconds，开售加速窗口内改用 burst 的间隔。"""
        base = self.config.poll
        burst = getattr(base, "burst", None)
        if burst:
            try:
                start = datetime.datetime.fromisoformat(str(burst["at"])).timestamp()
            except (ValueError, OSError, KeyError):
                start = None
            if start is not None and start <= now <= start + float(burst.get("window_seconds", 120)):
                return (
                    float(burst.get("interval_seconds", 0.5)),
                    float(burst.get("jitter_seconds", 0.2)),
                )
        return base.interval_seconds, base.jitter_seconds

    def _parse_stop_at(self) -> Optional[float]:
        raw = self.config.poll.stop_at
        if not raw:
            return None
        try:
            return datetime.datetime.fromisoformat(str(raw)).timestamp()
        except (ValueError, OSError):
            return None

    def _expire(self, now: float) -> None:
        ttl = self.config.poll.plan_ttl_seconds
        with self._lock:
            expired = [pid for pid, plan in self._plans.items() if now - plan.created > ttl]
            for plan_id in expired:
                plan = self._plans.pop(plan_id)
                key = (plan.task_id, plan.date)
                if self._inflight.get(key) == plan_id:
                    self._inflight.pop(key, None)
                    self._due[key] = now  # 立刻可重试
                self.log.warning("计划 %s 超时未回报（%s %s）", plan_id, plan.task_id, plan.date)

    # ------------------------------------------------------------ 订阅/广播
    def subscribe(self) -> "queue.Queue[Dict[str, Any]]":
        channel: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=200)
        with self._lock:
            self._subscribers.append(channel)
        return channel

    def unsubscribe(self, channel: "queue.Queue[Dict[str, Any]]") -> None:
        with self._lock:
            if channel in self._subscribers:
                self._subscribers.remove(channel)

    def broadcast(self, event: str, data: Dict[str, Any]) -> None:
        payload = {"event": event, "data": data}
        with self._lock:
            targets = list(self._subscribers)
        for channel in targets:
            try:
                channel.put_nowait(payload)
            except queue.Full:
                pass

    # ------------------------------------------------------------ 调试上报
    def record_debug(self, payload: Dict[str, Any]) -> None:
        """浏览器端遇到搞不清的页面结构时，把诊断信息扔上来，方便定位。"""
        with self._lock:
            self._debug = payload
        self.log.warning("收到浏览器诊断数据：%s", str(payload)[:200])

    # ------------------------------------------------------------ 状态快照
    def status_snapshot(self) -> Dict[str, Any]:
        now = self._now()
        clients = self.store.list_clients()
        online = [c for c in clients if c.get("online")]
        with self._lock:
            stats = dict(self._stats)
            inflight = len(self._inflight)
        stats["inflight"] = inflight
        stats["clients_online"] = len(online)
        return {
            "server_time": now,
            "started_at": self.started_at,
            "uptime_seconds": int(now - self.started_at),
            "stats": stats,
            "clients": clients,
            "stopped": self._is_stopped(now),
        }

    def _is_stopped(self, now: float) -> bool:
        stop_at = self._parse_stop_at()
        return stop_at is not None and now >= stop_at

    def state(self, alert_limit: int = 40) -> Dict[str, Any]:
        with self._lock:
            last = dict(self._last)
        tasks_out = []
        for task in self.config.tasks:
            item = task.to_dict()
            per_date = []
            for date in task.dates:
                snapshot = last.get((task.id, date))
                due = self._due.get((task.id, date))
                per_date.append(
                    {
                        "date": date,
                        "last": snapshot,
                        "next_in": round(max(0.0, (due or 0) - self._now()), 1) if due else 0.0,
                        "inflight": (task.id, date) in self._inflight,
                    }
                )
            item["dates_state"] = per_date
            item["last"] = per_date[-1]["last"] if per_date else None
            tasks_out.append(item)

        status = self.status_snapshot()
        status.update(
            {
                "config": self.config.summary(),
                "tasks": tasks_out,
                "alerts": self.store.list_alerts(limit=alert_limit),
                "running": self.running,
                "notify_channels": self.notifier.describe() if self.notifier else [],
                "debug": self._debug,
            }
        )
        return status
