"""색인 잡 러너 — 중복 차단, 진행률, 재시작 복구."""
from __future__ import annotations

import time

import pytest

import corpora
from admin import jobs
from ingest.store import get_collection

BODY = "규정 본문입니다. 충분히 길게 작성합니다.\n" * 5


@pytest.fixture(autouse=True)
def clean_executor():
    yield
    jobs.shutdown(wait=True)


def _write_doc(cfg, name: str):
    path = cfg.docs_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BODY, encoding="utf-8")
    return path


def _wait_for(job_id: int, timeout: float = 10.0) -> jobs.Job:
    """잡이 끝날 때까지 기다린다."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobs.get(job_id)
        assert job is not None
        if not job.is_active:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


# ---------------------------------------------------------------------------
# 중복 차단
# ---------------------------------------------------------------------------

def test_duplicate_job_for_same_corpus_is_refused(plain_corpus):
    jobs._create_job(plain_corpus.id, jobs.KIND_INCREMENTAL, "tester")

    with pytest.raises(jobs.JobError, match="진행 중"):
        jobs.enqueue(plain_corpus.id, jobs.KIND_INCREMENTAL, "tester")


def test_different_corpora_can_queue_independently(plain_corpus, seed_corpus):
    jobs._create_job(plain_corpus.id, jobs.KIND_INCREMENTAL, "tester")

    # 다른 corpus는 막히지 않는다.
    job = jobs.enqueue(seed_corpus.id, jobs.KIND_INCREMENTAL, "tester")
    assert job.corpus_id == seed_corpus.id
    _wait_for(job.id)


def test_unknown_job_kind_is_refused(plain_corpus):
    with pytest.raises(jobs.JobError):
        jobs.enqueue(plain_corpus.id, "nonsense", "tester")


def test_finished_job_does_not_block_next(plain_corpus, patch_embed):
    _write_doc(plain_corpus, "규정.md")

    first = jobs.enqueue(plain_corpus.id, jobs.KIND_INCREMENTAL, "tester")
    _wait_for(first.id)

    second = jobs.enqueue(plain_corpus.id, jobs.KIND_INCREMENTAL, "tester")
    assert _wait_for(second.id).status == jobs.STATUS_SUCCEEDED


# ---------------------------------------------------------------------------
# 실행 결과
# ---------------------------------------------------------------------------

def test_incremental_job_indexes_documents(plain_corpus, patch_embed):
    _write_doc(plain_corpus, "규정 1.md")
    _write_doc(plain_corpus, "규정 2.md")

    job = jobs.enqueue(plain_corpus.id, jobs.KIND_INCREMENTAL, "tester")
    finished = _wait_for(job.id)

    assert finished.status == jobs.STATUS_SUCCEEDED
    assert finished.stats["indexed_docs"] == 2
    assert get_collection(plain_corpus.active_collection).count() > 0


def test_progress_reaches_total(plain_corpus, patch_embed):
    for i in range(3):
        _write_doc(plain_corpus, f"규정 {i}.md")

    job = jobs.enqueue(plain_corpus.id, jobs.KIND_INCREMENTAL, "tester")
    finished = _wait_for(job.id)

    assert finished.progress_total == 3
    assert finished.progress_current == 3
    assert finished.progress_percent == 100


def test_rebuild_job_switches_collection(plain_corpus, patch_embed):
    _write_doc(plain_corpus, "규정.md")
    _wait_for(jobs.enqueue(plain_corpus.id, jobs.KIND_INCREMENTAL, "tester").id)

    job = jobs.enqueue(plain_corpus.id, jobs.KIND_REBUILD, "tester")
    finished = _wait_for(job.id)

    assert finished.status == jobs.STATUS_SUCCEEDED
    assert corpora.get(plain_corpus.id).active_collection == "rules_v2"


def test_failed_job_records_error(plain_corpus, monkeypatch):
    """실패는 잡 상태로 남고 워커 스레드를 죽이지 않는다."""
    import ingest.build_index as build_index_module

    _write_doc(plain_corpus, "규정.md")

    def explode(texts, cfg):
        raise RuntimeError("Gemini 장애")

    monkeypatch.setattr(build_index_module, "embed_documents", explode)

    job = jobs.enqueue(plain_corpus.id, jobs.KIND_INCREMENTAL, "tester")
    finished = _wait_for(job.id)

    assert finished.status == jobs.STATUS_FAILED
    assert "Gemini 장애" in finished.error


def test_worker_survives_failed_job(plain_corpus, monkeypatch, patch_embed):
    """한 잡이 실패해도 다음 잡은 정상 실행되어야 한다."""
    import ingest.build_index as build_index_module

    _write_doc(plain_corpus, "규정.md")

    def explode(texts, cfg):
        raise RuntimeError("일시적 장애")

    monkeypatch.setattr(build_index_module, "embed_documents", explode)
    failed = _wait_for(jobs.enqueue(plain_corpus.id, jobs.KIND_INCREMENTAL, "x").id)
    assert failed.status == jobs.STATUS_FAILED

    monkeypatch.setattr(
        build_index_module,
        "embed_documents",
        lambda texts, cfg: [[0.1] * cfg.embed_dim for _ in texts],
    )
    recovered = _wait_for(jobs.enqueue(plain_corpus.id, jobs.KIND_INCREMENTAL, "x").id)
    assert recovered.status == jobs.STATUS_SUCCEEDED


def test_job_reads_latest_config(plain_corpus, patch_embed):
    """큐에 있는 동안 설정이 바뀌면 최신 설정으로 색인해야 한다."""
    _write_doc(plain_corpus, "규정.md")
    corpora.update(plain_corpus, chunk_size=300, chunk_overlap=50)

    job = jobs.enqueue(plain_corpus.id, jobs.KIND_INCREMENTAL, "tester")

    assert _wait_for(job.id).status == jobs.STATUS_SUCCEEDED


# ---------------------------------------------------------------------------
# 재시작 복구
# ---------------------------------------------------------------------------

def test_reset_interrupted_marks_stale_jobs(plain_corpus):
    """프로세스가 색인 중에 죽으면 잡이 영원히 running으로 남는다."""
    queued = jobs._create_job(plain_corpus.id, jobs.KIND_INCREMENTAL, "tester")
    running = jobs._create_job(plain_corpus.id, jobs.KIND_REBUILD, "tester")
    jobs._mark_running(running)

    n = jobs.reset_interrupted()

    assert n == 2
    assert jobs.get(queued).status == jobs.STATUS_INTERRUPTED
    assert jobs.get(running).status == jobs.STATUS_INTERRUPTED
    assert "재시작" in jobs.get(running).error


def test_reset_interrupted_leaves_finished_jobs_alone(plain_corpus, patch_embed):
    _write_doc(plain_corpus, "규정.md")
    done = _wait_for(jobs.enqueue(plain_corpus.id, jobs.KIND_INCREMENTAL, "x").id)

    jobs.reset_interrupted()

    assert jobs.get(done.id).status == jobs.STATUS_SUCCEEDED


def test_interrupted_job_does_not_block_new_ones(plain_corpus, patch_embed):
    jobs._mark_running(jobs._create_job(plain_corpus.id, jobs.KIND_REBUILD, "x"))
    jobs.reset_interrupted()

    _write_doc(plain_corpus, "규정.md")
    job = jobs.enqueue(plain_corpus.id, jobs.KIND_INCREMENTAL, "tester")

    assert _wait_for(job.id).status == jobs.STATUS_SUCCEEDED


# ---------------------------------------------------------------------------
# 이력 조회
# ---------------------------------------------------------------------------

def test_history_is_scoped_per_corpus(plain_corpus, seed_corpus):
    jobs._create_job(plain_corpus.id, jobs.KIND_INCREMENTAL, "tester")
    jobs._create_job(seed_corpus.id, jobs.KIND_INCREMENTAL, "tester")

    assert len(jobs.list_for_corpus(plain_corpus.id)) == 1
    assert len(jobs.list_for_corpus(seed_corpus.id)) == 1
    assert len(jobs.list_recent()) == 2


def test_audit_records_job_lifecycle(plain_corpus, patch_embed):
    from admin import audit

    _write_doc(plain_corpus, "규정.md")
    job = jobs.enqueue(plain_corpus.id, jobs.KIND_INCREMENTAL, "tester")
    _wait_for(job.id)

    actions = {entry.action for entry in audit.recent()}
    assert "job.incremental.started" in actions
    assert "job.incremental.succeeded" in actions
