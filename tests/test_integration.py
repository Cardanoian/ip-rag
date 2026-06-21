"""Integration test: build_index -> ChromaDB -> search (no real Gemini API).

Fake-embedding approach
-----------------------
We patch `ingest.build_index.embed_documents` and `ingest.search.embed_query`
with a deterministic char-frequency bucket function.

  fake_embed(text) -> float[EMBED_DIM]

  For each character in `text`, map it to a bucket:
    bucket = ord(ch) % EMBED_DIM
  Increment that bucket's count, then L2-normalise the result.

Why this guarantees self-recall
--------------------------------
At index time, build_index calls embed_documents with texts of the form:
    "제목: <title> | 분야: <category>\\n<body>"

At query time, search calls embed_query with just the raw query (no prefix —
the real embedder adds QUERY_TASK_PREFIX internally, but we bypass that by
patching the higher-level symbol embed_query in ingest.search).

So the indexed vector has a short prefix before the body, and the query vector
is the body alone.  With a char-frequency embedding, the cosine similarity
between the two is dominated by the body chars (which are identical); the
~30-char prefix adds small noise.  More importantly, each of our 3 test
documents has *distinct* Korean content, so the self-doc similarity will always
outrank cross-doc similarity.

Patch targets (module-level names already bound by the `from … import`):
  ingest.build_index.embed_documents  (used inside _flush())
  ingest.search.embed_query            (used at the top of search())
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

import config
import ingest.store as store
from ingest.build_index import build_index
from ingest.search import search


# ---------------------------------------------------------------------------
# Fake embedding
# ---------------------------------------------------------------------------

def _fake_embed_one(text: str) -> list[float]:
    """Char-frequency bucket embedding, L2-normalised."""
    dim = config.EMBED_DIM
    vec = [0.0] * dim
    for ch in text:
        vec[ord(ch) % dim] += 1.0
    # L2 normalise
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def _fake_embed_documents(texts: list[str]) -> list[list[float]]:
    return [_fake_embed_one(t) for t in texts]


def _fake_embed_query(text: str) -> list[float]:
    return _fake_embed_one(text)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_chroma(tmp_path, monkeypatch):
    """Point config.CHROMA_PATH at a fresh tmp dir and reset store cache."""
    monkeypatch.setattr(config, "CHROMA_PATH", tmp_path / "chroma")
    store.reset_cache()
    yield
    store.reset_cache()


@pytest.fixture()
def patch_embedders(monkeypatch):
    """Patch both embed symbols with the fake implementation."""
    import ingest.build_index as bi
    import ingest.search as se
    monkeypatch.setattr(bi, "embed_documents", _fake_embed_documents)
    monkeypatch.setattr(se, "embed_query", _fake_embed_query)


@pytest.fixture()
def docs_dir(tmp_path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Document bodies — DISTINCT Korean content per document
# ---------------------------------------------------------------------------

BODY_USAN = (
    "자동 우산 건조기는 우산의 물기를 제거하는 발명품입니다. "
    "비가 온 뒤 젖은 우산을 건조기에 넣으면 회전 드럼이 우산 표면의 "
    "물방울을 원심력으로 제거합니다. 손잡이 부분에는 흡수성 스펀지가 "
    "부착되어 손잡이의 물기도 함께 흡수합니다. 건조 후 우산은 깔끔하게 "
    "접혀 보관함에 자동으로 수납됩니다. 이 장치는 학교 현관이나 "
    "지하철 입구에 설치하여 이용객의 편의를 높일 수 있습니다."
)

BODY_PAENGI = (
    "빛나는 팽이는 LED 조명을 내장한 과학완구입니다. 팽이가 회전하면 "
    "원심력에 의해 배터리와 LED 회로가 자동으로 연결되어 다양한 색상의 "
    "빛을 발산합니다. 정지 상태에서는 배터리가 분리되어 전력을 소모하지 "
    "않습니다. 회전 속도에 따라 점등 패턴이 달라지며, 어두운 곳에서 "
    "화려한 빛의 궤적을 만들어냅니다. 팽이의 축은 세라믹 볼베어링을 "
    "사용하여 마찰을 최소화하였습니다."
)

BODY_DOKSODAE = (
    "접이식 독서대는 책을 편안하게 읽을 수 있도록 설계된 학습용품입니다. "
    "알루미늄 합금 프레임을 사용하여 가볍고 튼튼합니다. 독서대의 각도는 "
    "15도에서 75도까지 무단 조절이 가능하여 사용자의 자세에 맞게 설정할 "
    "수 있습니다. 사용하지 않을 때는 얇게 접혀 가방에 쉽게 수납됩니다. "
    "페이지 고정 클립이 내장되어 바람에도 책장이 넘어가지 않습니다."
)

BODY_ADVISOR = (
    "본 논문은 학생 발명 지도 방법론에 관한 연구입니다. "
    "창의적 문제 해결 기법을 초등학생에게 적용한 사례를 분석하였습니다. "
    "트리즈 기법과 브레인스토밍을 결합한 지도 방식이 가장 효과적임을 "
    "확인하였으며, 발명 교육과정 개선 방향을 제언합니다. 지도논문."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(docs_dir: Path, filename: str, body: str) -> Path:
    p = docs_dir / filename
    p.write_text(body, encoding="utf-8")
    return p


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Integration fixture: build the index once, run all integration tests on it
# ---------------------------------------------------------------------------

@pytest.fixture()
def built_index(docs_dir, patch_embedders):
    """Write test docs, build the index, return (docs_dir, stats)."""
    _write(docs_dir, "1999-생활과학Ⅰ-홍길동-자동 우산 건조기-.md", BODY_USAN)
    _write(docs_dir, "2001-과학완구-김철수-빛나는 팽이-.md", BODY_PAENGI)
    _write(docs_dir, "2005-학습용품-이영희-접이식 독서대-.md", BODY_DOKSODAE)
    _write(docs_dir, "2003-교육연구-박지도-발명 지도 방법론 연구(지도논문)-.md", BODY_ADVISOR)
    _write(docs_dir, "2010-과학완구-공백-빈파일-.md", "")  # 0-byte → skipped

    stats = build_index(docs_dir=docs_dir, reset=True)
    return docs_dir, stats


# ---------------------------------------------------------------------------
# Test 1: Indexing stats — valid docs indexed, 0-byte file skipped
# ---------------------------------------------------------------------------

class TestIndexingStats:
    def test_valid_docs_are_indexed(self, built_index):
        """build_index returns indexed_docs=4 (3 main + 1 advisor)."""
        _, stats = built_index
        assert stats["indexed_docs"] == 4

    def test_zero_byte_file_is_skipped(self, built_index):
        """build_index skips the 0-byte file."""
        _, stats = built_index
        assert stats["skipped_docs"] == 1

    def test_no_failures(self, built_index):
        """No documents should fail processing."""
        _, stats = built_index
        assert stats["failed_docs"] == 0

    def test_chunks_were_embedded(self, built_index):
        """Embedded chunks equals total chunks (fresh build, no reuse)."""
        _, stats = built_index
        assert stats["total_chunks"] > 0
        assert stats["embedded_chunks"] == stats["total_chunks"]

    def test_chroma_contains_all_chunks(self, built_index):
        """ChromaDB collection count matches reported total_chunks."""
        _, stats = built_index
        col = store.get_collection()
        assert col.count() == stats["total_chunks"]


# ---------------------------------------------------------------------------
# Test 2: Self-recall — query on a doc's own body returns that doc as top result
# ---------------------------------------------------------------------------

class TestSelfRecall:
    def test_usan_doc_is_top_result_for_its_own_body(self, built_index):
        """Query with 우산 body text → 우산 document is result[0]."""
        results = search(BODY_USAN, top_k=3)
        assert len(results) >= 1, "Expected at least one result"
        assert "우산" in results[0]["title"] or "우산" in results[0]["snippet"], (
            f"Expected 우산 doc as top result, got: {results[0]}"
        )

    def test_usan_self_recall_similarity_is_high(self, built_index):
        """Self-recall similarity for 우산 query should be near 1.0."""
        results = search(BODY_USAN, top_k=3)
        assert results[0]["similarity"] >= 0.90, (
            f"Self-recall similarity too low: {results[0]['similarity']}"
        )

    def test_paengi_doc_is_top_result_for_its_own_body(self, built_index):
        """Query with 팽이 body text → 팽이 document is result[0]."""
        results = search(BODY_PAENGI, top_k=3)
        assert len(results) >= 1
        assert "팽이" in results[0]["title"] or "팽이" in results[0]["snippet"], (
            f"Expected 팽이 doc as top result, got: {results[0]}"
        )

    def test_paengi_self_recall_similarity_is_high(self, built_index):
        """Self-recall similarity for 팽이 query should be near 1.0."""
        results = search(BODY_PAENGI, top_k=3)
        assert results[0]["similarity"] >= 0.90, (
            f"Self-recall similarity too low: {results[0]['similarity']}"
        )

    def test_doksodae_doc_is_top_result_for_its_own_body(self, built_index):
        """Query with 독서대 body text → 독서대 document is result[0]."""
        results = search(BODY_DOKSODAE, top_k=3)
        assert len(results) >= 1
        assert "독서대" in results[0]["title"] or "독서대" in results[0]["snippet"], (
            f"Expected 독서대 doc as top result, got: {results[0]}"
        )


# ---------------------------------------------------------------------------
# Test 3: Advisor filtering
# ---------------------------------------------------------------------------

class TestAdvisorFiltering:
    def test_advisor_doc_excluded_by_default(self, built_index):
        """search() with default include_advisor_docs=False excludes 지도논문."""
        results = search(BODY_ADVISOR, top_k=5, include_advisor_docs=False)
        doc_types = {r["doc_type"] for r in results}
        assert config.ADVISOR_DOC_TYPE not in doc_types, (
            f"지도논문 should be filtered out, but found in results: {results}"
        )

    def test_advisor_doc_included_when_flag_is_true(self, built_index):
        """search() with include_advisor_docs=True can return 지도논문."""
        results = search(BODY_ADVISOR, top_k=5, include_advisor_docs=True)
        doc_types = {r["doc_type"] for r in results}
        assert config.ADVISOR_DOC_TYPE in doc_types, (
            f"지도논문 should appear when include_advisor_docs=True, got: {results}"
        )


# ---------------------------------------------------------------------------
# Test 4: Aggregation sanity
# ---------------------------------------------------------------------------

class TestAggregationSanity:
    def test_results_are_distinct_source_paths(self, built_index):
        """No duplicate source_path in results."""
        results = search(BODY_USAN, top_k=5)
        source_paths = [r["source_path"] for r in results]
        assert len(source_paths) == len(set(source_paths)), (
            f"Duplicate source_paths found: {source_paths}"
        )

    def test_results_sorted_by_similarity_descending(self, built_index):
        """Results are in similarity descending order."""
        results = search(BODY_USAN, top_k=5)
        sims = [r["similarity"] for r in results]
        assert sims == sorted(sims, reverse=True), (
            f"Results not sorted by similarity desc: {sims}"
        )

    def test_result_dict_has_required_keys(self, built_index):
        """Each result dict has the seven documented keys plus snippet."""
        results = search(BODY_USAN, top_k=3)
        required = {"title", "year", "category", "author", "doc_type",
                    "source_path", "similarity", "snippet"}
        for r in results:
            assert set(r.keys()) == required, (
                f"Result missing keys. Got: {set(r.keys())}, expected: {required}"
            )

    def test_self_beats_others_cosine_sanity(self):
        """Unit-level sanity: fake_embed gives higher cosine for self than cross-doc."""
        # Index-time text: build_index prepends "제목: T | 분야: C\n" to the body
        prefix_usan = "제목: 자동 우산 건조기 | 분야: 생활과학Ⅰ\n"
        indexed_usan = _fake_embed_one(prefix_usan + BODY_USAN)
        query_usan = _fake_embed_one(BODY_USAN)

        prefix_paengi = "제목: 빛나는 팽이 | 분야: 과학완구\n"
        indexed_paengi = _fake_embed_one(prefix_paengi + BODY_PAENGI)

        sim_self = _cosine(indexed_usan, query_usan)
        sim_cross = _cosine(indexed_paengi, query_usan)

        assert sim_self > sim_cross, (
            f"Self-cosine {sim_self:.4f} must exceed cross-doc cosine {sim_cross:.4f}"
        )
