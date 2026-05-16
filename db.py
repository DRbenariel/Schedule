"""SQLite schema and all database queries."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS morning_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL UNIQUE,   -- YYYY-MM-DD
    ran_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_state (
    chat_id      INTEGER PRIMARY KEY,
    state_name   TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS morning_answers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    answer_date  TEXT NOT NULL,
    answer_text  TEXT NOT NULL,
    answerer_id  INTEGER NOT NULL,
    answered_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_assignment (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid         TEXT NOT NULL,
    title             TEXT NOT NULL,
    event_date        TEXT NOT NULL,            -- YYYY-MM-DD
    chat_id           INTEGER NOT NULL,
    asked_day_before  INTEGER NOT NULL DEFAULT 0,
    resolved          INTEGER NOT NULL DEFAULT 0,
    resolved_to       TEXT,                     -- 'טל' | 'בן' | 'שניהם' | NULL
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS caldav_write_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid   TEXT NOT NULL,
    event_title TEXT NOT NULL,
    start_time  TEXT,
    end_time    TEXT,
    is_all_day  INTEGER NOT NULL DEFAULT 0,
    written_by  INTEGER NOT NULL,
    written_at  TEXT NOT NULL
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ---------- conversation_state ----------

def save_state(conn: sqlite3.Connection, chat_id: int, state_name: str, payload: dict[str, Any]) -> None:
    now = datetime.utcnow().isoformat()
    conn.execute(
        """
        INSERT INTO conversation_state (chat_id, state_name, payload_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            state_name   = excluded.state_name,
            payload_json = excluded.payload_json,
            updated_at   = excluded.updated_at
        """,
        (chat_id, state_name, json.dumps(payload, ensure_ascii=False), now, now),
    )
    conn.commit()


def load_state(conn: sqlite3.Connection, chat_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT state_name, payload_json, updated_at FROM conversation_state WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "state_name": row["state_name"],
        "payload": json.loads(row["payload_json"]),
        "updated_at": row["updated_at"],
    }


def clear_state(conn: sqlite3.Connection, chat_id: int) -> None:
    conn.execute("DELETE FROM conversation_state WHERE chat_id = ?", (chat_id,))
    conn.commit()


# ---------- morning_answers ----------

def save_morning_answer(conn: sqlite3.Connection, answer_date: str, answer_text: str, answerer_id: int) -> None:
    conn.execute(
        """
        INSERT INTO morning_answers (answer_date, answer_text, answerer_id, answered_at)
        VALUES (?, ?, ?, ?)
        """,
        (answer_date, answer_text, answerer_id, datetime.utcnow().isoformat()),
    )
    conn.commit()


def get_morning_answer(conn: sqlite3.Connection, answer_date: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT answer_text, answerer_id, answered_at
        FROM morning_answers
        WHERE answer_date = ?
        ORDER BY id DESC LIMIT 1
        """,
        (answer_date,),
    ).fetchone()
    return dict(row) if row else None


# ---------- pending_assignment ----------

def add_pending_assignment(
    conn: sqlite3.Connection,
    event_uid: str,
    title: str,
    event_date: str,
    chat_id: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO pending_assignment (event_uid, title, event_date, chat_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (event_uid, title, event_date, chat_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_pending_for_date(
    conn: sqlite3.Connection, event_date: str, day_before: bool = False
) -> list[dict[str, Any]]:
    """Return unresolved pending assignments.

    If `day_before` is True, only those not yet asked day-before; otherwise all
    unresolved for `event_date`.
    """
    if day_before:
        rows = conn.execute(
            """
            SELECT id, event_uid, title, event_date, chat_id
            FROM pending_assignment
            WHERE event_date = ? AND resolved = 0 AND asked_day_before = 0
            """,
            (event_date,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, event_uid, title, event_date, chat_id
            FROM pending_assignment
            WHERE event_date = ? AND resolved = 0
            """,
            (event_date,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_pending_by_id(conn: sqlite3.Connection, row_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, event_uid, title, event_date, chat_id, resolved FROM pending_assignment WHERE id = ?",
        (row_id,),
    ).fetchone()
    return dict(row) if row else None


def mark_asked_day_before(conn: sqlite3.Connection, row_id: int) -> None:
    conn.execute(
        "UPDATE pending_assignment SET asked_day_before = 1 WHERE id = ?",
        (row_id,),
    )
    conn.commit()


def resolve_pending(conn: sqlite3.Connection, row_id: int, resolved_to: str) -> None:
    conn.execute(
        "UPDATE pending_assignment SET resolved = 1, resolved_to = ? WHERE id = ?",
        (resolved_to, row_id),
    )
    conn.commit()


# ---------- morning_log ----------

def log_morning_run(conn: sqlite3.Connection, run_date: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO morning_log (run_date, ran_at) VALUES (?, ?)",
        (run_date, datetime.utcnow().isoformat()),
    )
    conn.commit()


def morning_ran_today(conn: sqlite3.Connection, run_date: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM morning_log WHERE run_date = ?", (run_date,)
    ).fetchone()
    return row is not None


# ---------- caldav_write_log ----------

def log_caldav_write(
    conn: sqlite3.Connection,
    event_uid: str,
    event_title: str,
    start_time: str | None,
    end_time: str | None,
    is_all_day: bool,
    written_by: int,
) -> None:
    conn.execute(
        """
        INSERT INTO caldav_write_log
            (event_uid, event_title, start_time, end_time, is_all_day, written_by, written_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_uid,
            event_title,
            start_time,
            end_time,
            1 if is_all_day else 0,
            written_by,
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
