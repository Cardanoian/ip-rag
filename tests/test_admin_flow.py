"""어드민 전체 흐름 — corpus 만들기 → 업로드 → 색인 → 공개 → 검색.

관리자가 브라우저에서 실제로 밟는 경로를 그대로 따라간다.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import corpora
from admin import jobs
from admin.auth import create_user
from tests.test_admin_auth import _extract_csrf, login

REGULATION_BODY = (
    "학교폭력이란 학교 내외에서 학생을 대상으로 발생한 상해, 폭행, 감금, 협박, "
    "약취·유인, 명예훼손·모욕, 공갈, 강요·강제적인 심부름 및 성폭력, 따돌림, "
    "사이버 따돌림, 정보통신망을 이용한 음란·폭력 정보 등에 의하여 신체·정신 "
    "또는 재산상의 피해를 수반하는 행위를 말한다.\n"
) * 3

WELFARE_BODY = (
    "학생맞춤형 통합복지는 경제적 어려움을 겪는 학생에게 교육비, 급식비, "
    "학용품비를 지원하는 제도입니다. 신청은 담임교사를 통해 접수하며 "
    "가구 소득인정액을 기준으로 지원 대상을 선정합니다.\n"
) * 3


@pytest.fixture(autouse=True)
def clean_executor():
    yield
    jobs.shutdown(wait=True)


@pytest.fixture()
def client(seed_corpus, patch_embed):
    import api.main as api_main

    create_user("boss", "boss-password-1234")
    with TestClient(api_main.app) as c:
        login(c, "boss", "boss-password-1234")
        yield c


def _csrf(client, path: str = "/admin/") -> str:
    return _extract_csrf(client.get(path).text)


def _create_corpus(client, slug: str, label: str) -> None:
    response = client.post(
        "/admin/corpora/new",
        data={
            "csrf": _csrf(client, "/admin/corpora/new"),
            "corpus_slug": slug,
            "label": label,
            "kind": "plain",
            "corpus_id": slug,
            "doc_prefix": f"[검색 대상 문서] 다음은 {label} 문서입니다.",
            "query_prefix": f"[검색 질의] {label} 관련 내용을 찾습니다.",
            "embed_dim": 1536,
            "chunk_size": 1000,
            "chunk_overlap": 150,
            "single_chunk_char_hint": 5500,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def _upload(client, slug: str, filename: str, body: str):
    return client.post(
        f"/admin/corpora/{slug}/documents",
        data={"csrf": _csrf(client, f"/admin/corpora/{slug}")},
        files={"files": (filename, body.encode("utf-8"), "text/markdown")},
        follow_redirects=False,
    )


def _reindex_and_wait(client, slug: str, mode: str = "incremental") -> jobs.Job:
    response = client.post(
        f"/admin/corpora/{slug}/reindex",
        data={"csrf": _csrf(client, f"/admin/corpora/{slug}"), "mode": mode},
        follow_redirects=False,
    )
    assert response.status_code == 303

    job = jobs.list_for_corpus(slug)[0]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = jobs.get(job.id)
        if not current.is_active:
            return current
        time.sleep(0.05)
    raise AssertionError("색인 잡이 끝나지 않았습니다")


# ---------------------------------------------------------------------------
# 전체 왕복
# ---------------------------------------------------------------------------

def test_full_corpus_lifecycle_through_admin(client):
    """관리자가 새 RAG를 만들어 서비스에 올리는 전 과정."""
    # 1) corpus 생성 — 초안 상태
    _create_corpus(client, "school-violence", "학교폭력 관련 규정")
    cfg = corpora.get("school-violence")
    assert cfg.status == corpora.STATUS_DRAFT

    # 2) 공개 전에는 검색 API에서 보이지 않는다
    assert client.post(
        "/v1/corpora/school-violence/search", json={"text": "학교폭력"}
    ).status_code == 404
    assert "school-violence" not in client.get("/v1/corpora").text

    # 3) 문서 업로드
    assert _upload(
        client, "school-violence", "학교폭력예방법 제2조.md", REGULATION_BODY
    ).status_code == 303
    detail = client.get("/admin/corpora/school-violence")
    assert "학교폭력예방법 제2조.md" in detail.text

    # 4) 색인
    job = _reindex_and_wait(client, "school-violence")
    assert job.status == jobs.STATUS_SUCCEEDED
    assert job.stats["indexed_docs"] == 1

    # 5) 공개
    response = client.post(
        "/admin/corpora/school-violence/publish",
        data={"csrf": _csrf(client, "/admin/corpora/school-violence")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert corpora.get("school-violence").status == corpora.STATUS_PUBLISHED

    # 6) 이제 검색 API에서 조회된다
    search = client.post(
        "/v1/corpora/school-violence/search", json={"text": "학교폭력이란 무엇인가"}
    )
    assert search.status_code == 200
    body = search.json()
    assert body["count"] >= 1
    assert body["results"][0]["title"] == "학교폭력예방법 제2조"
    assert body["corpus"] == "school-violence"

    # 7) 목록에도 나온다
    listing = client.get("/v1/corpora").json()
    names = {item["corpus"] for item in listing["corpora"]}
    assert names == {"inventions", "school-violence"}


def test_admin_repairs_legacy_zip_filenames_and_requires_rebuild(client):
    _create_corpus(client, "school-violence", "학교폭력 규정")
    cfg = corpora.get("school-violence")
    correct_name = "학교폭력 정의.md"
    broken_name = correct_name.encode("cp949").decode("cp437")
    broken_path = cfg.docs_dir() / broken_name
    broken_path.write_text(REGULATION_BODY, encoding="utf-8")

    detail = client.get("/admin/corpora/school-violence")
    assert "한글 파일명 복구" in detail.text

    response = client.post(
        "/admin/corpora/school-violence/documents/repair-filenames",
        data={"csrf": _csrf(client, "/admin/corpora/school-violence")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert not broken_path.exists()
    assert (cfg.docs_dir() / correct_name).exists()
    assert corpora.get("school-violence").needs_rebuild is True
    repaired_page = client.get(response.headers["location"])
    assert "한글 파일명 1개를 복구했습니다" in repaired_page.text


def test_two_corpora_do_not_bleed_into_each_other(client):
    """서로 다른 주제의 corpus가 한 서버에서 독립적으로 동작해야 한다."""
    for slug, label, body, filename in [
        ("school-violence", "학교폭력 규정", REGULATION_BODY, "학교폭력 정의.md"),
        ("welfare", "학생맞춤형 통합복지", WELFARE_BODY, "교육비 지원 안내.md"),
    ]:
        _create_corpus(client, slug, label)
        _upload(client, slug, filename, body)
        assert _reindex_and_wait(client, slug).status == jobs.STATUS_SUCCEEDED
        client.post(
            f"/admin/corpora/{slug}/publish",
            data={"csrf": _csrf(client, f"/admin/corpora/{slug}")},
            follow_redirects=False,
        )

    violence = client.post(
        "/v1/corpora/school-violence/search", json={"text": "질의"}
    ).json()
    welfare = client.post(
        "/v1/corpora/welfare/search", json={"text": "질의"}
    ).json()

    assert {r["title"] for r in violence["results"]} == {"학교폭력 정의"}
    assert {r["title"] for r in welfare["results"]} == {"교육비 지원 안내"}
    assert violence["index_version"] != welfare["index_version"]


# ---------------------------------------------------------------------------
# 검색 콘솔
# ---------------------------------------------------------------------------

def test_console_works_on_draft_corpus(client):
    """공개 전에 색인 품질을 확인할 수 있어야 한다."""
    _create_corpus(client, "school-violence", "학교폭력 규정")
    _upload(client, "school-violence", "학교폭력 정의.md", REGULATION_BODY)
    _reindex_and_wait(client, "school-violence")

    console_path = "/admin/corpora/school-violence/console"
    response = client.post(
        console_path,
        data={
            "csrf": _csrf(client, console_path),
            "query": "학교폭력의 정의",
            "top_k": 5,
        },
    )

    assert response.status_code == 200
    assert "학교폭력 정의" in response.text
    # 아직 검색 API에는 열려 있지 않다.
    assert corpora.get("school-violence").status == corpora.STATUS_DRAFT


# ---------------------------------------------------------------------------
# 문서 삭제와 재색인
# ---------------------------------------------------------------------------

def test_deleting_document_removes_it_from_search(client):
    _create_corpus(client, "school-violence", "학교폭력 규정")
    _upload(client, "school-violence", "학교폭력 정의.md", REGULATION_BODY)
    _upload(client, "school-violence", "신고 절차.md", REGULATION_BODY.replace("학교폭력이란", "신고는"))
    _reindex_and_wait(client, "school-violence")
    client.post(
        "/admin/corpora/school-violence/publish",
        data={"csrf": _csrf(client, "/admin/corpora/school-violence")},
        follow_redirects=False,
    )

    before = client.post(
        "/v1/corpora/school-violence/search", json={"text": "질의", "top_k": 10}
    ).json()
    assert before["count"] == 2

    client.post(
        "/admin/corpora/school-violence/documents/delete",
        data={
            "csrf": _csrf(client, "/admin/corpora/school-violence"),
            "filenames": ["신고 절차.md"],
        },
        follow_redirects=False,
    )

    after = client.post(
        "/v1/corpora/school-violence/search", json={"text": "질의", "top_k": 10}
    ).json()
    assert {r["title"] for r in after["results"]} == {"학교폭력 정의"}


def test_delete_all_documents_has_no_document_count_form_limit(client):
    from admin.documents import list_documents
    from ingest.store import count_documents

    _create_corpus(client, "school-violence", "학교폭력 규정")
    for number in range(25):
        _upload(
            client,
            "school-violence",
            f"학교폭력 규정 {number}.md",
            f"{number}\n{REGULATION_BODY}",
        )
    _reindex_and_wait(client, "school-violence")
    cfg = corpora.update(corpora.get("school-violence"), needs_rebuild=True)
    assert count_documents(cfg.active_collection) > 0

    path = "/admin/corpora/school-violence/documents/delete-all"
    rejected = client.post(
        path,
        data={
            "csrf": _csrf(client, "/admin/corpora/school-violence"),
            "confirm": "wrong",
        },
        follow_redirects=False,
    )
    assert rejected.status_code == 303
    assert len(list_documents(cfg)) == 25

    response = client.post(
        path,
        data={
            "csrf": _csrf(client, "/admin/corpora/school-violence"),
            "confirm": "school-violence",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert list_documents(cfg) == []
    assert count_documents(cfg.active_collection) == 0
    assert corpora.get("school-violence").needs_rebuild is False


# ---------------------------------------------------------------------------
# 무중단 재색인
# ---------------------------------------------------------------------------

def test_search_keeps_working_across_rebuild(client):
    """전체 재색인을 돌려도 검색이 끊기지 않아야 한다."""
    _create_corpus(client, "school-violence", "학교폭력 규정")
    _upload(client, "school-violence", "학교폭력 정의.md", REGULATION_BODY)
    _reindex_and_wait(client, "school-violence")
    client.post(
        "/admin/corpora/school-violence/publish",
        data={"csrf": _csrf(client, "/admin/corpora/school-violence")},
        follow_redirects=False,
    )
    before_version = corpora.get("school-violence").index_version

    job = _reindex_and_wait(client, "school-violence", mode="rebuild")
    assert job.status == jobs.STATUS_SUCCEEDED

    after = corpora.get("school-violence")
    assert after.active_collection == "school-violence_v2"
    assert after.index_version != before_version

    response = client.post(
        "/v1/corpora/school-violence/search", json={"text": "학교폭력"}
    )
    assert response.status_code == 200
    assert response.json()["count"] >= 1


def test_settings_change_flags_rebuild_needed(client):
    _create_corpus(client, "school-violence", "학교폭력 규정")
    _upload(client, "school-violence", "학교폭력 정의.md", REGULATION_BODY)
    _reindex_and_wait(client, "school-violence")

    settings_path = "/admin/corpora/school-violence/settings"
    response = client.post(
        settings_path,
        data={
            "csrf": _csrf(client, settings_path),
            "label": "학교폭력 규정",
            "corpus_id_value": "school-violence",
            "doc_prefix": "[검색 대상 문서] 완전히 다른 지시문입니다.",
            "query_prefix": "[검색 질의] 관련 내용을 찾습니다.",
            "embed_dim": 1536,
            "chunk_size": 1000,
            "chunk_overlap": 150,
            "single_chunk_char_hint": 5500,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert corpora.get("school-violence").needs_rebuild is True


def test_embed_dim_change_queues_rebuild_immediately(client):
    """차원이 바뀌면 검색이 즉시 깨지므로 재색인을 자동으로 건다."""
    _create_corpus(client, "school-violence", "학교폭력 규정")
    _upload(client, "school-violence", "학교폭력 정의.md", REGULATION_BODY)
    _reindex_and_wait(client, "school-violence")

    settings_path = "/admin/corpora/school-violence/settings"
    client.post(
        settings_path,
        data={
            "csrf": _csrf(client, settings_path),
            "label": "학교폭력 규정",
            "corpus_id_value": "school-violence",
            "doc_prefix": "[검색 대상 문서] 다음은 학교폭력 규정 문서입니다.",
            "query_prefix": "[검색 질의] 학교폭력 규정 관련 내용을 찾습니다.",
            "embed_dim": 768,
            "chunk_size": 1000,
            "chunk_overlap": 150,
            "single_chunk_char_hint": 5500,
        },
        follow_redirects=False,
    )

    history = jobs.list_for_corpus("school-violence")
    assert any(job.kind == jobs.KIND_REBUILD for job in history)


# ---------------------------------------------------------------------------
# 완전삭제
# ---------------------------------------------------------------------------

def test_destroy_removes_files_and_collections(client):
    from ingest.store import collection_exists

    _create_corpus(client, "school-violence", "학교폭력 규정")
    _upload(client, "school-violence", "학교폭력 정의.md", REGULATION_BODY)
    _reindex_and_wait(client, "school-violence")

    cfg = corpora.get("school-violence")
    docs_dir = cfg.docs_dir()
    assert docs_dir.exists()
    assert collection_exists(cfg.active_collection)

    # 비공개를 거쳐야 삭제할 수 있다
    client.post(
        "/admin/corpora/school-violence/unpublish",
        data={"csrf": _csrf(client, "/admin/corpora/school-violence")},
        follow_redirects=False,
    )
    response = client.post(
        "/admin/corpora/school-violence/destroy",
        data={
            "csrf": _csrf(client, "/admin/corpora/school-violence"),
            "confirm": "school-violence",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert not docs_dir.exists()
    assert not collection_exists(cfg.active_collection)
    with pytest.raises(corpora.CorpusNotFound):
        corpora.get("school-violence")

    # 시드 corpus는 멀쩡하다
    assert corpora.get("inventions").status == corpora.STATUS_PUBLISHED


# ---------------------------------------------------------------------------
# 기존 연동 회귀
# ---------------------------------------------------------------------------

def test_legacy_endpoints_still_serve_inventions(client, monkeypatch):
    """새 corpus를 추가해도 기존 Rails 연동이 깨지면 안 된다."""
    import api.main as api_main

    _create_corpus(client, "school-violence", "학교폭력 규정")

    fake = [{
        "document_id": "d" * 24,
        "title": "발명품",
        "source_path": "inventions/x.md",
        "similarity": 0.9,
        "snippet": "발췌",
        "metadata": {"title": "발명품", "year": 2001, "category": "과학완구",
                     "doc_type": "작품설명서"},
        "_raw_metadata": {"title": "발명품", "year": 2001, "category": "과학완구",
                          "author": "학생", "doc_type": "작품설명서"},
    }]
    monkeypatch.setattr(api_main, "search", lambda cfg, t, k, o: fake)

    v1 = client.post("/v1/search", json={"text": "발명 아이디어"})
    assert v1.status_code == 200
    assert v1.json()["results"][0]["year"] == 2001

    legacy = client.post("/search", json={"text": "발명 아이디어"})
    assert legacy.status_code == 200
    assert legacy.json()["results"][0]["author"] == "학생"
