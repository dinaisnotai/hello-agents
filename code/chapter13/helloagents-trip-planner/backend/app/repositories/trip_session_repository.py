"""SQLite persistence for trip conversations and immutable plan versions."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional
from uuid import uuid4

from ..models.conversation import (
    ConversationMessage,
    PlanVersionSummary,
    TripSessionDetail,
    TripSessionSummary,
)
from ..models.schemas import TripPlan, TripRequest


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_database_path() -> Path:
    configured = os.getenv("TRIP_SESSION_DB_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "data" / "trip_sessions.db"


class TripSessionRepository:
    def __init__(self, database_path: str | Path | None = None):
        self.database_path = Path(database_path) if database_path else _default_database_path()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS trip_sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    city TEXT NOT NULL,
                    current_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trip_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES trip_sessions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS trip_plan_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    request_json TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    change_summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, version),
                    FOREIGN KEY(session_id) REFERENCES trip_sessions(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_trip_messages_session
                    ON trip_messages(session_id, id);
                CREATE INDEX IF NOT EXISTS idx_trip_versions_session
                    ON trip_plan_versions(session_id, version);
                """
            )

    def create(
        self,
        request: TripRequest,
        plan: TripPlan,
        *,
        change_summary: str = "创建旅行计划",
    ) -> TripSessionDetail:
        session_id = uuid4().hex
        now = _utc_now()
        title = f"{request.city}{request.travel_days}日旅行"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO trip_sessions
                    (id, title, city, current_version, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (session_id, title, request.city, now, now),
            )
            connection.execute(
                """
                INSERT INTO trip_plan_versions
                    (session_id, version, request_json, plan_json, change_summary, created_at)
                VALUES (?, 1, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    request.model_dump_json(),
                    plan.model_dump_json(),
                    change_summary,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO trip_messages (session_id, role, content, created_at)
                VALUES (?, 'assistant', ?, ?)
                """,
                (session_id, f"已创建{request.city}旅行计划，你可以继续调整预算、节奏或必去景点。", now),
            )
        detail = self.get(session_id)
        if detail is None:
            raise RuntimeError("Session was not persisted")
        return detail

    def add_version(
        self,
        session_id: str,
        request: TripRequest,
        plan: TripPlan,
        change_summary: str,
    ) -> int:
        now = _utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT current_version FROM trip_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            version = int(row["current_version"]) + 1
            connection.execute(
                """
                INSERT INTO trip_plan_versions
                    (session_id, version, request_json, plan_json, change_summary, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    version,
                    request.model_dump_json(),
                    plan.model_dump_json(),
                    change_summary,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE trip_sessions
                SET city = ?, current_version = ?, updated_at = ?
                WHERE id = ?
                """,
                (request.city, version, now, session_id),
            )
        return version

    def add_message(self, session_id: str, role: str, content: str) -> None:
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM trip_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(session_id)
            connection.execute(
                """
                INSERT INTO trip_messages (session_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, role, content, _utc_now()),
            )

    def save_turn(
        self,
        session_id: str,
        *,
        user_message: str,
        assistant_message: str,
        request: TripRequest,
        plan: TripPlan,
        change_summary: str,
    ) -> int:
        """Atomically save both messages and the plan version they produced."""

        now = _utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT current_version FROM trip_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            version = int(row["current_version"]) + 1
            connection.execute(
                """
                INSERT INTO trip_plan_versions
                    (session_id, version, request_json, plan_json, change_summary, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    version,
                    request.model_dump_json(),
                    plan.model_dump_json(),
                    change_summary,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO trip_messages (session_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    (session_id, "user", user_message, now),
                    (session_id, "assistant", assistant_message, now),
                ),
            )
            connection.execute(
                """
                UPDATE trip_sessions
                SET city = ?, current_version = ?, updated_at = ?
                WHERE id = ?
                """,
                (request.city, version, now, session_id),
            )
        return version

    def list(self, limit: int = 50) -> List[TripSessionSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, city, current_version, created_at, updated_at
                FROM trip_sessions
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [TripSessionSummary(**dict(row)) for row in rows]

    def get(self, session_id: str) -> Optional[TripSessionDetail]:
        with self._connect() as connection:
            session = connection.execute(
                """
                SELECT id, title, city, current_version, created_at, updated_at
                FROM trip_sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if session is None:
                return None

            current = connection.execute(
                """
                SELECT request_json, plan_json
                FROM trip_plan_versions
                WHERE session_id = ? AND version = ?
                """,
                (session_id, session["current_version"]),
            ).fetchone()
            messages = connection.execute(
                """
                SELECT id, role, content, created_at
                FROM trip_messages WHERE session_id = ? ORDER BY id
                """,
                (session_id,),
            ).fetchall()
            versions = connection.execute(
                """
                SELECT version, change_summary, created_at
                FROM trip_plan_versions
                WHERE session_id = ? ORDER BY version DESC
                """,
                (session_id,),
            ).fetchall()

        if current is None:
            return None
        return TripSessionDetail(
            **dict(session),
            request=TripRequest.model_validate(json.loads(current["request_json"])),
            plan=TripPlan.model_validate(json.loads(current["plan_json"])),
            messages=[ConversationMessage(**dict(row)) for row in messages],
            versions=[PlanVersionSummary(**dict(row)) for row in versions],
        )
