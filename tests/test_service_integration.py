"""공동 서버 계약: 권한·출처·중복·작업 경합을 실제 SQLite/Chroma로 검증한다."""
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import corpora
import storage
from api import services, sources
from ingest import embedder
from ingest.search import search
from ingest.store import get_collection
from tests.conftest import fake_embed_documents, fake_embed_query

SID = "019a0000-0000-7000-8000-000000000001"
V1 = "019a0000-0000-7000-8000-000000000002"
V2 = "019a0000-0000-7000-8000-000000000003"
COAI = "test-coai-token-" + "a" * 32
READ = "test-easyflow-read-" + "b" * 32
WRITE = "test-easyflow-write-" + "c" * 32


def header(token):
    return {"Authorization": f"Bearer {token}"}


def document(revision=1, version=V1, key=None):
    return {"revision": revision, "idempotency_key": key or f"index-{revision}", "source_version_id": version,
            "title": "학교회계 지침", "pages": [{"page": 3, "text": "학교회계 지출 승인 절차를 확인합니다. " * 15}]}


@pytest.fixture()
def shared(seed_corpus, monkeypatch):
    cfg = corpora.create(corpus_slug="easyflow", label="승인 업무", kind="easyflow", corpus_id="easyflow-documents",
        doc_prefix="학교 업무", query_prefix="학교 검색", embed_dim=1536, chunk_size=1000, chunk_overlap=150,
        single_chunk_char_hint=5500, created_by="test")
    cfg = corpora.update(cfg, status=corpora.STATUS_PUBLISHED)
    services.register("coai", "coai-read", COAI, {"inventions": ["search"]})
    services.register("easyflow", "easyflow-read", READ, {"easyflow": ["search"]})
    services.register("easyflow", "easyflow-write", WRITE, {"easyflow": ["write"]})
    monkeypatch.setattr(embedder, "embed_documents", fake_embed_documents)
    monkeypatch.setattr("ingest.search.embed_query", fake_embed_query)
    from api.main import app
    # lifespan 밖에서 작업을 직접 진행하여 중지와 완료의 순서를 통제한다.
    return cfg, TestClient(app)


def upload(client, payload=None):
    return client.put(f"/v1/corpora/easyflow/sources/{SID}", headers=header(WRITE), json=payload or document())


def test_service_scope_list_and_search(shared):
    _, client = shared
    assert [c["corpus"] for c in client.get("/v1/corpora", headers=header(COAI)).json()["corpora"]] == ["inventions"]
    assert client.get("/v1/corpora").status_code == 401
    assert client.post("/v1/corpora/easyflow/search", headers=header(COAI), json={"text": "업무"}).status_code == 404
    assert client.post("/v1/corpora/inventions/search", headers=header(READ), json={"text": "발명"}).status_code == 404
    assert client.put(f"/v1/corpora/easyflow/sources/{SID}", headers=header(READ), json=document()).status_code == 403


def test_legacy_token_is_limited_to_original_corpus(shared, monkeypatch):
    _, client = shared
    monkeypatch.setenv("RAG_API_TOKEN", "legacy-only-token")
    assert client.post("/v1/corpora/easyflow/search", headers=header("legacy-only-token"), json={"text": "업무"}).status_code == 404
    assert services.authenticate("Bearer legacy-only-token").allows("inventions")


def test_roundtrip_metadata_and_job_visibility(shared):
    cfg, client = shared
    response = upload(client)
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert response.json()["state"] == "queued"
    assert client.get(f"/v1/jobs/{job_id}", headers=header(COAI)).status_code == 404
    assert sources.process_next()
    status = client.get(f"/v1/jobs/{job_id}", headers=header(WRITE)).json()
    assert status["state"] == "succeeded"
    result = client.post("/v1/corpora/easyflow/search", headers=header(READ), json={"text": "지출 승인"})
    assert result.status_code == 200
    hit = result.json()["results"][0]
    assert hit["metadata"]["source_id"] == SID
    assert hit["metadata"]["source_version_id"] == V1
    assert hit["metadata"]["page"] == 3
    assert hit["title"] == "학교회계 지침"
    assert "source_path" not in hit and "operation_id" not in hit["metadata"]
    assert result.headers["X-Request-ID"]


def test_idempotency_and_conflict(shared):
    _, client = shared
    first = upload(client).json()
    assert upload(client).json()["job_id"] == first["job_id"]
    changed = document()
    changed["title"] = "다른 제목"
    assert upload(client, changed).status_code == 409
    assert upload(client, document(1, key="other-key")).status_code == 409


def test_metadata_only_change_replaces_version(shared):
    _, client = shared
    upload(client)
    sources.process_next()
    second = document(2, V2)
    second["title"] = "새로운 제목"
    upload(client, second)
    sources.process_next()
    result = client.post("/v1/corpora/easyflow/search", headers=header(READ), json={"text": "지침"}).json()
    assert result["results"][0]["title"] == "새로운 제목"
    assert result["results"][0]["metadata"]["source_version_id"] == V2


def test_disable_while_index_running_does_not_resurrect(shared, monkeypatch):
    cfg, client = shared
    job_id = upload(client).json()["job_id"]
    def disable(texts, config):
        response = client.patch(f"/v1/corpora/easyflow/sources/{SID}", headers=header(WRITE),
            json={"revision": 2, "idempotency_key": "disable", "enabled": False})
        assert response.status_code == 202
        return fake_embed_documents(texts, config)
    monkeypatch.setattr(embedder, "embed_documents", disable)
    sources.process_next()
    assert client.get(f"/v1/jobs/{job_id}", headers=header(WRITE)).json()["state"] == "superseded"
    assert search(cfg, "업무") == []


def test_failed_new_version_keeps_old(shared, monkeypatch):
    cfg, client = shared
    upload(client)
    sources.process_next()
    def unavailable(*args):
        raise RuntimeError("private provider detail")
    monkeypatch.setattr(embedder, "embed_documents", unavailable)
    job_id = upload(client, document(2, V2)).json()["job_id"]
    sources.process_next()
    assert search(cfg, "업무")[0]["metadata"]["source_version_id"] == V1
    status = client.get(f"/v1/jobs/{job_id}", headers=header(WRITE))
    assert status.json()["state"] == "failed"
    assert "private provider detail" not in status.text


def test_delete_is_immediate_and_retry_is_idempotent(shared):
    cfg, client = shared
    upload(client)
    sources.process_next()
    body = {"revision": 2, "idempotency_key": "delete", "enabled": False}
    response = client.request("DELETE", f"/v1/corpora/easyflow/sources/{SID}", headers=header(WRITE), json=body)
    assert response.status_code == 202
    assert search(cfg, "업무") == []
    sources.process_next()
    assert get_collection(cfg.active_collection).count() == 0
    again = client.request("DELETE", f"/v1/corpora/easyflow/sources/{SID}", headers=header(WRITE), json=body)
    assert again.json()["job_id"] == response.json()["job_id"]
    assert not cfg.docs_dir().joinpath(f"{SID}.md").exists()


def test_leased_job_is_recovered(shared):
    _, client = shared
    job_id = upload(client).json()["job_id"]
    with storage.transaction() as cur:
        cur.execute("UPDATE source_jobs SET state='running',lease_until=? WHERE id=?", (time.time()-1, job_id))
    sources.process_next()
    assert client.get(f"/v1/jobs/{job_id}", headers=header(WRITE)).json()["state"] == "succeeded"


def test_rate_limit_does_not_call_provider(shared, monkeypatch):
    _, client = shared
    services.register("coai", "coai-read", COAI, {"inventions": ["search"]}, 1)
    monkeypatch.setattr("api.main.search", lambda *args: [])
    assert client.post("/v1/corpora/inventions/search", headers=header(COAI), json={"text": "발명"}).status_code == 200
    response = client.post("/v1/corpora/inventions/search", headers=header(COAI), json={"text": "발명"})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0
    assert response.json()["error"]["code"] == "rate_limited"


def test_revoked_token_is_rejected(shared):
    _, client = shared
    with storage.transaction() as cur:
        cur.execute("UPDATE service_tokens SET active=0 WHERE token_id='coai-read'")
    assert client.get("/v1/corpora", headers=header(COAI)).status_code == 401


def test_validation_errors_do_not_echo_document_body(shared):
    _, client = shared
    body = document()
    body["pages"][0]["text"] = "sensitive-doc" * 30000
    response = upload(client, body)
    assert response.status_code == 422
    assert "sensitive-doc" not in response.text


def test_rebuild_preserves_page_and_version(shared):
    cfg, client = shared
    upload(client)
    sources.process_next()
    from ingest.rebuild import rebuild_corpus
    updated, stats = rebuild_corpus(cfg)
    assert updated.active_collection != cfg.active_collection
    assert stats["indexed_docs"] == 1
    result = search(updated, "지침")
    assert result[0]["metadata"]["source_version_id"] == V1
    assert result[0]["metadata"]["page"] == 3


def test_same_version_cannot_change_text(shared):
    _, client = shared
    upload(client)
    changed = document(2)
    changed["pages"][0]["text"] = "다른 원문" * 30
    response = upload(client, changed)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "version_conflict"


def test_corpus_readiness_is_isolated(shared, monkeypatch):
    _, client = shared
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("api.main.count_documents", lambda name: 42 if name.startswith("inventions") else 0)
    response = client.get("/v1/corpora/inventions/ready", headers=header(COAI))
    assert response.status_code == 200
    assert response.json()["corpus"] == "inventions"
    assert client.get("/v1/corpora/easyflow/ready", headers=header(COAI)).status_code == 404
    assert client.get("/v1/corpora/easyflow/ready", headers=header(READ)).status_code == 503


def test_index_completion_cannot_reenable_deleted_source(shared, monkeypatch):
    cfg, client = shared
    job_id = upload(client).json()["job_id"]
    def delete_during_index(texts, config):
        response = client.request("DELETE", f"/v1/corpora/easyflow/sources/{SID}", headers=header(WRITE),
            json={"revision": 2, "idempotency_key": "delete-during-index", "enabled": False})
        assert response.status_code == 202
        return fake_embed_documents(texts, config)
    monkeypatch.setattr(embedder, "embed_documents", delete_during_index)
    sources.process_next()
    assert client.get(f"/v1/jobs/{job_id}", headers=header(WRITE)).json()["state"] == "superseded"
    assert search(cfg, "업무") == []


def test_new_job_cannot_be_claimed_twice(shared):
    _, client = shared
    job_id = upload(client).json()["job_id"]
    with storage.transaction() as cur:
        cur.execute("UPDATE source_jobs SET state='running',lease_until=? WHERE id=?", (time.time()+300, job_id))
    assert not sources.process_next()


def test_oversized_document_request_is_rejected_before_validation(shared):
    _, client = shared
    response = client.put(f"/v1/corpora/easyflow/sources/{SID}", headers=header(WRITE), content=b'x'*(6*1024*1024+1))
    assert response.status_code == 413


def test_even_wrong_grant_cannot_mix_service_domains(shared):
    _, client = shared
    with pytest.raises(ValueError, match="혼용"):
        services.register("coai", "wrong-grant", COAI+"wrong", {"easyflow": ["search"]})
    with pytest.raises(ValueError, match="혼용"):
        services.register("easyflow", "wrong-grant", READ+"wrong", {"inventions": ["search"]})
    # 이전 DB에 잘못된 grant가 남아 있어도 런타임에서 보호한다.
    with storage.transaction() as cur:
        cur.execute('UPDATE service_tokens SET grants_json=? WHERE token_id=?', ('{"easyflow":["search"]}', 'coai-read'))
    assert client.post("/v1/corpora/easyflow/search", headers=header(COAI), json={"text":"업무"}).status_code == 404


def test_legacy_corpus_override_cannot_expose_easyflow(shared, monkeypatch):
    _, client = shared
    monkeypatch.setenv("RAG_API_TOKEN", "legacy")
    monkeypatch.setenv("RAG_LEGACY_CORPUS", "easyflow")
    assert client.post("/v1/corpora/easyflow/search", headers=header("legacy"), json={"text":"업무"}).status_code == 404
