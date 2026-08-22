"""corpus 상태 기계와 완전삭제 — draft → published → unpublished → 삭제."""
from __future__ import annotations

import pytest

import corpora
from corpora.models import CorpusValidationError
from ingest.build_index import build_index
from ingest.store import collection_exists, get_collection


def _write_doc(cfg, name: str, body: str = "규정 본문입니다.\n" * 10):
    path = cfg.docs_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 공개 전 격리
# ---------------------------------------------------------------------------

def test_draft_is_not_in_published_list(plain_corpus):
    published = {cfg.id for cfg in corpora.list_published()}
    assert plain_corpus.id not in published
    assert "inventions" in published


def test_get_published_rejects_draft(plain_corpus):
    with pytest.raises(corpora.CorpusNotFound):
        corpora.get_published(plain_corpus.id)


def test_admin_can_still_see_draft(plain_corpus):
    """어드민은 초안도 봐야 관리할 수 있다."""
    assert corpora.get(plain_corpus.id).id == plain_corpus.id
    assert plain_corpus.id in {cfg.id for cfg in corpora.list_all()}


# ---------------------------------------------------------------------------
# 공개 조건
# ---------------------------------------------------------------------------

def test_cannot_publish_empty_corpus(plain_corpus):
    """색인이 비어 있으면 공개해도 검색이 빈 결과만 준다."""
    with pytest.raises(CorpusValidationError):
        corpora.set_status(plain_corpus, corpora.STATUS_PUBLISHED)


def test_publish_after_indexing(plain_corpus, patch_embed):
    _write_doc(plain_corpus, "학교 규정.md")
    build_index(plain_corpus)

    published = corpora.set_status(plain_corpus, corpora.STATUS_PUBLISHED)

    assert published.status == corpora.STATUS_PUBLISHED
    assert plain_corpus.id in {cfg.id for cfg in corpora.list_published()}


def test_unpublish_keeps_data(plain_corpus, patch_embed):
    _write_doc(plain_corpus, "학교 규정.md")
    build_index(plain_corpus)
    cfg = corpora.set_status(plain_corpus, corpora.STATUS_PUBLISHED)

    cfg = corpora.set_status(cfg, corpora.STATUS_UNPUBLISHED)

    assert cfg.status == corpora.STATUS_UNPUBLISHED
    # 데이터는 그대로 남는다 — 되돌릴 수 있어야 한다.
    assert cfg.docs_dir().exists()
    assert get_collection(cfg.active_collection).count() > 0


def test_republish_from_unpublished(plain_corpus, patch_embed):
    _write_doc(plain_corpus, "학교 규정.md")
    build_index(plain_corpus)
    cfg = corpora.set_status(plain_corpus, corpora.STATUS_PUBLISHED)
    cfg = corpora.set_status(cfg, corpora.STATUS_UNPUBLISHED)

    cfg = corpora.set_status(cfg, corpora.STATUS_PUBLISHED)

    assert cfg.status == corpora.STATUS_PUBLISHED


# ---------------------------------------------------------------------------
# 완전삭제
# ---------------------------------------------------------------------------

def test_delete_requires_unpublished(plain_corpus, patch_embed):
    _write_doc(plain_corpus, "학교 규정.md")
    build_index(plain_corpus)
    cfg = corpora.set_status(plain_corpus, corpora.STATUS_PUBLISHED)

    with pytest.raises(CorpusValidationError):
        corpora.delete(cfg)


def test_delete_rejects_draft_too(plain_corpus):
    """초안도 곧바로 지울 수 없다 — 비공개를 한 번 거쳐야 한다."""
    with pytest.raises(CorpusValidationError):
        corpora.delete(plain_corpus)


def test_seed_corpus_cannot_be_deleted(seed_corpus):
    cfg = corpora.set_status(seed_corpus, corpora.STATUS_UNPUBLISHED)
    with pytest.raises(CorpusValidationError):
        corpora.delete(cfg)


def test_delete_removes_registry_row(plain_corpus):
    cfg = corpora.set_status(plain_corpus, corpora.STATUS_UNPUBLISHED)

    corpora.delete(cfg)

    with pytest.raises(corpora.CorpusNotFound):
        corpora.get(cfg.id)
    assert cfg.id not in {c.id for c in corpora.list_all()}


def test_delete_does_not_touch_other_corpora(plain_corpus, patch_embed, seed_corpus):
    _write_doc(seed_corpus, "1999-생활과학Ⅰ-홍길동-우산-.md", "발명품 설명\n" * 20)
    build_index(seed_corpus)
    seed_chunks = get_collection(seed_corpus.active_collection).count()

    cfg = corpora.set_status(plain_corpus, corpora.STATUS_UNPUBLISHED)
    corpora.delete(cfg)

    assert corpora.get(seed_corpus.id) is not None
    assert get_collection(seed_corpus.active_collection).count() == seed_chunks


def test_delete_removes_job_history(plain_corpus):
    from admin import jobs

    jobs._create_job(plain_corpus.id, jobs.KIND_INCREMENTAL, "tester")
    assert jobs.list_for_corpus(plain_corpus.id)

    cfg = corpora.set_status(plain_corpus, corpora.STATUS_UNPUBLISHED)
    corpora.delete(cfg)

    assert jobs.list_for_corpus(plain_corpus.id) == []


# ---------------------------------------------------------------------------
# corpus 간 격리
# ---------------------------------------------------------------------------

def test_corpora_use_separate_collections(plain_corpus, seed_corpus, patch_embed):
    _write_doc(plain_corpus, "규정.md")
    _write_doc(seed_corpus, "1999-생활과학Ⅰ-홍길동-우산-.md", "발명품 설명\n" * 20)

    build_index(plain_corpus)
    build_index(seed_corpus)

    assert plain_corpus.active_collection != seed_corpus.active_collection
    assert collection_exists(plain_corpus.active_collection)
    assert collection_exists(seed_corpus.active_collection)

    # 한쪽 corpus의 문서가 다른 쪽 컬렉션에 섞이면 안 된다.
    plain_sources = {
        meta["source_path"]
        for meta in get_collection(plain_corpus.active_collection)
        .get(include=["metadatas"])["metadatas"]
    }
    assert all(sp.startswith(f"{plain_corpus.id}/") for sp in plain_sources)
