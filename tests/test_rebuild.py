"""무중단 alias 전환 재색인 테스트.

핵심 계약: 재색인이 실패하면 기존 색인이 그대로 살아 있어야 한다.
"""
from __future__ import annotations

import pytest

import corpora
from ingest.build_index import build_index
from ingest.rebuild import RebuildError, prune_old_collections, rebuild_corpus
from ingest.store import collection_exists, get_collection, list_collections


def _write_doc(cfg, name: str, body: str = "규정 본문입니다.\n" * 10):
    path = cfg.docs_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture()
def indexed_corpus(plain_corpus, patch_embed):
    """문서 2건이 색인된 corpus."""
    _write_doc(plain_corpus, "규정 1.md")
    _write_doc(plain_corpus, "규정 2.md", "다른 규정 본문입니다.\n" * 10)
    build_index(plain_corpus)
    return corpora.get(plain_corpus.id)


# ---------------------------------------------------------------------------
# 성공 경로
# ---------------------------------------------------------------------------

def test_rebuild_switches_to_new_collection(indexed_corpus, patch_embed):
    assert indexed_corpus.active_collection == "rules_v1"

    updated, stats = rebuild_corpus(indexed_corpus)

    assert updated.active_collection == "rules_v2"
    assert stats["indexed_docs"] == 2
    assert get_collection("rules_v2").count() > 0


def test_rebuild_updates_index_version(indexed_corpus, patch_embed):
    before = indexed_corpus.index_version

    updated, _ = rebuild_corpus(indexed_corpus)

    assert updated.index_version != before
    assert updated.index_version.endswith("v2")


def test_rebuild_clears_needs_rebuild_flag(indexed_corpus, patch_embed):
    stale = corpora.update(indexed_corpus, needs_rebuild=True)

    updated, _ = rebuild_corpus(stale)

    assert updated.needs_rebuild is False


def test_rebuild_persists_to_registry(indexed_corpus, patch_embed):
    """전환은 DB에 남아야 재시작 후에도 새 컬렉션을 본다."""
    rebuild_corpus(indexed_corpus)

    corpora.invalidate_cache()
    assert corpora.get(indexed_corpus.id).active_collection == "rules_v2"


def test_rebuild_picks_up_new_documents(indexed_corpus, patch_embed):
    _write_doc(indexed_corpus, "규정 3.md", "새로 추가된 규정입니다.\n" * 10)

    _, stats = rebuild_corpus(indexed_corpus)

    assert stats["indexed_docs"] == 3


# ---------------------------------------------------------------------------
# 실패 경로 — 기존 색인 보존
# ---------------------------------------------------------------------------

def test_failed_rebuild_keeps_old_collection(indexed_corpus, monkeypatch):
    """임베딩이 실패해도 기존 색인으로 계속 검색할 수 있어야 한다."""
    import ingest.build_index as build_index_module

    old_count = get_collection("rules_v1").count()

    def explode(texts, cfg):
        raise RuntimeError("Gemini API 오류")

    monkeypatch.setattr(build_index_module, "embed_documents", explode)

    with pytest.raises(RuntimeError):
        rebuild_corpus(indexed_corpus)

    # 활성 포인터는 그대로, 옛 컬렉션도 그대로다.
    assert corpora.get(indexed_corpus.id).active_collection == "rules_v1"
    assert get_collection("rules_v1").count() == old_count
    # 실패한 새 컬렉션은 남기지 않는다.
    assert not collection_exists("rules_v2")


def test_empty_rebuild_is_refused(plain_corpus, patch_embed):
    """문서가 하나도 없으면 전환하지 않는다 — 색인을 비워버리는 사고 방지."""
    with pytest.raises(RebuildError):
        rebuild_corpus(plain_corpus)

    assert corpora.get(plain_corpus.id).active_collection == "rules_v1"
    assert not collection_exists("rules_v2")


def test_rebuild_after_all_documents_deleted_keeps_old_index(
    indexed_corpus, patch_embed
):
    for path in indexed_corpus.docs_dir().glob("*.md"):
        path.unlink()

    with pytest.raises(RebuildError):
        rebuild_corpus(indexed_corpus)

    assert get_collection("rules_v1").count() > 0


# ---------------------------------------------------------------------------
# 옛 컬렉션 정리
# ---------------------------------------------------------------------------

def test_rebuild_keeps_one_old_version_for_rollback(indexed_corpus, patch_embed):
    v2, _ = rebuild_corpus(indexed_corpus)

    # v1은 롤백용으로 남는다.
    assert collection_exists("rules_v1")
    assert collection_exists("rules_v2")
    assert v2.active_collection == "rules_v2"


def test_third_rebuild_prunes_oldest(indexed_corpus, patch_embed):
    v2, _ = rebuild_corpus(indexed_corpus)
    v3, _ = rebuild_corpus(v2)

    assert v3.active_collection == "rules_v3"
    assert collection_exists("rules_v3")
    assert collection_exists("rules_v2")  # 직전 버전은 보존
    assert not collection_exists("rules_v1")  # 더 오래된 것은 정리


def test_prune_respects_keep_setting(indexed_corpus, patch_embed):
    v2, _ = rebuild_corpus(indexed_corpus)
    v3, _ = rebuild_corpus(v2)

    dropped = prune_old_collections(v3, keep=0)

    assert "rules_v2" in dropped
    assert not collection_exists("rules_v2")
    assert collection_exists("rules_v3")


def test_prune_never_touches_other_corpora(indexed_corpus, seed_corpus, patch_embed):
    _write_doc(seed_corpus, "1999-생활과학Ⅰ-홍길동-우산-.md", "발명품 설명\n" * 20)
    build_index(seed_corpus)

    rebuild_corpus(indexed_corpus)

    assert collection_exists(seed_corpus.active_collection)


# ---------------------------------------------------------------------------
# 무중단 — 전환 전까지 옛 컬렉션이 계속 응답한다
# ---------------------------------------------------------------------------

def test_old_collection_serves_until_switch(indexed_corpus, monkeypatch, patch_embed):
    """색인이 도는 중간에도 활성 포인터는 옛 컬렉션을 가리킨다."""
    import ingest.build_index as build_index_module

    observed: list[str] = []
    original = build_index_module.embed_documents

    def spy(texts, cfg):
        observed.append(corpora.get(cfg.id).active_collection)
        return original(texts, cfg)

    monkeypatch.setattr(build_index_module, "embed_documents", spy)

    rebuild_corpus(indexed_corpus)

    # 색인 중 관측된 활성 컬렉션은 전부 옛 버전이어야 한다.
    assert observed
    assert set(observed) == {"rules_v1"}
    # 끝난 뒤에야 새 버전으로 바뀐다.
    assert corpora.get(indexed_corpus.id).active_collection == "rules_v2"
