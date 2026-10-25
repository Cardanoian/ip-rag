"""승인 자료의 영속 작업·버전 전환. 외부 임베딩은 DB 트랜잭션 밖에서 실행한다."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, model_validator

import corpora
import storage
from api.auth import require_service_token
from api.services import Principal, failure

logger = logging.getLogger(__name__)
router = APIRouter()
_CORPUS_LOCKS = {}
_CORPUS_LOCKS_GUARD = threading.Lock()


def corpus_lock(corpus):
    # 현재 지원하는 단일 서버 프로세스에서 관리자 재색인과 API 버전 전환을 직렬화한다.
    with _CORPUS_LOCKS_GUARD:
        return _CORPUS_LOCKS.setdefault(corpus, threading.RLock())
SCHEMA = """
CREATE TABLE IF NOT EXISTS service_sources (
    corpus TEXT NOT NULL, source_id TEXT NOT NULL, service_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0, active_job TEXT,
    PRIMARY KEY(corpus, source_id)
);
CREATE TABLE IF NOT EXISTS source_jobs (
    id TEXT PRIMARY KEY, service_id TEXT NOT NULL, corpus TEXT NOT NULL,
    source_id TEXT NOT NULL, revision INTEGER NOT NULL, kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued', error TEXT, lease_until REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, finished_at REAL,
    UNIQUE(service_id, corpus, source_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS source_versions (
    corpus TEXT NOT NULL, source_id TEXT NOT NULL, version_id TEXT NOT NULL, content_hash TEXT NOT NULL,
    PRIMARY KEY(corpus, source_id, version_id)
);
CREATE INDEX IF NOT EXISTS ix_source_jobs_pending ON source_jobs(state, created_at);
"""


class Page(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=200000)


class Write(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, le=9223372036854775807)
    idempotency_key: str = Field(min_length=1, max_length=128)
    source_version_id: uuid.UUID
    title: str = Field(min_length=1, max_length=500)
    pages: list[Page] = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_pages(self):
        if len({p.page for p in self.pages}) != len(self.pages):
            raise ValueError("페이지 번호가 중복됩니다")
        if sum(len(p.text.encode()) for p in self.pages) > 5 * 1024 * 1024:
            raise ValueError("문서 텍스트가 5MB를 넘습니다")
        return self


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, le=9223372036854775807)
    idempotency_key: str = Field(min_length=1, max_length=128)
    enabled: bool = False


def resolve(principal, corpus):
    principal.authorize(corpus, "write")
    try:
        cfg = corpora.get(corpus)
    except corpora.CorpusNotFound:
        raise failure(404, "corpus_not_available", "자료집을 이용할 수 없습니다")
    if cfg.kind != "easyflow":
        raise failure(403, "operation_not_allowed", "서비스 색인은 easyflow 종류의 자료집만 지원합니다")
    return cfg


def public_job(row):
    payload = json.loads(row["payload_json"])
    return {"job_id": row["id"], "state": row["state"], "corpus": row["corpus"],
            "source_id": row["source_id"], "source_version_id": payload.get("source_version_id"),
            "revision": row["revision"], "error_code": row["error"]}


def submit(principal, corpus, source_id, kind, payload):
    resolve(principal, corpus)
    source_id = str(source_id)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    checksum = hashlib.sha256((kind + encoded).encode()).hexdigest()
    revision = payload["revision"]
    with storage.transaction() as cur:
        cur.execute("SELECT * FROM source_jobs WHERE service_id=? AND corpus=? AND source_id=? AND idempotency_key=?",
                    (principal.service_id, corpus, source_id, payload["idempotency_key"]))
        existing = cur.fetchone()
        if existing:
            if existing["payload_hash"] != checksum:
                raise failure(409, "idempotency_conflict", "같은 요청 키의 내용이 다릅니다")
            return public_job(existing)
        cur.execute("SELECT * FROM service_sources WHERE corpus=? AND source_id=?", (corpus, source_id))
        source = cur.fetchone()
        if source and source["service_id"] != principal.service_id:
            raise failure(404, "source_not_available", "자료를 이용할 수 없습니다")
        if source and revision <= source["revision"]:
            raise failure(409, "stale_revision", "더 최신의 자료 요청이 있습니다")
        cur.execute("SELECT COUNT(*) AS n FROM source_jobs WHERE service_id=? AND state IN ('queued','running')", (principal.service_id,))
        if cur.fetchone()["n"] >= 100:
            raise failure(429, "rate_limited", "색인 대기 한도를 초과했습니다", {"Retry-After": "30"})
        if kind == "index":
            version_hash = hashlib.sha256(json.dumps({"title": payload["title"], "pages": payload["pages"]}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            cur.execute("INSERT OR IGNORE INTO source_versions VALUES (?,?,?,?)", (corpus, source_id, payload["source_version_id"], version_hash))
            cur.execute("SELECT content_hash FROM source_versions WHERE corpus=? AND source_id=? AND version_id=?", (corpus, source_id, payload["source_version_id"]))
            if cur.fetchone()["content_hash"] != version_hash:
                raise failure(409, "version_conflict", "같은 문서 버전의 내용이 다릅니다")
        if not source:
            cur.execute("INSERT INTO service_sources(corpus,source_id,service_id) VALUES (?,?,?)", (corpus, source_id, principal.service_id))
        job_id = str(uuid.uuid4())
        cur.execute("""INSERT INTO source_jobs
            (id,service_id,corpus,source_id,revision,kind,idempotency_key,payload_hash,payload_json,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (job_id, principal.service_id, corpus, source_id, revision, kind,
                                            payload["idempotency_key"], checksum, encoded, time.time()))
        if kind == "index":
            # 새 버전의 준비 중에는 기존 정상 버전을 유지한다.
            cur.execute("UPDATE service_sources SET revision=? WHERE corpus=? AND source_id=?", (revision, corpus, source_id))
        else:
            # 검색 제외는 실제 청크 정리를 기다리지 않는다.
            enabled = bool(payload.get("enabled")) and kind == "state"
            if enabled and (not source or not source["active_job"] or source["deleted"]):
                raise failure(409, "source_not_ready", "다시 색인해야 하는 자료입니다")
            cur.execute("UPDATE service_sources SET revision=?,enabled=?,deleted=? WHERE corpus=? AND source_id=?",
                        (revision, int(enabled), int(kind == "delete"), corpus, source_id))
        cur.execute("SELECT * FROM source_jobs WHERE id=?", (job_id,))
        result = public_job(cur.fetchone())
        cur.execute("INSERT INTO audit_log(actor,action,target,detail_json,created_at) VALUES (?,?,?,?,?)",
                    (f"service:{principal.service_id}", f"source.{kind}.queued", corpus, json.dumps({"source_id": source_id, "job_id": job_id}), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return result


@router.put("/v1/corpora/{corpus}/sources/{source_id}", status_code=202)
def put_source(corpus: str, source_id: uuid.UUID, body: Write, principal: Principal = Depends(require_service_token)):
    return submit(principal, corpus, source_id, "index", body.model_dump(mode="json"))


@router.patch("/v1/corpora/{corpus}/sources/{source_id}", status_code=202)
def change_source(corpus: str, source_id: uuid.UUID, body: Change, principal: Principal = Depends(require_service_token)):
    return submit(principal, corpus, source_id, "state", body.model_dump(mode="json"))


@router.delete("/v1/corpora/{corpus}/sources/{source_id}", status_code=202)
def delete_source(corpus: str, source_id: uuid.UUID, body: Change, principal: Principal = Depends(require_service_token)):
    return submit(principal, corpus, source_id, "delete", body.model_dump(mode="json"))


@router.get("/v1/jobs/{job_id}")
def job_status(job_id: str, principal: Principal = Depends(require_service_token)):
    with storage.cursor() as cur:
        cur.execute("SELECT * FROM source_jobs WHERE id=? AND service_id=?", (job_id, principal.service_id))
        row = cur.fetchone()
    if row is None:
        raise failure(404, "job_not_available", "작업을 이용할 수 없습니다")
    principal.authorize(row["corpus"], "write")
    return public_job(row)


def active_jobs(corpus):
    with storage.cursor() as cur:
        cur.execute("SELECT active_job FROM service_sources WHERE corpus=? AND enabled=1 AND deleted=0 AND active_job IS NOT NULL", (corpus,))
        return [row["active_job"] for row in cur.fetchall()]


def _index(cfg, job, payload, collection_name=None):
    from ingest.chunker import chunk_document
    from ingest.embedder import embed_documents
    from ingest.store import get_collection
    from corpora.kinds import kind_of
    kind = kind_of(cfg)
    records = []
    for page in payload["pages"]:
        for chunk in chunk_document(page["text"], cfg, count_tokens=None):
            records.append((chunk, page["page"]))
    if not records:
        raise ValueError("empty_document")
    collection = get_collection(collection_name or cfg.active_collection)
    for offset in range(0, len(records), 50):
        batch = records[offset:offset + 50]
        metas = [{"title": payload["title"], "source_id": job["source_id"],
                  "source_version_id": payload["source_version_id"], "operation_id": job["id"],
                  "source_path": f"{cfg.id}/{job['source_id']}.md", "revision": job["revision"], "page": page,
                  "anchor": f"p.{page}", "chunk_index": offset + i, "n_chunks": len(records),
                  "content_hash": job["payload_hash"]} for i, (_, page) in enumerate(batch)]
        embeddings = embed_documents([kind.embed_text({"title": payload["title"]}, text) for text, _ in batch], cfg)
        collection.upsert(ids=[f"{job['id']}::{offset + i}" for i in range(len(batch))],
                          embeddings=embeddings, documents=[text for text, _ in batch], metadatas=metas)
        with storage.transaction() as cur:
            cur.execute("UPDATE source_jobs SET lease_until=? WHERE id=? AND state='running'", (time.time() + 300, job["id"]))
    return collection


def process_next():
    now = time.time()
    with storage.transaction() as cur:
        # BEGIN IMMEDIATE로 다른 프로세스와도 한 작업을 한 번만 선점한다.
        cur.execute("SELECT * FROM source_jobs WHERE state='queued' OR (state='running' AND lease_until<?) ORDER BY created_at LIMIT 1", (now,))
        row = cur.fetchone()
        if row is None:
            return False
        job = dict(row)
        cur.execute("UPDATE source_jobs SET state='running',lease_until=? WHERE id=?", (now + 300, job["id"]))
    with corpus_lock(job["corpus"]):
        payload = json.loads(job["payload_json"])
        collection = None
        committed = False
        try:
            cfg = corpora.get(job["corpus"])
            if job["kind"] == "index":
                collection = _index(cfg, job, payload)
            with storage.transaction() as cur:
                cur.execute("SELECT * FROM service_sources WHERE corpus=? AND source_id=?", (job["corpus"], job["source_id"]))
                current = cur.fetchone()
                fresh = current is not None and current["revision"] == job["revision"]
                if fresh and job["kind"] == "index":
                    # 원본은 전체 재색인·복구에서도 같은 출처를 재현할 수 있게 남긴다.
                    cfg.docs_dir().mkdir(parents=True, exist_ok=True)
                    path = cfg.docs_dir() / f"{job['source_id']}.md"
                    path.write_text("\n\n".join(f"## p.{p['page']}\n\n{p['text']}" for p in payload["pages"]), encoding="utf-8")
                    sidecar = dict(payload, source_id=job["source_id"], operation_id=job["id"])
                    path.with_suffix(".json").write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
                    cur.execute("UPDATE service_sources SET enabled=1,deleted=0,active_job=? WHERE corpus=? AND source_id=?",
                                (job["id"], job["corpus"], job["source_id"]))
                if fresh and job["kind"] == "delete":
                    for suffix in [".md", ".json"]:
                        (cfg.docs_dir() / f"{job['source_id']}{suffix}").unlink(missing_ok=True)
                state = "succeeded" if fresh else "superseded"
                cur.execute("UPDATE source_jobs SET state=?,finished_at=?,error=NULL,lease_until=0 WHERE id=?", (state, time.time(), job["id"]))
                cur.execute("INSERT INTO audit_log(actor,action,target,detail_json,created_at) VALUES (?,?,?,?,?)",
                            (f"service:{job['service_id']}", f"source.{job['kind']}.{state}", job["corpus"],
                             json.dumps({"source_id": job["source_id"], "job_id": job["id"]}), datetime.now(timezone.utc).isoformat(timespec="seconds")))
            committed = True
            # 원자적인 활성 포인터 전환 이후에만 옛 청크를 정리한다.
            from ingest.store import get_collection
            collection = collection or get_collection(cfg.active_collection)
            if job["kind"] == "index":
                if not fresh:
                    collection.delete(where={"operation_id": job["id"]})
                else:
                    collection.delete(where={"$and": [{"source_id": job["source_id"]}, {"revision": {"$lt": job["revision"]}}]})
            elif job["kind"] == "delete" and fresh:
                collection.delete(where={"$and": [{"source_id": job["source_id"]}, {"revision": {"$lte": job["revision"]}}]})
        except Exception as error:
            if committed and job["kind"] == "index":
                logger.warning("old_chunk_cleanup_failed job=%s error=%s", job["id"], type(error).__name__)
                return True
            # 예외 원문에는 본문·경로·외부 키가 포함될 수 있어 저장하지 않는다.
            with storage.transaction() as cur:
                cur.execute("UPDATE source_jobs SET state='failed',finished_at=?,error=?,lease_until=0 WHERE id=?",
                            (time.time(), type(error).__name__, job["id"]))
            if collection is not None and job["kind"] == "index":
                with storage.cursor() as cur:
                    cur.execute("SELECT active_job FROM service_sources WHERE corpus=? AND source_id=?", (job["corpus"], job["source_id"]))
                    source = cur.fetchone()
                if source is None or source["active_job"] != job["id"]:
                    collection.delete(where={"operation_id": job["id"]})
            logger.warning("service_index_failed job=%s error=%s", job["id"], type(error).__name__)
        return True


async def worker(stop):
    while not stop.is_set():
        try:
            processed = await asyncio.to_thread(process_next)
        except Exception as error:
            logger.warning("service_worker_failed error=%s", type(error).__name__)
            processed = False
        if not processed:
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass


def _rebuild_managed(cfg, target_collection=None, progress=None, reset=False, limit=None):
    """승인된 활성 버전의 영속 payload로 페이지·출처를 복원한다."""
    from ingest.store import get_collection
    collection = get_collection(target_collection or cfg.active_collection, reset=reset)
    with storage.cursor() as cur:
        cur.execute("""SELECT j.* FROM source_jobs j JOIN service_sources s ON j.id=s.active_job
            WHERE s.corpus=? AND s.deleted=0""", (cfg.id,))
        jobs = [dict(row) for row in cur.fetchall()]
    if limit is not None:
        jobs = jobs[:limit]
    total = 0
    for index, job in enumerate(jobs):
        _index(cfg, job, json.loads(job["payload_json"]), target_collection)
        if progress:
            progress(index + 1, len(jobs))
    total = collection.count()
    return {"indexed_docs": len(jobs), "skipped_docs": 0, "failed_docs": 0,
            "total_chunks": total, "embedded_chunks": total, "reused_docs": 0, "lfs_pointer_docs": 0}


def rebuild_managed(cfg, target_collection=None, progress=None, reset=False, limit=None):
    with corpus_lock(cfg.id):
        return _rebuild_managed(corpora.get(cfg.id), target_collection, progress, reset, limit)
