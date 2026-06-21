"""FastAPI /search 엔드포인트 테스트 — 실제 API/Chroma 호출 없음."""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import config

# ---------------------------------------------------------------------------
# 픽스처 공통 데이터
# ---------------------------------------------------------------------------

FAKE_RESULTS = [
    {
        "title": "공기의 압력을 이용한 미니 발전기",
        "year": 1979,
        "category": "과학완구",
        "author": "강용환",
        "doc_type": "작품설명서",
        "source_path": "docs/1979-과학완구-강용환-공기의 압력을 이용한 미니 발전기-.md",
        "similarity": 0.87,
        "snippet": "공기 압력을 이용해 소형 발전 장치를 구성한다.",
    },
    {
        "title": "태양열 집열 장치",
        "year": 1985,
        "category": "생활과학Ⅰ",
        "author": "김철수",
        "doc_type": "작품설명서",
        "source_path": "docs/1985-생활과학Ⅰ-김철수-태양열 집열 장치-.md",
        "similarity": 0.72,
        "snippet": "태양열을 이용한 집열 장치 설계.",
    },
]

RESULT_ITEM_KEYS = {
    "title", "year", "category", "author", "doc_type",
    "source_path", "similarity", "snippet",
}


@pytest.fixture()
def client(monkeypatch):
    """TestClient with search monkeypatched — no real API/Chroma calls."""
    import api.main as api_main

    monkeypatch.setattr(api_main, "search", lambda query, top_k, include_advisor_docs: FAKE_RESULTS)

    from fastapi.testclient import TestClient
    with TestClient(api_main.app) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. 정상 검색 요청 → 200, 스키마 검증, similarity 내림차순
# ---------------------------------------------------------------------------

def test_search_valid(client):
    resp = client.post("/search", json={"text": "공기 압력을 이용한 소형 발전 장치"})
    assert resp.status_code == 200
    body = resp.json()

    assert "query" in body
    assert "results" in body
    assert "count" in body
    assert body["count"] == len(body["results"])
    assert body["count"] == 2

    for item in body["results"]:
        assert RESULT_ITEM_KEYS == set(item.keys())

    # similarity 내림차순
    sims = [r["similarity"] for r in body["results"]]
    assert sims == sorted(sims, reverse=True)


# ---------------------------------------------------------------------------
# 2. 입력 검증 — 빈 문자열, 공백만, 초과 길이 → 4xx
# ---------------------------------------------------------------------------

def test_empty_text(client):
    resp = client.post("/search", json={"text": ""})
    assert resp.status_code == 422


def test_whitespace_only_text(client):
    resp = client.post("/search", json={"text": "   "})
    assert 400 <= resp.status_code < 500


def test_text_too_long(client):
    long_text = "가" * (config.MAX_QUERY_CHARS + 1)
    resp = client.post("/search", json={"text": long_text})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 3. GET /health → 200 {"status": "ok"}
# ---------------------------------------------------------------------------

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# 4. include_advisor_docs=true 가 search 에 전달되는지 확인
# ---------------------------------------------------------------------------

def test_include_advisor_docs_forwarded(monkeypatch):
    import api.main as api_main

    captured: dict = {}

    def fake_search(query, top_k, include_advisor_docs):
        captured["query"] = query
        captured["top_k"] = top_k
        captured["include_advisor_docs"] = include_advisor_docs
        return FAKE_RESULTS

    monkeypatch.setattr(api_main, "search", fake_search)

    with TestClient(api_main.app) as c:
        resp = c.post(
            "/search",
            json={"text": "발명 아이디어", "top_k": 3, "include_advisor_docs": True},
        )

    assert resp.status_code == 200
    assert captured["include_advisor_docs"] is True
    assert captured["top_k"] == 3


# ---------------------------------------------------------------------------
# 5. search 가 RuntimeError 를 던지면 → 503
# ---------------------------------------------------------------------------

def test_runtime_error_returns_503(monkeypatch):
    import api.main as api_main

    def fake_search_error(query, top_k, include_advisor_docs):
        raise RuntimeError("GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")

    monkeypatch.setattr(api_main, "search", fake_search_error)

    with TestClient(api_main.app) as c:
        resp = c.post("/search", json={"text": "발명 아이디어"})

    assert resp.status_code == 503
