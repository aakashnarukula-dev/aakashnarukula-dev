"""SQLite persistence. Keeps state across restarts so pending retries are never lost."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import CallOutcome, Run, RunStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id    TEXT    NOT NULL,
    recipient_id   TEXT    NOT NULL,
    scheduled_for  TEXT    NOT NULL,
    status         TEXT    NOT NULL,
    attempt        INTEGER NOT NULL DEFAULT 0,
    token          TEXT    NOT NULL,
    next_action_at TEXT,
    last_outcome   TEXT,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    UNIQUE (schedule_id, scheduled_for)
);

CREATE TABLE IF NOT EXISTS attempts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    attempt_no       INTEGER NOT NULL,
    provider_call_id TEXT,
    outcome          TEXT    NOT NULL,
    detail           TEXT,
    started_at       TEXT    NOT NULL,
    ended_at         TEXT,
    UNIQUE (run_id, attempt_no)
);

CREATE INDEX IF NOT EXISTS idx_runs_status ON runs (status, next_action_at);
CREATE INDEX IF NOT EXISTS idx_attempts_call ON attempts (provider_call_id);
"""


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _row_to_run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        schedule_id=row["schedule_id"],
        recipient_id=row["recipient_id"],
        scheduled_for=_parse(row["scheduled_for"]),
        status=RunStatus(row["status"]),
        attempt=row["attempt"],
        token=row["token"],
        next_action_at=_parse(row["next_action_at"]),
        last_outcome=CallOutcome(row["last_outcome"]) if row["last_outcome"] else None,
        created_at=_parse(row["created_at"]),
        updated_at=_parse(row["updated_at"]),
    )


class Store:
    """Small synchronous data-access layer; safe to share across threads."""

    def __init__(self, path: str) -> None:
        self._path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ runs

    def create_run(
        self,
        *,
        schedule_id: str,
        recipient_id: str,
        scheduled_for: datetime,
        token: str,
        now: datetime,
    ) -> int | None:
        """Insert a run, or return None if this dose was already triggered.

        The UNIQUE(schedule_id, scheduled_for) constraint is what makes a duplicate
        cron fire — or a restart mid-minute — harmless.
        """
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO runs
                    (schedule_id, recipient_id, scheduled_for, status, attempt, token,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    schedule_id,
                    recipient_id,
                    _iso(scheduled_for),
                    RunStatus.CALLING.value,
                    token,
                    _iso(now),
                    _iso(now),
                ),
            )
            self._conn.commit()
            return cursor.lastrowid if cursor.rowcount else None

    def get_run(self, run_id: int) -> Run | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        return _row_to_run(row) if row else None

    def update_run(
        self,
        run_id: int,
        *,
        now: datetime,
        status: RunStatus | None = None,
        attempt: int | None = None,
        next_action_at: datetime | None = None,
        clear_next_action: bool = False,
        last_outcome: CallOutcome | None = None,
    ) -> None:
        sets: list[str] = ["updated_at = ?"]
        params: list[object] = [_iso(now)]
        if status is not None:
            sets.append("status = ?")
            params.append(status.value)
        if attempt is not None:
            sets.append("attempt = ?")
            params.append(attempt)
        if clear_next_action:
            sets.append("next_action_at = NULL")
        elif next_action_at is not None:
            sets.append("next_action_at = ?")
            params.append(_iso(next_action_at))
        if last_outcome is not None:
            sets.append("last_outcome = ?")
            params.append(last_outcome.value)
        params.append(run_id)

        with self._lock:
            self._conn.execute(
                f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", params
            )
            self._conn.commit()

    def in_flight_calls(self) -> list[tuple[Run, str]]:
        """(run, provider_call_id) for every call still awaiting an outcome."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT r.*, a.provider_call_id AS call_id
                  FROM runs r
                  JOIN attempts a ON a.run_id = r.id AND a.attempt_no = r.attempt
                 WHERE r.status = ?
                   AND a.provider_call_id IS NOT NULL
                   AND a.outcome = ?
                """,
                (RunStatus.CALLING.value, CallOutcome.IN_PROGRESS.value),
            ).fetchall()
        return [(_row_to_run(row), row["call_id"]) for row in rows]

    def recent_runs(self, limit: int = 25) -> list[Run]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_run(row) for row in rows]

    # -------------------------------------------------------------- attempts

    def start_attempt(self, run_id: int, attempt_no: int, now: datetime) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO attempts (run_id, attempt_no, outcome, started_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (run_id, attempt_no) DO UPDATE
                    SET outcome = excluded.outcome,
                        started_at = excluded.started_at,
                        ended_at = NULL,
                        detail = NULL
                """,
                (run_id, attempt_no, CallOutcome.IN_PROGRESS.value, _iso(now)),
            )
            self._conn.commit()

    def set_attempt_call_id(self, run_id: int, attempt_no: int, call_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE attempts SET provider_call_id = ? WHERE run_id = ? AND attempt_no = ?",
                (call_id, run_id, attempt_no),
            )
            self._conn.commit()

    def finish_attempt(
        self,
        run_id: int,
        attempt_no: int,
        outcome: CallOutcome,
        now: datetime,
        detail: str | None = None,
    ) -> bool:
        """Record a final outcome. Returns False if this attempt was already finished.

        This is the idempotency guard for webhooks: Twilio retries callbacks, and the
        gather + status callbacks can arrive in either order.
        """
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE attempts
                   SET outcome = ?, detail = ?, ended_at = ?
                 WHERE run_id = ? AND attempt_no = ? AND outcome = ?
                """,
                (
                    outcome.value,
                    detail,
                    _iso(now),
                    run_id,
                    attempt_no,
                    CallOutcome.IN_PROGRESS.value,
                ),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def attempts_for(self, run_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM attempts WHERE run_id = ? ORDER BY attempt_no", (run_id,)
            ).fetchall()

    def due_by_status(self, status: RunStatus, now: datetime) -> list[Run]:
        """Runs in ``status`` whose ``next_action_at`` timer has elapsed."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM runs
                 WHERE status = ? AND next_action_at IS NOT NULL AND next_action_at <= ?
                 ORDER BY next_action_at
                """,
                (status.value, _iso(now)),
            ).fetchall()
        return [_row_to_run(row) for row in rows]
