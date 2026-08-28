"""SQLite 스키마와 커넥션 관리.

- 메모 원문은 memos 에 그대로 둔다(원본 불변).
- 파생물(청크/임베딩/태그/할일)은 언제든 재생성 가능한 캐시로 취급한다.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any, Iterable

from . import config

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    body        TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'web',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    pinned      INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    enriched_at TEXT,
    external_id TEXT            -- 외부 앱에서 가져온 메모의 원본 식별자
);
CREATE INDEX IF NOT EXISTS idx_memos_created ON memos(created_at DESC);
-- external_id 인덱스는 _migrate() 에서 만든다. 이 스크립트는 컬럼이 없는
-- 구버전 DB 에도 실행되므로, 여기서 참조하면 열리지도 않는다.

CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    memo_id    INTEGER NOT NULL REFERENCES memos(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    text       TEXT NOT NULL,
    model      TEXT,
    dim        INTEGER,
    embedding  BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_memo ON chunks(memo_id);
CREATE INDEX IF NOT EXISTS idx_chunks_model ON chunks(model);

-- rowid = memos.id 로 맞춰 쓴다. grams 는 한글 n-gram, raw 는 원문(영문/해시태그용).
CREATE VIRTUAL TABLE IF NOT EXISTS memo_fts USING fts5(grams, raw);

CREATE TABLE IF NOT EXISTS facets (
    memo_id INTEGER NOT NULL REFERENCES memos(id) ON DELETE CASCADE,
    kind    TEXT NOT NULL,          -- tag | person | link | date | decision | question
    value   TEXT NOT NULL,
    PRIMARY KEY (memo_id, kind, value)
);
CREATE INDEX IF NOT EXISTS idx_facets_kind ON facets(kind, value);

CREATE TABLE IF NOT EXISTS todos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    memo_id    INTEGER NOT NULL REFERENCES memos(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0,
    due        TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_todos_memo ON todos(memo_id);
CREATE INDEX IF NOT EXISTS idx_todos_done ON todos(done);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect() -> sqlite3.Connection:
    """스레드별 커넥션. FastAPI 워커/백그라운드 스레드에서 공유하면 안 된다."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.ensure_home()
        conn = sqlite3.connect(config.DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        _migrate(conn)
        _local.conn = conn
    return conn


# (컬럼명, 정의) — CREATE TABLE 보다 나중에 추가된 것들. 기존 DB 를 살려서 올린다.
_ADDED_COLUMNS = [("memos", "external_id", "TEXT")]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memos_external ON memos(source, external_id) "
        "WHERE external_id IS NOT NULL"
    )
    conn.commit()


def init() -> None:
    connect()


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return list(connect().execute(sql, tuple(params)))


def one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    conn = connect()
    cur = conn.execute(sql, tuple(params))
    conn.commit()
    return cur


def get_meta(key: str, default: str | None = None) -> str | None:
    row = one("SELECT value FROM meta WHERE key = ?", (key,))
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
