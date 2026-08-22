"""build_index 테스트 — 실제 API 호출도, 실제 corpus 문서도 쓰지 않는다.

격리 전략 (tests/conftest.py 참조):
- embed_documents를 가짜 벡터로 교체
- DATA_DIR 계열 경로를 tmp_path로 돌려 Chroma·운영 DB·문서 루트를 모두 분리
"""
from __future__ import annotations

from pathlib import Path

import pytest

import config
import corpora
import ingest.store as store
from ingest.build_index import build_index, remove_document
from tests.conftest import fake_embed_documents as _fake_embed


def _write_doc(docs_dir: Path, filename: str, body: str) -> Path:
    p = docs_dir / filename
    p.write_text(body, encoding="utf-8")
    return p


def _long_body(n_chars: int = 6000) -> str:
    """Return a body longer than SINGLE_CHUNK_CHAR_HINT so it gets split into multiple chunks."""
    line = "발명품 본문 내용입니다. 이 문장은 청킹 테스트를 위해 반복됩니다.\n"
    repetitions = (n_chars // len(line)) + 1
    return line * repetitions


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def cfg(seed_corpus):
    """시드 발명 corpus. conftest가 데이터 경로를 이미 격리해 두었다."""
    return seed_corpus


# ---------------------------------------------------------------------------
# Test 1: First build indexes valid docs, skips 0-byte file
# ---------------------------------------------------------------------------

def test_first_build_indexes_valid_docs_skips_empty(cfg, docs_dir, patch_embed):
    # 2 valid docs + 1 empty
    body = "발명품 설명\n" * 20  # well above MIN_DOC_CHARS (50)
    _write_doc(docs_dir, "1999-생활과학Ⅰ-홍길동-간편한 우산-.md", body)
    _write_doc(docs_dir, "2005-학습용품-이발명-멋진발명품-.md", body)
    _write_doc(docs_dir, "2010-과학완구-공백-빈파일-.md", "")  # 0-byte → skipped

    stats = build_index(cfg, docs_dir=docs_dir, reset=False)

    assert stats["indexed_docs"] == 2
    assert stats["skipped_docs"] == 1
    assert stats["failed_docs"] == 0
    assert stats["total_chunks"] > 0
    assert stats["embedded_chunks"] == stats["total_chunks"]
    assert stats["reused_docs"] == 0

    # Collection must actually contain chunks
    col = store.get_collection(cfg.active_collection)
    assert col.count() == stats["total_chunks"]


# ---------------------------------------------------------------------------
# Test 2: Idempotency — second build with unchanged files reuses all docs
# ---------------------------------------------------------------------------

def test_idempotency_unchanged_files(cfg, docs_dir, patch_embed):
    body = "발명품 설명\n" * 20
    _write_doc(docs_dir, "1999-생활과학Ⅰ-홍길동-간편한 우산-.md", body)
    _write_doc(docs_dir, "2005-학습용품-이발명-멋진발명품-.md", body)

    stats1 = build_index(cfg, docs_dir=docs_dir)
    count_after_first = store.get_collection(cfg.active_collection).count()

    stats2 = build_index(cfg, docs_dir=docs_dir)
    count_after_second = store.get_collection(cfg.active_collection).count()

    # All docs reused on second run
    assert stats2["reused_docs"] == 2
    assert stats2["indexed_docs"] == 0
    assert stats2["embedded_chunks"] == 0

    # No duplicate ids — collection count must not grow
    assert count_after_second == count_after_first


# ---------------------------------------------------------------------------
# Test 3: Orphan deletion — shrinking a doc leaves no leftover chunk ids
# ---------------------------------------------------------------------------

def test_orphan_deletion_on_shrink(cfg, docs_dir, patch_embed):
    fname = "2001-자원재활용-김발명-재활용 발명품-.md"

    # First build: long body → multiple chunks
    long_body = _long_body(n_chars=7000)
    _write_doc(docs_dir, fname, long_body)
    stats1 = build_index(cfg, docs_dir=docs_dir)

    col = store.get_collection(cfg.active_collection)
    # Determine source_path as build_index would compute it (POSIX relative)
    doc_first = corpora.kind_of(cfg).load(docs_dir / fname, cfg.id)
    assert doc_first is not None
    source_path = doc_first["source_path"]

    first_chunks = col.get(where={"source_path": source_path}, include=["metadatas"])
    n_first = len(first_chunks["ids"])
    assert n_first > 1, "Long body must produce multiple chunks for this test to be meaningful"

    # Overwrite with a SHORT body → fewer chunks
    short_body = "발명품 설명\n" * 20  # well under SINGLE_CHUNK_CHAR_HINT → 1 chunk
    (docs_dir / fname).write_text(short_body, encoding="utf-8")

    stats2 = build_index(cfg, docs_dir=docs_dir)

    col2 = store.get_collection(cfg.active_collection)
    after = col2.get(where={"source_path": source_path}, include=["metadatas"])
    n_after = len(after["ids"])

    # Must have fewer chunks now (no orphans from the old, longer version)
    assert n_after < n_first
    assert n_after >= 1

    # chunk_index values must be exactly 0..n_after-1 (contiguous, no gaps/orphans)
    chunk_indices = sorted(m["chunk_index"] for m in after["metadatas"])
    assert chunk_indices == list(range(n_after))

    # n_chunks metadata must reflect new count
    for meta in after["metadatas"]:
        assert meta["n_chunks"] == n_after


def test_lfs_pointer_documents_are_counted_not_indexed(cfg, docs_dir, patch_embed):
    """포인터 파일은 색인하지 않고 따로 세어 원인을 드러낸다."""
    pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:e8334a1c9f2b7d6e5a4c3b2a1908f7e6d5c4b3a2918f7e6d5c4b3a2918f7e6d5\n"
        "size 4096\n"
    )
    _write_doc(docs_dir, "1979-과학완구-홍길동-포인터-.md", pointer)
    _write_doc(docs_dir, "2005-학습용품-이발명-정상문서-.md", "발명품 설명\n" * 20)

    stats = build_index(cfg, docs_dir=docs_dir)

    assert stats["lfs_pointer_docs"] == 1
    assert stats["indexed_docs"] == 1
    assert stats["failed_docs"] == 0
    # 포인터 텍스트가 색인에 들어가지 않았다.
    documents = store.get_collection(cfg.active_collection).get(include=["documents"])
    assert not any("git-lfs" in doc for doc in documents["documents"])
