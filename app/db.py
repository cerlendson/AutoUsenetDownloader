"""SQLite storage: schema, migrations and small helpers.

The web UI and the worker run as separate processes that share one database
file, so we use WAL mode and a busy timeout. Schema changes are applied with
numbered migrations tracked in PRAGMA user_version, which lets users upgrade
the container image without losing data.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE indexers (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        api_url       TEXT NOT NULL,
        api_key       TEXT NOT NULL DEFAULT '',
        enabled       INTEGER NOT NULL DEFAULT 1,
        priority      INTEGER NOT NULL DEFAULT 25,
        category_map  TEXT NOT NULL DEFAULT '{}',   -- {"audio.lossless": "3040,3045"}
        caps_json     TEXT NOT NULL DEFAULT '[]',   -- categories reported by the indexer
        last_test_at  TEXT,
        last_test_ok  INTEGER,
        last_test_msg TEXT,
        created_at    TEXT NOT NULL
    );

    CREATE TABLE profiles (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        enabled     INTEGER NOT NULL DEFAULT 1,
        categories  TEXT NOT NULL DEFAULT '[]',     -- ["audio", "audio.lossless"]
        keywords    TEXT NOT NULL DEFAULT '',       -- one per line; "quoted" = exact phrase
        indexer_ids TEXT NOT NULL DEFAULT '[]',     -- empty = all enabled indexers
        last_run_at TEXT,
        created_at  TEXT NOT NULL
    );

    -- Every release this app has sent to NZBGet. Used for duplicate prevention
    -- and for tracking download status.
    CREATE TABLE downloads (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id    INTEGER,
        profile_name  TEXT,
        indexer_id    INTEGER,
        indexer_name  TEXT,
        guid          TEXT,
        nzb_id        TEXT,
        title         TEXT NOT NULL,
        title_hash    TEXT NOT NULL,
        nzb_hash      TEXT,
        ext_hash      TEXT,
        category      TEXT,
        size          INTEGER,
        pub_date      TEXT,
        matched_term  TEXT,
        nzbget_id     INTEGER,
        status        TEXT NOT NULL DEFAULT 'queued',  -- queued|downloading|processing|completed|failed
        nzbget_status TEXT,
        queued_at     TEXT NOT NULL,
        completed_at  TEXT,
        source_dir    TEXT,
        move_state    TEXT,          -- pending|done|error|disabled
        moved_to      TEXT,
        message       TEXT
    );
    CREATE INDEX ix_dl_guid ON downloads(indexer_id, guid);
    CREATE INDEX ix_dl_nzbid ON downloads(indexer_id, nzb_id);
    CREATE INDEX ix_dl_title_hash ON downloads(title_hash);
    CREATE INDEX ix_dl_nzb_hash ON downloads(nzb_hash);
    CREATE INDEX ix_dl_status ON downloads(status);
    CREATE INDEX ix_dl_queued ON downloads(queued_at);

    CREATE TABLE activity (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts      TEXT NOT NULL,
        level   TEXT NOT NULL,
        message TEXT NOT NULL
    );
    CREATE INDEX ix_activity_ts ON activity(ts);
    """,
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "scan_interval_minutes": 30,
    "search_window_hours": 24,
    "monitor_interval_seconds": 60,
    "results_per_query": 100,
    # NZBGet connection
    "nzbget_host": "nzbget",
    "nzbget_port": 6789,
    "nzbget_username": "nzbget",
    "nzbget_password": "",
    "nzbget_ssl": False,
    "nzbget_category": "",
    "nzbget_priority": 0,
    # Completed download handling
    "move_completed": False,
    "target_dir": "/target",
    "target_subfolder_per_profile": True,
    "nzbget_path_prefix": "",   # path as NZBGet reports it, e.g. /downloads/completed
    "local_path_prefix": "",    # same folder as mounted in this container
    # Worker bookkeeping
    "last_scan_at": None,
    "last_scan_summary": "",
    "next_scan_at": None,
    "scan_requested": False,
    "worker_heartbeat": None,
    "last_monitor_at": None,
    "monitor_error": "",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Yield a connection inside a transaction (committed on success)."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db() -> None:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
            no_comments = "\n".join(line.split("--", 1)[0] for line in script.splitlines())
            for stmt in no_comments.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            conn.execute(f"PRAGMA user_version={i}")
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- settings

def get_settings() -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    out = dict(DEFAULT_SETTINGS)
    for row in rows:
        try:
            out[row["key"]] = json.loads(row["value"])
        except (TypeError, ValueError):
            out[row["key"]] = row["value"]
    return out


def get_setting(key: str) -> Any:
    return get_settings().get(key)


def set_settings(values: dict[str, Any]) -> None:
    with connect() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )


# ---------------------------------------------------------------- activity

def log_activity(message: str, level: str = "info") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO activity(ts, level, message) VALUES (?, ?, ?)",
            (iso(utcnow()), level, message[:2000]),
        )
        # keep the log bounded
        conn.execute(
            "DELETE FROM activity WHERE id <= (SELECT id FROM activity ORDER BY id DESC LIMIT 1 OFFSET 2000)"
        )


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]
