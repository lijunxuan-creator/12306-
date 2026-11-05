"""SQLite 存储：告警流水、去重窗口、客户端心跳。

单文件、无依赖，直接 sqlite3。所有方法线程安全（内部加锁）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    key      TEXT NOT NULL,
    task_id  TEXT,
    date     TEXT,
    train    TEXT,
    seat     TEXT,
    level    TEXT,
    message  TEXT,
    acked    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts DESC);

CREATE TABLE IF NOT EXISTS seen (
    key      TEXT PRIMARY KEY,
    first_ts REAL NOT NULL,
    last_ts  REAL NOT NULL,
    count    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    ts        REAL NOT NULL,
    info      TEXT
);
"""


class Store:
    def __init__(self, path: str) -> None:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ 去重
    def should_alert(self, key: str, cooldown: float, now: Optional[float] = None) -> bool:
        """同一命中每 cooldown 秒最多提醒一次。

        被抑制时只累加计数、不推进 last_ts，因此「一直有票」的场景会按固定节奏
        重复提醒，而不是被每次轮询无限期推迟。
        """
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT last_ts, count FROM seen WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO seen(key, first_ts, last_ts, count) VALUES(?,?,?,1)",
                    (key, now, now),
                )
                self._conn.commit()
                return True
            if (now - float(row["last_ts"])) >= cooldown:
                self._conn.execute(
                    "UPDATE seen SET last_ts = ?, count = count + 1 WHERE key = ?", (now, key)
                )
                self._conn.commit()
                return True
            self._conn.execute(
                "UPDATE seen SET count = count + 1 WHERE key = ?", (key,)
            )
            self._conn.commit()
            return False

    # ------------------------------------------------------------------ 告警
    def record_alert(self, alert: Dict[str, Any]) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO alerts(ts, key, task_id, date, train, seat, level, message) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    float(alert.get("ts") or time.time()),
                    str(alert.get("key") or ""),
                    alert.get("task_id"),
                    alert.get("date"),
                    alert.get("train"),
                    alert.get("seat"),
                    alert.get("level"),
                    alert.get("message"),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list_alerts(self, limit: int = 50, only_new: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM alerts"
        if only_new:
            sql += " WHERE acked = 0"
        sql += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (int(limit),)).fetchall()
        return [dict(row) for row in rows]

    def ack_alert(self, alert_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE alerts SET acked = 1 WHERE id = ?", (int(alert_id),)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def clear_alerts(self) -> int:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM alerts")
            self._conn.commit()
            return cursor.rowcount

    # ------------------------------------------------------------------ 客户端
    def upsert_client(self, client_id: str, info: Optional[Dict[str, Any]] = None) -> None:
        payload = json.dumps(info or {}, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO clients(client_id, ts, info) VALUES(?,?,?) "
                "ON CONFLICT(client_id) DO UPDATE SET ts = excluded.ts, info = excluded.info",
                (client_id, time.time(), payload),
            )
            self._conn.commit()

    def list_clients(self, stale_after: float = 90.0) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            rows = self._conn.execute("SELECT * FROM clients ORDER BY ts DESC").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["info"] = json.loads(item.get("info") or "{}")
            except json.JSONDecodeError:
                item["info"] = {}
            item["online"] = (now - float(item["ts"])) <= stale_after
            item["age"] = round(now - float(item["ts"]), 1)
            out.append(item)
        return out

    # ------------------------------------------------------------------ 维护
    def prune(self, days: float = 7.0, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        cutoff = now - days * 86400
        with self._lock:
            removed = self._conn.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,)).rowcount
            self._conn.execute("DELETE FROM seen WHERE last_ts < ?", (cutoff,))
            self._conn.execute("DELETE FROM clients WHERE ts < ?", (now - 7 * 86400,))
            self._conn.commit()
        return int(removed)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            alerts = self._conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"]
            new = self._conn.execute(
                "SELECT COUNT(*) AS n FROM alerts WHERE acked = 0"
            ).fetchone()["n"]
            seen = self._conn.execute("SELECT COUNT(*) AS n FROM seen").fetchone()["n"]
        return {"alerts": int(alerts), "unacked": int(new), "keys": int(seen)}
