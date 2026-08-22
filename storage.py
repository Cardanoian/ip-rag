"""운영 SQLite 접근 계층 — corpora 정의, 관리자 계정, 색인 잡, 감사 로그.

Chroma가 벡터를 담는다면 이 DB는 "무엇을 어떻게 색인하는가"와 "누가 무엇을 했는가"를 담는다.
FastAPI가 요청을 스레드로 넘기므로 커넥션을 전역에 캐시하지 않고 호출마다 열고 닫는다.
SQLite는 로컬 파일 열기가 저렴하고, WAL 모드에서 읽기는 쓰기를 막지 않는다.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS corpora (
    id                     TEXT PRIMARY KEY,
    kind                   TEXT NOT NULL,
    label                  TEXT NOT NULL,
    corpus_id              TEXT NOT NULL,
    base_collection        TEXT NOT NULL UNIQUE,
    doc_prefix             TEXT NOT NULL,
    query_prefix           TEXT NOT NULL,
    embed_dim              INTEGER NOT NULL,
    chunk_size             INTEGER NOT NULL,
    chunk_overlap          INTEGER NOT NULL,
    single_chunk_char_hint INTEGER NOT NULL,
    active_collection      TEXT NOT NULL,
    index_version          TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'draft',
    is_seed                INTEGER NOT NULL DEFAULT 0,
    needs_rebuild          INTEGER NOT NULL DEFAULT 0,
    docs_dir_override      TEXT,
    created_at             TEXT NOT NULL,
    created_by             TEXT,
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    salt            TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('superadmin', 'admin')),
    is_active       INTEGER NOT NULL DEFAULT 1,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    created_by      TEXT,
    last_login_at   TEXT
);

-- 최고관리자 1명 불변식을 애플리케이션이 아니라 DB가 강제한다.
CREATE UNIQUE INDEX IF NOT EXISTS ux_single_superadmin
    ON admin_users(role) WHERE role = 'superadmin';

CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    corpus_id        TEXT NOT NULL,
    kind             TEXT NOT NULL,
    status           TEXT NOT NULL,
    progress_current INTEGER NOT NULL DEFAULT 0,
    progress_total   INTEGER NOT NULL DEFAULT 0,
    stats_json       TEXT,
    error            TEXT,
    created_by       TEXT,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT
);

CREATE INDEX IF NOT EXISTS ix_jobs_corpus ON jobs(corpus_id, id DESC);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT,
    action      TEXT NOT NULL,
    target      TEXT,
    detail_json TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_audit_created ON audit_log(id DESC);
"""

_initialized_paths: set[str] = set()


def db_path() -> Path:
    """설정된 DB 경로. 테스트가 config.APP_DB_PATH를 갈아끼울 수 있도록 매번 읽는다."""
    return Path(config.APP_DB_PATH)


def connect() -> sqlite3.Connection:
    """스키마가 준비된 커넥션을 연다. 호출자가 닫아야 한다."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")

    key = str(path.resolve())
    if key not in _initialized_paths:
        conn.executescript(SCHEMA)
        conn.commit()
        _initialized_paths.add(key)
    return conn


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    """읽기 전용 조회용. 커밋하지 않는다."""
    conn = connect()
    try:
        yield conn.cursor()
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Cursor]:
    """쓰기용. 예외가 나면 롤백한다.

    최고관리자 이양처럼 여러 UPDATE가 원자적으로 묶여야 하는 작업에 쓴다.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reset_cache() -> None:
    """테스트용: 스키마 초기화 여부 캐시를 비운다."""
    _initialized_paths.clear()
