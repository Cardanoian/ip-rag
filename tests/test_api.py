"""검색 API 엔드포인트 테스트 — 실제 API/Chroma 호출 없음."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import config
import corpora

# search()가 돌려주는 내부 형태 (metadata + 하위 호환용 _raw_metadata)
FAKE_RESULTS = [
    {
        "document_id": "a" * 24,
        "title": "공기의 압력을 이용한 미니 발전기",
        "source_path": "inventions/1979-과학완구-강용환-공기의 압력을 이용한 미니 발전기-.md",
        "similarity": 0.87,
        "snippet": "공기 압력을 이용해 소형 발전 장치를 구성한다.",
        "metadata": {
            "title": "공기의 압력을 이용한 미니 발전기",
            "year": 1979,
            "category": "과학완구",
            "doc_type": "작품설명서",
        },
        "_raw_metadata": {
            "title": "공기의 압력을 이용한 미니 발전기",
            "year": 1979,
            "category": "과학완구",
            "author": "강용환",
            "doc_type": "작품설명서",
        },
    },
    {
        "document_id": "b" * 24,
        "title": "태양열 집열 장치",
        "source_path": "inventions/1985-생활과학Ⅰ-김철수-태양열 집열 장치-.md",
        "similarity": 0.72,
        "snippet": "태양열을 이용한 집열 장치 설계.",
        "metadata": {
            "title": "태양열 집열 장치",
            "year": 1985,
            "category": "생활과학Ⅰ",
            "doc_type": "작품설명서",
        },
        "_raw_metadata": {
            "title": "태양열 집열 장치",
            "year": 1985,
            "category": "생활과학Ⅰ",
            "author": "김철수",
            "doc_type": "작품설명서",
        },
    },
]

LEGACY_RESULT_KEYS = {
    "title", "year", "category", "author", "doc_type",
    "source_path", "similarity", "snippet",
}


@pytest.fixture()
def client(monkeypatch, seed_corpus):
    """search를 가짜로 교체한 TestClient — 실제 API/Chroma 호출 없음."""
    import api.main as api_main

    monkeypatch.delenv("RAG_API_TOKEN", raising=False)
    monkeypatch.setattr(
        api_main, "search", lambda cfg, text, top_k, options: FAKE_RESULTS
    )
    with TestClient(api_main.app) as c:
        yield c


# ---------------------------------------------------------------------------
# corpus 지정 검색 (신규 표준 경로)
# ---------------------------------------------------------------------------

def test_corpus_search_valid(client):
    resp = client.post(
        "/v1/corpora/inventions/search",
        json={"text": "공기 압력을 이용한 소형 발전 장치"},
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["corpus"] == "inventions"
    assert body["count"] == len(body["results"]) == 2
    assert body["score_kind"] == "rescaled_cosine_match"

    for item in body["results"]:
        assert set(item.keys()) == {
            "document_id", "title", "similarity", "snippet", "metadata"
        }
        # 저자와 내부 경로는 어디에도 나오면 안 된다.
        assert "author" not in item["metadata"]
        assert "source_path" not in item

    sims = [r["similarity"] for r in body["results"]]
    assert sims == sorted(sims, reverse=True)


def test_unknown_corpus_returns_404(client):
    resp = client.post("/v1/corpora/nonexistent/search", json={"text": "질의"})
    assert resp.status_code == 404


def test_unpublished_corpus_is_not_searchable(client, plain_corpus):
    """초안 corpus는 검색 API에 존재하지 않는 것으로 취급한다."""
    resp = client.post(f"/v1/corpora/{plain_corpus.id}/search", json={"text": "질의"})
    assert resp.status_code == 404


def test_corpus_list_excludes_unpublished(client, plain_corpus):
    resp = client.get("/v1/corpora")
    assert resp.status_code == 200
    body = resp.json()

    names = {item["corpus"] for item in body["corpora"]}
    assert "inventions" in names
    assert plain_corpus.id not in names


def test_options_are_forwarded_to_search(monkeypatch, seed_corpus):
    import api.main as api_main

    captured: dict = {}

    def fake_search(cfg, text, top_k, options):
        captured.update(
            {"corpus": cfg.id, "text": text, "top_k": top_k, "options": options}
        )
        return FAKE_RESULTS

    monkeypatch.delenv("RAG_API_TOKEN", raising=False)
    monkeypatch.setattr(api_main, "search", fake_search)

    with TestClient(api_main.app) as c:
        resp = c.post(
            "/v1/corpora/inventions/search",
            json={
                "text": "발명 아이디어",
                "top_k": 3,
                "options": {"include_advisor_docs": True},
            },
        )

    assert resp.status_code == 200
    assert captured["corpus"] == "inventions"
    assert captured["top_k"] == 3
    assert captured["options"]["include_advisor_docs"] is True


def test_legacy_top_level_flag_merges_into_options(monkeypatch, seed_corpus):
    """구 클라이언트가 보내는 최상위 include_advisor_docs도 계속 동작해야 한다."""
    import api.main as api_main

    captured: dict = {}

    def fake_search(cfg, text, top_k, options):
        captured["options"] = options
        return FAKE_RESULTS

    monkeypatch.delenv("RAG_API_TOKEN", raising=False)
    monkeypatch.setattr(api_main, "search", fake_search)

    with TestClient(api_main.app) as c:
        resp = c.post(
            "/v1/corpora/inventions/search",
            json={"text": "발명 아이디어", "include_advisor_docs": True},
        )

    assert resp.status_code == 200
    assert captured["options"]["include_advisor_docs"] is True


# ---------------------------------------------------------------------------
# 하위 호환 경로
# ---------------------------------------------------------------------------

def test_legacy_v1_search_keeps_response_shape(client):
    """기존 Rails 연동이 그대로 동작해야 한다."""
    resp = client.post("/v1/search", json={"text": "공기 압력"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["count"] == 2
    for item in body["results"]:
        assert set(item.keys()) == {
            "document_id", "title", "year", "category",
            "doc_type", "similarity", "snippet",
        }
    assert body["corpus_id"] == corpora.get("inventions").corpus_id


def test_legacy_search_keeps_response_shape(client):
    resp = client.post("/search", json={"text": "공기 압력"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["count"] == 2
    for item in body["results"]:
        assert LEGACY_RESULT_KEYS == set(item.keys())
    assert body["results"][0]["author"] == "강용환"


# ---------------------------------------------------------------------------
# 입력 검증
# ---------------------------------------------------------------------------

def test_empty_text(client):
    resp = client.post("/v1/corpora/inventions/search", json={"text": ""})
    assert resp.status_code == 422


def test_whitespace_only_text(client):
    resp = client.post("/v1/corpora/inventions/search", json={"text": "   "})
    assert 400 <= resp.status_code < 500


def test_text_too_long(client):
    long_text = "가" * (config.MAX_QUERY_CHARS + 1)
    resp = client.post("/v1/corpora/inventions/search", json={"text": long_text})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 헬스체크
# ---------------------------------------------------------------------------

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ready_reports_per_corpus(client):
    resp = client.get("/ready")
    body = resp.json()

    assert "corpora" in body
    names = {item["corpus"] for item in body["corpora"]}
    assert names == {"inventions"}


def test_ready_ignores_draft_corpus(client, plain_corpus):
    """갓 만든 빈 corpus가 헬스체크를 깨뜨리면 안 된다."""
    resp = client.get("/ready")
    body = resp.json()

    names = {item["corpus"] for item in body["corpora"]}
    assert plain_corpus.id not in names
    assert not any(plain_corpus.id in problem for problem in body["problems"])


# ---------------------------------------------------------------------------
# 오류 매핑
# ---------------------------------------------------------------------------

def test_runtime_error_returns_503(monkeypatch, seed_corpus):
    import api.main as api_main

    def fake_search_error(cfg, text, top_k, options):
        raise RuntimeError("GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")

    monkeypatch.delenv("RAG_API_TOKEN", raising=False)
    monkeypatch.setattr(api_main, "search", fake_search_error)

    with TestClient(api_main.app) as c:
        resp = c.post("/v1/corpora/inventions/search", json={"text": "발명 아이디어"})

    assert resp.status_code == 503
    # 공급자 오류 상세가 응답에 새면 안 된다.
    assert "GEMINI_API_KEY" not in resp.text
