"""서비스 토큰·corpus 권한. 비밀 원문은 저장하지 않는다."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException

import storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS service_tokens (
    token_id TEXT PRIMARY KEY, service_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
    grants_json TEXT NOT NULL, requests_per_minute INTEGER NOT NULL DEFAULT 120,
    active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS service_usage (
    service_id TEXT NOT NULL, bucket INTEGER NOT NULL, requests INTEGER NOT NULL,
    PRIMARY KEY(service_id, bucket)
);
"""


def failure(status, code, message, headers=None):
    return HTTPException(status, detail={"code": code, "message": message}, headers=headers)


def register(service_id, token_id, token, grants, requests_per_minute=120):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", service_id):
        raise ValueError("서비스 ID 형식이 올바르지 않습니다")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", token_id):
        raise ValueError("토큰 ID 형식이 올바르지 않습니다")
    if len(token) < 32 or token != token.strip():
        raise ValueError("토큰은 앞뒤 공백 없는 32자 이상 값이어야 합니다")
    if not grants or requests_per_minute < 1:
        raise ValueError("허용 corpus와 양수 요청 한도가 필요합니다")
    from corpora.models import validate_slug
    normalized = {}
    for corpus, operations in grants.items():
        corpus = validate_slug(corpus)
        if not operations or set(operations) - {"search", "write"}:
            raise ValueError("허용 작업은 search 또는 write입니다")
        import corpora
        protected = corpora.get(corpus).kind == "easyflow"
        if protected != (service_id == "easyflow"):
            raise ValueError("EasyFlow 전용 자료집과 다른 서비스의 자료를 혼용할 수 없습니다")
        normalized[corpus] = sorted(set(operations))
    with storage.transaction() as cur:
        cur.execute("""INSERT INTO service_tokens
            (token_id, service_id, token_hash, grants_json, requests_per_minute, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(token_id) DO UPDATE SET service_id=excluded.service_id,
            token_hash=excluded.token_hash, grants_json=excluded.grants_json,
            requests_per_minute=excluded.requests_per_minute, active=1""",
            (token_id, service_id, hashlib.sha256(token.encode()).hexdigest(),
             json.dumps(normalized), requests_per_minute, time.time()))


@dataclass(frozen=True)
class Principal:
    service_id: str
    token_id: str
    grants: dict
    requests_per_minute: int = 120

    def allows(self, corpus, operation="search"):
        import corpora
        try:
            protected = corpora.get(corpus).kind == "easyflow"
        except corpora.CorpusNotFound:
            return False
        # 앱 간 자료 혼용 금지. 운영자가 잘못 부여한 grant도 이 경계를 넘지 못한다.
        if protected != (self.service_id == "easyflow"):
            return False
        return operation in self.grants.get(corpus, []) or (
            self.service_id == "development" and operation == "search")

    def authorize(self, corpus, operation="search"):
        if not self.allows(corpus, "search") and not self.allows(corpus, "write"):
            raise failure(404, "corpus_not_available", "자료집을 이용할 수 없습니다")
        if not self.allows(corpus, operation):
            raise failure(403, "operation_not_allowed", "허용되지 않은 작업입니다")


def authenticate(authorization):
    supplied = ""
    scheme, separator, candidate = (authorization or "").partition(" ")
    if separator and scheme.lower() == "bearer":
        supplied = candidate
    digest = hashlib.sha256(supplied.encode()).hexdigest()
    with storage.cursor() as cur:
        cur.execute("SELECT * FROM service_tokens WHERE token_hash=? AND active=1", (digest,))
        row = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS n FROM service_tokens WHERE active=1")
        configured = cur.fetchone()["n"] > 0
    if row and supplied:
        return Principal(row["service_id"], row["token_id"], json.loads(row["grants_json"]), row["requests_per_minute"])
    legacy = os.getenv("RAG_API_TOKEN", "").strip()
    if legacy and secrets.compare_digest(supplied, legacy):
        corpus = os.getenv("RAG_LEGACY_CORPUS", "inventions").strip()
        return Principal("coai", "legacy", {corpus: ["search"]})
    production = os.getenv("APP_ENV", "development").lower() in {"prod", "production"}
    if not legacy and not configured and not production:
        return Principal("development", "development", {})
    if not legacy and not configured:
        raise failure(503, "service_not_ready", "서비스 인증이 구성되지 않았습니다")
    raise failure(401, "invalid_service_token", "유효한 서비스 토큰이 필요합니다", {"WWW-Authenticate": "Bearer"})


def check_rate(principal):
    if principal.service_id == "development":
        return
    now = time.time()
    bucket = int(now // 60)
    with storage.transaction() as cur:
        cur.execute("DELETE FROM service_usage WHERE bucket < ?", (bucket - 2,))
        cur.execute("""INSERT INTO service_usage VALUES (?, ?, 1)
            ON CONFLICT(service_id, bucket) DO UPDATE SET requests=requests+1""",
            (principal.service_id, bucket))
        cur.execute("SELECT requests FROM service_usage WHERE service_id=? AND bucket=?", (principal.service_id, bucket))
        exceeded = cur.fetchone()["requests"] > principal.requests_per_minute
    if exceeded:
        raise failure(429, "rate_limited", "서비스 요청 한도를 초과했습니다", {"Retry-After": str(max(1, 60 - int(now % 60)))})


def configured():
    if os.getenv("RAG_API_TOKEN", "").strip():
        return True
    with storage.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM service_tokens WHERE active=1")
        return cur.fetchone()["n"] > 0
