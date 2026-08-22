"""Co-AI 연동에 필요한 운영·개인정보 보호 회귀 테스트."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

import config
import corpora


FAKE_RESULTS = [
    {
        "document_id": "c" * 24,
        "title": "공기의 압력을 이용한 미니 발전기",
        "source_path": "inventions/private-internal-path.md",
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
            "author": "검색 응답에 노출하지 않을 이름",
            "doc_type": "작품설명서",
        },
    }
]


def test_v1_response_hides_author_and_source_path(monkeypatch):
    import api.main as api_main

    monkeypatch.delenv("RAG_API_TOKEN", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setattr(
        api_main,
        "search",
        lambda cfg, text, top_k, options: FAKE_RESULTS,
    )

    with TestClient(api_main.app) as client:
        response = client.post("/v1/search", json={"text": "공기압 발전기"})

    assert response.status_code == 200
    body = response.json()
    item = body["results"][0]
    assert "author" not in item
    assert "source_path" not in item
    assert len(item["document_id"]) == 24
    seed = corpora.get(corpora.SEED_CORPUS_ID)
    assert body["corpus_id"] == seed.corpus_id
    assert body["index_version"] == seed.index_version
    assert body["score_kind"] == "rescaled_cosine_match"
    assert "판정하지 않습니다" in body["notice"]


def test_service_token_is_required_when_configured(monkeypatch):
    import api.main as api_main

    monkeypatch.setenv("RAG_API_TOKEN", "test-service-token")
    monkeypatch.setattr(
        api_main,
        "search",
        lambda cfg, text, top_k, options: FAKE_RESULTS,
    )

    with TestClient(api_main.app) as client:
        unauthorized = client.post("/v1/search", json={"text": "아이디어"})
        authorized = client.post(
            "/v1/search",
            json={"text": "아이디어"},
            headers={"Authorization": "Bearer test-service-token"},
        )

    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"
    assert authorized.status_code == 200


def test_production_without_service_token_returns_503(monkeypatch):
    import api.main as api_main

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("RAG_API_TOKEN", raising=False)

    with TestClient(api_main.app) as client:
        response = client.post("/v1/search", json={"text": "아이디어"})

    assert response.status_code == 503
    assert "구성되지 않았습니다" in response.json()["detail"]


def test_provider_error_detail_is_not_exposed(monkeypatch):
    import api.main as api_main

    monkeypatch.delenv("RAG_API_TOKEN", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)

    def fail(*args):
        raise RuntimeError("sensitive provider detail")

    monkeypatch.setattr(api_main, "search", fail)
    with TestClient(api_main.app) as client:
        response = client.post("/v1/search", json={"text": "아이디어"})

    assert response.status_code == 503
    assert "sensitive provider detail" not in response.text


def test_ready_checks_key_and_index(monkeypatch):
    import api.main as api_main

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "APP_ENV", "development")
    monkeypatch.setattr(api_main, "count_documents", lambda name: 42)

    with TestClient(api_main.app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["indexed_chunks"] == 42


def test_retry_backoff_only_retries_transient_errors(monkeypatch):
    import ingest.embedder as embedder

    calls = {"count": 0}

    def transient_then_success():
        calls["count"] += 1
        if calls["count"] < 3:
            raise httpx.ReadTimeout("temporary timeout")
        return "ok"

    monkeypatch.setattr(embedder.time, "sleep", lambda delay: None)
    result = embedder._call_with_backoff(
        transient_then_success,
        max_tries=3,
        base_delay=0,
    )

    assert result == "ok"
    assert calls["count"] == 3


def test_retry_backoff_fails_fast_for_permanent_error(monkeypatch):
    import ingest.embedder as embedder

    calls = {"count": 0}

    def invalid_request():
        calls["count"] += 1
        raise ValueError("invalid request")

    monkeypatch.setattr(embedder.time, "sleep", lambda delay: None)
    try:
        embedder._call_with_backoff(invalid_request, max_tries=5, base_delay=0)
    except ValueError:
        pass
    else:
        raise AssertionError("ValueError must be raised")

    assert calls["count"] == 1


def test_external_docs_path_does_not_leak_absolute_path(tmp_path):
    """서버의 실제 파일 경로가 색인이나 응답에 새면 안 된다."""
    from corpora.kinds import InventionKind

    path = tmp_path / "2000-생활과학-학생-테스트 발명품-.md"
    path.write_text("충분히 긴 발명품 설명입니다. " * 10, encoding="utf-8")

    document = InventionKind().load(path, "inventions")

    assert document is not None
    assert document["source_path"] == f"inventions/{path.name}"
    assert str(tmp_path) not in document["source_path"]


def test_provider_auth_failure_maps_to_503(monkeypatch, seed_corpus):
    """API 키 오류는 서버 설정 문제다. Rails가 버그(500)로 오인하면 안 된다."""
    from google.genai import errors

    import ingest.embedder as embedder

    class FakeResponse:
        status_code = 400
        headers: dict = {}

        def json(self):
            return {"error": {"code": 400, "message": "API key not valid"}}

    def raise_client_error():
        raise errors.ClientError(400, FakeResponse().json(), FakeResponse())

    monkeypatch.setattr(embedder, "_call_with_backoff", lambda fn, **kw: raise_client_error())

    with pytest.raises(embedder.EmbeddingUnavailable):
        embedder.embed_query("질의", seed_corpus)


def test_provider_failure_detail_is_not_in_exception_message(monkeypatch, seed_corpus):
    """예외 메시지에 공급자 응답 본문이 섞이면 로그·응답으로 새어나갈 수 있다."""
    import ingest.embedder as embedder

    def raise_timeout(fn, **kwargs):
        raise httpx.ReadTimeout("secret-internal-host-detail")

    monkeypatch.setattr(embedder, "_call_with_backoff", raise_timeout)

    with pytest.raises(embedder.EmbeddingUnavailable) as caught:
        embedder.embed_query("질의", seed_corpus)

    assert "secret-internal-host-detail" not in str(caught.value)


def test_embedding_unavailable_is_runtime_error():
    """API 계층의 503 매핑이 RuntimeError 기준이므로 상속 관계가 계약이다."""
    import ingest.embedder as embedder

    assert issubclass(embedder.EmbeddingUnavailable, RuntimeError)
