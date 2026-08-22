"""색인 잡 러너 — 오래 걸리는 색인을 HTTP 요청 밖에서 돌린다.

Chroma는 SQLite 기반이라 여러 프로세스가 동시에 쓰면 위험하다. 그래서 외부 워커
(Celery/RQ)를 두지 않고 같은 프로세스의 단일 워커 스레드로 직렬 실행한다.
검색 요청은 그동안에도 정상 처리된다.

잡 상태는 SQLite에 남기므로 서버를 재시작해도 이력이 보인다.
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import corpora
import storage
from admin import audit

logger = logging.getLogger(__name__)

KIND_INCREMENTAL = "incremental"
KIND_REBUILD = "rebuild"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"

ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)

# 진행률을 DB에 쓰는 간격(문서 수). 매 문서마다 쓰면 색인이 느려진다.
_PROGRESS_STRIDE = 10

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


class JobError(RuntimeError):
    """잡을 시작할 수 없다."""


@dataclass(frozen=True)
class Job:
    id: int
    corpus_id: str
    kind: str
    status: str
    progress_current: int
    progress_total: int
    stats: dict
    error: str | None
    created_by: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def progress_percent(self) -> int:
        if self.progress_total <= 0:
            return 0
        return min(100, int(self.progress_current * 100 / self.progress_total))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_job(row) -> Job:
    try:
        stats = json.loads(row["stats_json"]) if row["stats_json"] else {}
    except json.JSONDecodeError:
        stats = {}
    return Job(
        id=row["id"],
        corpus_id=row["corpus_id"],
        kind=row["kind"],
        status=row["status"],
        progress_current=row["progress_current"],
        progress_total=row["progress_total"],
        stats=stats,
        error=row["error"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="index-job"
            )
        return _executor


# --- 조회 ---------------------------------------------------------------


def get(job_id: int) -> Job | None:
    with storage.cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = cur.fetchone()
    return _row_to_job(row) if row else None


def list_for_corpus(corpus_id: str, limit: int = 20) -> list[Job]:
    with storage.cursor() as cur:
        cur.execute(
            "SELECT * FROM jobs WHERE corpus_id = ? ORDER BY id DESC LIMIT ?",
            (corpus_id, limit),
        )
        return [_row_to_job(row) for row in cur.fetchall()]


def list_recent(limit: int = 20) -> list[Job]:
    with storage.cursor() as cur:
        cur.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))
        return [_row_to_job(row) for row in cur.fetchall()]


def active_job_for(corpus_id: str) -> Job | None:
    with storage.cursor() as cur:
        cur.execute(
            "SELECT * FROM jobs WHERE corpus_id = ? AND status IN (?, ?) "
            "ORDER BY id DESC LIMIT 1",
            (corpus_id, STATUS_QUEUED, STATUS_RUNNING),
        )
        row = cur.fetchone()
    return _row_to_job(row) if row else None


# --- 상태 전이 -----------------------------------------------------------


def _create_job(corpus_id: str, kind: str, created_by: str | None) -> int:
    with storage.transaction() as cur:
        cur.execute(
            "INSERT INTO jobs (corpus_id, kind, status, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (corpus_id, kind, STATUS_QUEUED, created_by, _now()),
        )
        return cur.lastrowid


def _mark_running(job_id: int) -> None:
    with storage.transaction() as cur:
        cur.execute(
            "UPDATE jobs SET status = ?, started_at = ? WHERE id = ?",
            (STATUS_RUNNING, _now(), job_id),
        )


def _mark_finished(
    job_id: int,
    status: str,
    stats: dict | None = None,
    error: str | None = None,
) -> None:
    with storage.transaction() as cur:
        cur.execute(
            "UPDATE jobs SET status = ?, stats_json = ?, error = ?, finished_at = ? "
            "WHERE id = ?",
            (
                status,
                json.dumps(stats, ensure_ascii=False) if stats else None,
                error,
                _now(),
                job_id,
            ),
        )


def _update_progress(job_id: int, current: int, total: int) -> None:
    with storage.transaction() as cur:
        cur.execute(
            "UPDATE jobs SET progress_current = ?, progress_total = ? WHERE id = ?",
            (current, total, job_id),
        )


def reset_interrupted() -> int:
    """서버 재시작으로 끊긴 잡을 정리한다. 앱 시작 시 호출한다."""
    with storage.transaction() as cur:
        cur.execute(
            "UPDATE jobs SET status = ?, error = ?, finished_at = ? "
            "WHERE status IN (?, ?)",
            (
                STATUS_INTERRUPTED,
                "서버가 재시작되어 중단되었습니다.",
                _now(),
                STATUS_QUEUED,
                STATUS_RUNNING,
            ),
        )
        n = cur.rowcount
    if n:
        logger.warning("reset_interrupted: 중단된 색인 잡 %d건 정리", n)
    return n


# --- 실행 ---------------------------------------------------------------


def _run_job(job_id: int, corpus_id: str, kind: str, actor: str | None) -> None:
    """워커 스레드 본체. 예외는 잡 상태로만 남기고 스레드를 죽이지 않는다."""
    from ingest.build_index import build_index
    from ingest.rebuild import rebuild_corpus

    _mark_running(job_id)
    last_written = -1

    def progress(current: int, total: int) -> None:
        nonlocal last_written
        if current - last_written >= _PROGRESS_STRIDE or current >= total:
            _update_progress(job_id, current, total)
            last_written = current

    try:
        # 잡이 큐에 있는 동안 설정이 바뀌었을 수 있으므로 최신 상태를 다시 읽는다.
        cfg = corpora.get(corpus_id)

        if kind == KIND_REBUILD:
            _, stats = rebuild_corpus(cfg, progress=progress)
        else:
            stats = build_index(cfg, progress=progress)

        _mark_finished(job_id, STATUS_SUCCEEDED, stats=stats)
        audit.record(
            actor, f"job.{kind}.succeeded", corpus_id, job_id=job_id, **stats
        )
        logger.info("job %d (%s/%s) 완료: %s", job_id, corpus_id, kind, stats)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        _mark_finished(job_id, STATUS_FAILED, error=message)
        audit.record(
            actor, f"job.{kind}.failed", corpus_id, job_id=job_id, error=message
        )
        logger.exception("job %d (%s/%s) 실패", job_id, corpus_id, kind)


def enqueue(corpus_id: str, kind: str, actor: str | None) -> Job:
    """색인 잡을 큐에 넣는다. 같은 corpus에 진행 중인 잡이 있으면 거부한다."""
    if kind not in (KIND_INCREMENTAL, KIND_REBUILD):
        raise JobError(f"알 수 없는 작업 종류입니다: {kind}")

    existing = active_job_for(corpus_id)
    if existing is not None:
        raise JobError(
            f"이미 진행 중인 색인 작업이 있습니다 (#{existing.id}). "
            "완료된 뒤 다시 시도하세요."
        )

    job_id = _create_job(corpus_id, kind, actor)
    audit.record(actor, f"job.{kind}.started", corpus_id, job_id=job_id)
    _get_executor().submit(_run_job, job_id, corpus_id, kind, actor)

    job = get(job_id)
    assert job is not None
    return job


def shutdown(wait: bool = False) -> None:
    """테스트·종료용. 워커 스레드를 정리한다."""
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=wait)
            _executor = None
