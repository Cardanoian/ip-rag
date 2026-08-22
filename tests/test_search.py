"""search() 단위 테스트 — 실제 API/Chroma 없음.

monkeypatch 대상:
  - ingest.search.embed_query    → 고정 가짜 벡터 반환
  - ingest.search.get_collection → 가짜 컬렉션 객체 반환
"""
from __future__ import annotations

import math

import pytest

import config
from corpora.models import CorpusConfig
from ingest.parse import ADVISOR_DOC_TYPE, MAIN_DOC_TYPE
from ingest.search import _collapse_snippet, public_document_id, search

FAKE_VEC = [0.0] * 10

INVENTION_CFG = CorpusConfig(
    id="inventions",
    kind="invention",
    label="발명대회 수상작",
    corpus_id="test-awards",
    base_collection="inventions",
    doc_prefix="문서\n",
    query_prefix="질의\n",
    embed_dim=1536,
    chunk_size=1000,
    chunk_overlap=150,
    single_chunk_char_hint=5500,
    active_collection="inventions_v1",
    index_version="inventions:v1",
)

PLAIN_CFG = INVENTION_CFG.with_updates(
    id="rules",
    kind="plain",
    label="학교 규정",
    corpus_id="school-rules",
    base_collection="rules",
    active_collection="rules_v1",
)


def _meta(source_path: str, n_chunks: int, chunk_index: int, **extra) -> dict:
    return {
        "source_path": source_path,
        "n_chunks": n_chunks,
        "chunk_index": chunk_index,
        "title": extra.get("title", f"제목_{source_path}"),
        "year": extra.get("year", 2000),
        "category": extra.get("category", "과학완구"),
        "author": extra.get("author", "홍길동"),
        "doc_type": extra.get("doc_type", MAIN_DOC_TYPE),
    }


def _make_fake_collection(chunks: list[tuple[str, float, dict]]):
    """chunks = list of (document_text, distance, metadata).

    captured_kwargs 속성에 마지막 query 호출의 kwargs가 저장된다.
    """

    class FakeCollection:
        def __init__(self):
            self.captured_kwargs: dict = {}

        def query(self, **kwargs):
            self.captured_kwargs = kwargs
            return {
                "ids": [[f"id_{i}" for i in range(len(chunks))]],
                "distances": [[c[1] for c in chunks]],
                "metadatas": [[c[2] for c in chunks]],
                "documents": [[c[0] for c in chunks]],
            }

    return FakeCollection()


@pytest.fixture()
def patch_search(monkeypatch):
    """embed_query와 get_collection을 가짜로 바꾸는 헬퍼를 돌려준다."""

    def _install(chunks):
        fake_col = _make_fake_collection(chunks)
        monkeypatch.setattr("ingest.search.embed_query", lambda q, cfg: FAKE_VEC)
        monkeypatch.setattr("ingest.search.get_collection", lambda name: fake_col)
        return fake_col

    return _install


# ---------------------------------------------------------------------------
# 집계 — 같은 source_path 의 두 청크가 하나의 문서로 합쳐진다.
# ---------------------------------------------------------------------------

def test_aggregation_collapses_same_source_path(patch_search):
    sp_a = "inventions/work_a.md"
    sp_b = "inventions/work_b.md"

    # sp_a: 청크 2개 (distance 0.4, 0.6) → sim 0.8, 0.7 → max=0.8
    # sp_b: 청크 1개 (distance 0.3)      → sim 0.85
    patch_search([
        ("텍스트 A0", 0.4, _meta(sp_a, n_chunks=2, chunk_index=0)),
        ("텍스트 A1", 0.6, _meta(sp_a, n_chunks=2, chunk_index=1)),
        ("텍스트 B0", 0.3, _meta(sp_b, n_chunks=1, chunk_index=0)),
    ])

    results = search(INVENTION_CFG, "테스트 질의", top_k=5)

    assert len(results) == 2
    assert {r["source_path"] for r in results} == {sp_a, sp_b}

    result_a = next(r for r in results if r["source_path"] == sp_a)
    expected = round(0.8 - config.LENGTH_NORM_C * math.log(2), 4)
    assert result_a["similarity"] == expected


# ---------------------------------------------------------------------------
# 유사도 변환 및 내림차순 정렬
# ---------------------------------------------------------------------------

def test_similarity_conversion_and_sort(patch_search):
    patch_search([
        ("완벽 매칭", 0.0, _meta("inventions/perfect.md", 1, 0)),
        ("최악 매칭", 2.0, _meta("inventions/worst.md", 1, 0)),
        ("중간 매칭", 1.0, _meta("inventions/mid.md", 1, 0)),
    ])

    results = search(INVENTION_CFG, "질의", top_k=5)

    assert [r["similarity"] for r in results] == [1.0, 0.5, 0.0]


# ---------------------------------------------------------------------------
# where 필터는 corpus 종류가 결정한다
# ---------------------------------------------------------------------------

def test_invention_filters_to_main_docs_by_default(patch_search):
    fake_col = patch_search([("텍스트", 0.5, _meta("inventions/a.md", 1, 0))])

    search(INVENTION_CFG, "질의")

    assert fake_col.captured_kwargs["where"] == {"doc_type": MAIN_DOC_TYPE}


def test_invention_includes_advisor_docs_when_requested(patch_search):
    fake_col = patch_search([("텍스트", 0.5, _meta("inventions/a.md", 1, 0))])

    search(INVENTION_CFG, "질의", options={"include_advisor_docs": True})

    assert fake_col.captured_kwargs["where"] is None


def test_plain_corpus_has_no_filter(patch_search):
    """일반 텍스트 corpus에는 doc_type 개념이 없으므로 필터도 없다."""
    fake_col = patch_search([("규정 본문", 0.4, {
        "source_path": "rules/제17조.md",
        "n_chunks": 1,
        "chunk_index": 0,
        "title": "제17조",
    })])

    search(PLAIN_CFG, "질의", options={"include_advisor_docs": True})

    assert fake_col.captured_kwargs["where"] is None


# ---------------------------------------------------------------------------
# 응답 형태
# ---------------------------------------------------------------------------

def test_result_shape_and_snippet(patch_search):
    long_text = "발명  내용\n" * 50
    patch_search([
        (long_text, 0.2, _meta(
            "inventions/inv.md", 1, 0,
            title="발명품A", year=2005, category="생활과학", author="김철수",
        )),
    ])

    results = search(INVENTION_CFG, "질의", top_k=5)
    assert len(results) == 1
    item = results[0]

    assert set(item.keys()) == {
        "document_id", "title", "source_path", "similarity",
        "snippet", "metadata", "_raw_metadata",
    }
    assert item["title"] == "발명품A"
    assert item["document_id"] == public_document_id("inventions/inv.md")

    # snippet: 공백 접기 + 200자 이하
    assert len(item["snippet"]) <= 200
    assert "\n" not in item["snippet"]
    assert "  " not in item["snippet"]


def test_public_metadata_excludes_author(patch_search):
    """저자명은 과거 참가 학생의 개인정보다. 공개 메타데이터에 들어가면 안 된다."""
    patch_search([
        ("본문", 0.2, _meta("inventions/inv.md", 1, 0, author="김철수")),
    ])

    item = search(INVENTION_CFG, "질의")[0]

    assert "author" not in item["metadata"]
    assert set(item["metadata"]) <= {"title", "year", "category", "doc_type"}
    # 원본 메타는 하위 호환 엔드포인트를 위해 별도 키로만 남는다.
    assert item["_raw_metadata"]["author"] == "김철수"


def test_plain_metadata_is_title_only(patch_search):
    patch_search([("규정 본문", 0.3, {
        "source_path": "rules/제17조.md",
        "n_chunks": 1,
        "chunk_index": 0,
        "title": "제17조",
    })])

    item = search(PLAIN_CFG, "질의")[0]

    assert item["metadata"] == {"title": "제17조"}


def test_year_sentinel_becomes_null(patch_search):
    """색인은 None을 못 담아 -1로 저장한다. 응답에서는 null로 되돌린다."""
    patch_search([("본문", 0.2, _meta("inventions/x.md", 1, 0, year=-1))])

    item = search(INVENTION_CFG, "질의")[0]

    assert item["metadata"]["year"] is None


def test_underfill_returns_available_results(patch_search):
    patch_search([("단일 문서", 0.3, _meta("inventions/only.md", 1, 0))])

    results = search(INVENTION_CFG, "질의", top_k=5)

    assert len(results) == 1
    assert results[0]["source_path"] == "inventions/only.md"


def test_query_uses_active_collection(patch_search, monkeypatch):
    """검색은 corpus의 활성 컬렉션을 본다 — alias 전환의 기반이다."""
    captured: dict = {}

    def fake_get_collection(name):
        captured["name"] = name
        return _make_fake_collection([("본문", 0.3, _meta("rules/a.md", 1, 0))])

    monkeypatch.setattr("ingest.search.embed_query", lambda q, cfg: FAKE_VEC)
    monkeypatch.setattr("ingest.search.get_collection", fake_get_collection)

    search(PLAIN_CFG.with_updates(active_collection="rules_v7"), "질의")

    assert captured["name"] == "rules_v7"


# ---------------------------------------------------------------------------
# snippet 헬퍼
# ---------------------------------------------------------------------------

def test_collapse_snippet_truncates_and_collapses():
    text = "  안녕   하세요\n\n\t반갑습니다  " + "x" * 300
    result = _collapse_snippet(text, max_chars=200)
    assert len(result) <= 200
    assert "  " not in result
    assert "\n" not in result
    assert "\t" not in result


def test_collapse_snippet_short_text():
    assert _collapse_snippet("짧은 텍스트") == "짧은 텍스트"


def test_public_document_id_is_stable_and_opaque():
    path = "inventions/1979-과학완구-홍길동-발명품-.md"
    assert public_document_id(path) == public_document_id(path)
    assert len(public_document_id(path)) == 24
    assert path not in public_document_id(path)
