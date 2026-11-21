"""SQLite spend ledger for SolRouter. Stores prompt hashes only, never raw prompts."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class LedgerEntry:
    prompt_hash: str
    category: str
    model_id: str | None
    cost_usd: float
    cost_is_estimated: bool
    latency_ms: int
    success: bool
    baseline_cost_usd: float
    attempts: int
    error: str | None = None
    ts: str | None = None


def hash_prompt(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    category TEXT NOT NULL,
    model_id TEXT,
    cost_usd REAL NOT NULL,
    cost_is_estimated INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    success INTEGER NOT NULL,
    baseline_cost_usd REAL NOT NULL,
    attempts INTEGER NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts);
"""


def _r(x: float) -> float:
    return round(float(x or 0.0), 6)


class Ledger:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def record(self, entry: LedgerEntry) -> int:
        ts = entry.ts or datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO calls (ts, prompt_hash, category, model_id, cost_usd,"
                " cost_is_estimated, latency_ms, success, baseline_cost_usd, attempts, error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts, entry.prompt_hash, entry.category, entry.model_id,
                 float(entry.cost_usd), int(entry.cost_is_estimated), int(entry.latency_ms),
                 int(entry.success), float(entry.baseline_cost_usd), int(entry.attempts),
                 entry.error),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def _spent_today(self, now: datetime | None) -> float:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM calls WHERE substr(ts, 1, 10) = ?",
            (day,),
        ).fetchone()
        return _r(row[0])

    def spent_today_usd(self, now: datetime | None = None) -> float:
        with self._lock:
            return self._spent_today(now)

    def totals(self) -> dict:
        with self._lock:
            spent, requests, successes, estimated = self._conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0), COUNT(*), COALESCE(SUM(success),0),"
                " COALESCE(SUM(cost_is_estimated),0) FROM calls"
            ).fetchone()
            base_ok, spent_ok = self._conn.execute(
                "SELECT COALESCE(SUM(baseline_cost_usd),0), COALESCE(SUM(cost_usd),0)"
                " FROM calls WHERE success = 1"
            ).fetchone()
            rows = self._conn.execute(
                "SELECT model_id, COUNT(*), COALESCE(SUM(cost_usd),0) FROM calls"
                " WHERE success = 1 AND model_id IS NOT NULL GROUP BY model_id"
            ).fetchall()
            today = self._spent_today(None)
        saved = base_ok - spent_ok
        return {
            "total_spent_usd": _r(spent),
            "total_baseline_usd": _r(base_ok),
            "total_saved_usd": _r(saved),
            "savings_percent": round(saved / base_ok * 100, 2) if base_ok > 0 else 0.0,
            "requests": requests,
            "successes": int(successes),
            "failures": requests - int(successes),
            "estimated_cost_requests": int(estimated),
            "per_model": {m: {"calls": n, "spent_usd": _r(s)} for m, n, s in rows},
            "spent_today_usd": today,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
