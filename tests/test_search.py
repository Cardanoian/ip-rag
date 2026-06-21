"""tests/test_search.py — search() 단위 테스트 (실제 API/Chroma 없음).

monkeypatch 대상:
  - ingest.search.embed_query   → 고정 가짜 벡터 반환
  - ingest.search.get_collection → 가짜 컬렉션 객체 반환
"""
from __future__ import annotations

import sys
import os
import math

import pytest

# 프로젝트 루트를 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from ingest.search import search, _collapse_snippet

# ---------------------------------------------------------------------------
# 헬퍼 — 가짜 Chroma 응답 빌더
# ---------------------------------------------------------------------------

FAKE_VEC = [0.0] * 10  # embed_query 가짜 반환값


def _meta(source_path: str, n_chunks: int, chunk_index: int, **extra) -> dict:
    base = {
        "source_path": source_path,
        "n_chunks": n_chunks,
        "chunk_index": chunk_index,
        "title": extra.get("title", f"제목_{source_path}"),
        "year": extra.get("year", 2000),
        "category": extra.get("category", "과학완구"),
        "author": extra.get("author", "홍길동"),
        "doc_type": extra.get("doc_type", config.MAIN_DOC_TYPE),
    }
    return base


def _make_fake_collection(chunks: list[tuple[str, float, dict]]):
    """chunks = list of (document_text, distance, metadata).
    반환: .query(**kwargs) 를 지원하는 가짜 컬렉션.
    captured_kwargs 속성에 마지막 query 호출의 kwargs 가 저장된다.
    """

    class FakeCollection:
        def __init__(self):
            self.captured_kwargs: dict = {}

        def query(self, **kwargs):
            self.captured_kwargs = kwargs
            ids = [f"id_{i}" for i in range(len(chunks))]
            distances = [c[1] for c in chunks]
            metadatas = [c[2] for c in chunks]
            documents = [c[0] for c in chunks]
            return {
                "ids": [ids],
                "distances": [distances],
                "metadatas": [metadatas],
                "documents": [documents],
            }

    return FakeCollection()


# ---------------------------------------------------------------------------
# 테스트 1: 집계 — 같은 source_path 의 두 청크가 하나의 작품으로 합쳐진다.
# ---------------------------------------------------------------------------

def test_aggregation_collapses_same_source_path(monkeypatch):
    """두 청크가 같은 source_path 이면 결과는 1건, 다른 source_path 면 별개 건."""
    sp_a = "docs/work_a.md"
    sp_b = "docs/work_b.md"

    # sp_a: 청크 2개 (distance 0.4, 0.6) → sim 0.8, 0.7 → max=0.8
    # sp_b: 청크 1개 (distance 0.3)        → sim 0.85
    chunks = [
        ("텍스트 A0", 0.4, _meta(sp_a, n_chunks=2, chunk_index=0)),
        ("텍스트 A1", 0.6, _meta(sp_a, n_chunks=2, chunk_index=1)),
        ("텍스트 B0", 0.3, _meta(sp_b, n_chunks=1, chunk_index=0)),
    ]
    fake_col = _make_fake_collection(chunks)

    monkeypatch.setattr("ingest.search.embed_query", lambda q: FAKE_VEC)
    monkeypatch.setattr("ingest.search.get_collection", lambda: fake_col)

    results = search("테스트 질의", top_k=5)

    # 3개 청크 → 2개 source_path → 2개 작품
    assert len(results) == 2

    source_paths = {r["source_path"] for r in results}
    assert sp_a in source_paths
    assert sp_b in source_paths

    # sp_a 결과: max_sim=0.8, penalty=c*log(2). config.LENGTH_NORM_C=0.0 이므로 score=0.8
    result_a = next(r for r in results if r["source_path"] == sp_a)
    expected_score_a = round(0.8 - config.LENGTH_NORM_C * math.log(2), 4)
    assert result_a["similarity"] == expected_score_a


# ---------------------------------------------------------------------------
# 테스트 2: 유사도 변환 및 내림차순 정렬
# ---------------------------------------------------------------------------

def test_similarity_conversion_and_sort(monkeypatch):
    """distance 0.0 → similarity 1.0, distance 2.0 → similarity 0.0; 내림차순."""
    chunks = [
        ("완벽 매칭", 0.0, _meta("docs/perfect.md", n_chunks=1, chunk_index=0)),
        ("최악 매칭", 2.0, _meta("docs/worst.md",   n_chunks=1, chunk_index=0)),
        ("중간 매칭", 1.0, _meta("docs/mid.md",     n_chunks=1, chunk_index=0)),
    ]
    fake_col = _make_fake_collection(chunks)

    monkeypatch.setattr("ingest.search.embed_query", lambda q: FAKE_VEC)
    monkeypatch.setattr("ingest.search.get_collection", lambda: fake_col)

    results = search("질의", top_k=5)

    assert len(results) == 3
    assert results[0]["similarity"] == 1.0   # distance 0.0
    assert results[1]["similarity"] == 0.5   # distance 1.0
    assert results[2]["similarity"] == 0.0   # distance 2.0

    # 내림차순 정렬 검증
    sims = [r["similarity"] for r in results]
    assert sims == sorted(sims, reverse=True)


# ---------------------------------------------------------------------------
# 테스트 3: where 필터 — include_advisor_docs 플래그
# ---------------------------------------------------------------------------

def test_where_filter_main_only(monkeypatch):
    """include_advisor_docs=False → where={"doc_type": MAIN_DOC_TYPE}."""
    chunks = [
        ("텍스트", 0.5, _meta("docs/a.md", n_chunks=1, chunk_index=0)),
    ]
    fake_col = _make_fake_collection(chunks)

    monkeypatch.setattr("ingest.search.embed_query", lambda q: FAKE_VEC)
    monkeypatch.setattr("ingest.search.get_collection", lambda: fake_col)

    search("질의", include_advisor_docs=False)

    assert fake_col.captured_kwargs["where"] == {"doc_type": config.MAIN_DOC_TYPE}


def test_where_filter_include_advisor(monkeypatch):
    """include_advisor_docs=True → where=None."""
    chunks = [
        ("텍스트", 0.5, _meta("docs/a.md", n_chunks=1, chunk_index=0)),
    ]
    fake_col = _make_fake_collection(chunks)

    monkeypatch.setattr("ingest.search.embed_query", lambda q: FAKE_VEC)
    monkeypatch.setattr("ingest.search.get_collection", lambda: fake_col)

    search("질의", include_advisor_docs=True)

    assert fake_col.captured_kwargs["where"] is None


# ---------------------------------------------------------------------------
# 테스트 4: 응답 dict 키 및 snippet 검증
# ---------------------------------------------------------------------------

def test_response_dict_keys_and_snippet(monkeypatch):
    """반환 dict에 요구되는 7개 키가 모두 있고 snippet 이 200자 이하."""
    long_text = "발명  내용\n" * 50  # 공백·개행 포함, 200자 초과
    chunks = [
        (long_text, 0.2, _meta(
            "docs/inv.md", n_chunks=1, chunk_index=0,
            title="발명품A", year=2005, category="생활과학", author="김철수",
            doc_type=config.MAIN_DOC_TYPE,
        )),
    ]
    fake_col = _make_fake_collection(chunks)

    monkeypatch.setattr("ingest.search.embed_query", lambda q: FAKE_VEC)
    monkeypatch.setattr("ingest.search.get_collection", lambda: fake_col)

    results = search("질의", top_k=5)

    assert len(results) == 1
    r = results[0]

    required_keys = {"title", "year", "category", "author", "doc_type",
                     "source_path", "similarity", "snippet"}
    assert set(r.keys()) == required_keys, f"키 불일치: {set(r.keys())} != {required_keys}"

    # 값 검증
    assert r["title"] == "발명품A"
    assert r["year"] == 2005
    assert r["category"] == "생활과학"
    assert r["author"] == "김철수"
    assert r["doc_type"] == config.MAIN_DOC_TYPE
    assert r["source_path"] == "docs/inv.md"

    # snippet: 공백 접기 + 200자 이하
    assert len(r["snippet"]) <= 200
    assert "\n" not in r["snippet"]        # 개행 없음
    assert "  " not in r["snippet"]        # 이중 공백 없음


# ---------------------------------------------------------------------------
# 테스트 5: under-fill — 1개 작품만 있어도 top_k=5 요청 시 에러 없이 1건 반환
# ---------------------------------------------------------------------------

def test_underfill_returns_available_results(monkeypatch):
    """over-fetch 후 작품이 top_k 미만이어도 에러 없이 확보된 수만 반환."""
    chunks = [
        ("단일 작품 텍스트", 0.3, _meta("docs/only.md", n_chunks=1, chunk_index=0)),
    ]
    fake_col = _make_fake_collection(chunks)

    monkeypatch.setattr("ingest.search.embed_query", lambda q: FAKE_VEC)
    monkeypatch.setattr("ingest.search.get_collection", lambda: fake_col)

    results = search("질의", top_k=5)

    assert len(results) == 1
    assert results[0]["source_path"] == "docs/only.md"


# ---------------------------------------------------------------------------
# 테스트 6: snippet 헬퍼 단독 검증
# ---------------------------------------------------------------------------

def test_collapse_snippet_truncates_and_collapses():
    text = "  안녕   하세요\n\n\t반갑습니다  " + "x" * 300
    result = _collapse_snippet(text, max_chars=200)
    assert len(result) <= 200
    assert "  " not in result  # 이중 공백 없음
    assert "\n" not in result
    assert "\t" not in result


def test_collapse_snippet_short_text():
    text = "짧은 텍스트"
    assert _collapse_snippet(text) == "짧은 텍스트"
