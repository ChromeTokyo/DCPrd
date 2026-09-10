"""SQLite 访问层：每请求一个连接、WAL、schema 初始化、通用查询辅助。"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    role              TEXT NOT NULL DEFAULT 'user',      -- super | admin | user
    tg_id             INTEGER UNIQUE,
    tg_username       TEXT,
    tg_first_name     TEXT,
    tg_last_name      TEXT,
    tg_photo_url      TEXT,
    invite_code       TEXT UNIQUE,
    invite_created_at TEXT,
    bound_at          TEXT,
    created_by        INTEGER,
    created_at        TEXT NOT NULL,
    deleted_at        TEXT,
    deleted_by        INTEGER
);

CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    csrf       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS requirements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project    TEXT NOT NULL,                              -- eb | im | tk
    kind       TEXT NOT NULL DEFAULT 'single',             -- single | compound
    name       TEXT NOT NULL,
    jira_keys  TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    share_code TEXT NOT NULL UNIQUE,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_by INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    deleted_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_req_updated ON requirements(updated_at);

CREATE TABLE IF NOT EXISTS requirement_owners (
    requirement_id INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    PRIMARY KEY (requirement_id, user_id)
);

CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    requirement_id INTEGER NOT NULL,
    name           TEXT NOT NULL,
    notes          TEXT NOT NULL DEFAULT '',
    share_code     TEXT NOT NULL UNIQUE,
    position       INTEGER NOT NULL DEFAULT 0,
    created_by     INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    deleted_at     TEXT,
    deleted_by     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_doc_req ON documents(requirement_id);

CREATE TABLE IF NOT EXISTS versions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id       INTEGER NOT NULL,
    number            INTEGER NOT NULL,
    kind              TEXT NOT NULL,                      -- html | zip
    original_filename TEXT NOT NULL,
    size_bytes        INTEGER NOT NULL DEFAULT 0,
    file_count        INTEGER NOT NULL DEFAULT 0,
    entry_path        TEXT,
    html_candidates   TEXT,
    note              TEXT NOT NULL DEFAULT '',
    source_version_id INTEGER,
    uploaded_by       INTEGER NOT NULL,
    uploaded_at       TEXT NOT NULL,
    deleted_at        TEXT,
    deleted_by        INTEGER,
    UNIQUE (document_id, number)
);

CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    color      TEXT NOT NULL DEFAULT 'gray',
    position   INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS requirement_tags (
    requirement_id INTEGER NOT NULL,
    tag_id         INTEGER NOT NULL,
    PRIMARY KEY (requirement_id, tag_id)
);

CREATE TABLE IF NOT EXISTS favorites (
    user_id        INTEGER NOT NULL,
    requirement_id INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (user_id, requirement_id)
);

CREATE TABLE IF NOT EXISTS recent_views (
    user_id        INTEGER NOT NULL,
    requirement_id INTEGER NOT NULL,
    viewed_at      TEXT NOT NULL,
    PRIMARY KEY (user_id, requirement_id)
);

CREATE TABLE IF NOT EXISTS comments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id    INTEGER NOT NULL,
    version_number INTEGER,
    author         TEXT NOT NULL,
    body           TEXT NOT NULL,
    ip             TEXT,
    created_at     TEXT NOT NULL,
    resolved_at    TEXT,
    resolved_by    INTEGER,
    deleted_at     TEXT,
    deleted_by     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_comments_doc ON comments(document_id, id);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    user_id     INTEGER,
    user_name   TEXT,
    action      TEXT NOT NULL,
    target_type TEXT,
    target_id   INTEGER,
    detail      TEXT
);
"""


def utcnow() -> str:
    """UTC ISO 字符串（秒精度，带 Z 不加，统一无时区后缀）。"""
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")


def parse_utc(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: Path, defaults: dict[str, str]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        # v2：2.0 数据表 + requirements 新列（幂等）
        from .v2.schema import REQUIREMENT_COLUMNS_V2, SCHEMA_V2
        conn.executescript(SCHEMA_V2)
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(requirements)")}
        for col, typ in REQUIREMENT_COLUMNS_V2.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE requirements ADD COLUMN {col} {typ}")
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    finally:
        conn.close()


# ---------- 通用辅助 ----------

def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, tuple(params)).fetchone()


def all_rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()


def insert(conn: sqlite3.Connection, table: str, data: dict[str, Any]) -> int:
    cols = ", ".join(data.keys())
    marks = ", ".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))
    return int(cur.lastrowid)


def update(conn: sqlite3.Connection, table: str, row_id: int, data: dict[str, Any]) -> None:
    sets = ", ".join(f"{k} = ?" for k in data)
    conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*data.values(), row_id))


# ---------- settings ----------

SETTING_KEYS = ("site_name", "jira_base_url", "max_upload_mb", "timezone", "sandbox_enabled", "public_widget_enabled")


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------- audit ----------

def audit(
    conn: sqlite3.Connection,
    user: sqlite3.Row | None,
    action: str,
    target_type: str | None = None,
    target_id: int | None = None,
    detail: Any = None,
) -> None:
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False)
    insert(
        conn,
        "audit_log",
        {
            "at": utcnow(),
            "user_id": user["id"] if user else None,
            "user_name": user["name"] if user else None,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "detail": detail,
        },
    )
