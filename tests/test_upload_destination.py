"""관리자가 선택한 corpus에만 저장·색인하며 EasyFlow 직접 업로드를 막는다."""
import pytest

from admin import jobs
from admin.auth import create_user
from tests.test_admin_auth import login, _extract_csrf
from tests.test_service_integration import shared


@pytest.fixture()
def admin_client(shared, plain_corpus, patch_embed):
    cfg, client = shared
    create_user("boss", "boss-password-1234")
    login(client, "boss", "boss-password-1234")
    yield cfg, plain_corpus, client
    jobs.shutdown(wait=True)


def csrf(client):
    return _extract_csrf(client.get("/admin/upload").text)


def test_selector_lists_allowed_manual_destinations_only(admin_client):
    _, _, client = admin_client
    response = client.get("/admin/upload")
    assert response.status_code == 200
    assert 'name="target_corpus" required' in response.text
    assert '<option value="inventions">' in response.text
    assert '<option value="rules">' in response.text
    assert '<option value="easyflow">' not in response.text


def test_selected_corpus_receives_file_and_index_job(admin_client):
    protected, target, client = admin_client
    response = client.post("/admin/documents/upload", data={"csrf": csrf(client), "target_corpus": "rules", "index_after_upload": "true"},
                           files={"files": ("규정.md", ("학교 규정 본문입니다. " * 20).encode(), "text/markdown")}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/corpora/rules"
    jobs.shutdown(wait=True)
    assert (target.docs_dir() / "규정.md").exists()
    assert not (protected.docs_dir() / "규정.md").exists()
    job = jobs.list_for_corpus("rules")[0]
    assert job.status == "succeeded"
    assert job.stats["indexed_docs"] == 1
    assert not jobs.list_for_corpus("inventions")


def test_tampered_easyflow_destination_is_rejected(admin_client):
    cfg, _, client = admin_client
    response = client.post("/admin/documents/upload", data={"csrf": csrf(client), "target_corpus": "easyflow"},
                           files={"files": ("문서.md", b"private text"*20, "text/markdown")})
    assert response.status_code == 200
    assert "EasyFlow에서 등록" in response.text
    assert not list(cfg.docs_dir().glob("*.md"))
    assert not jobs.list_for_corpus("easyflow")


def test_unknown_destination_never_uses_default_corpus(admin_client):
    _, _, client = admin_client
    response = client.post("/admin/documents/upload", data={"csrf": csrf(client), "target_corpus": "does-not-exist"},
                           files={"files": ("문서.md", b"text"*20, "text/markdown")})
    assert response.status_code == 404
    assert not jobs.list_recent()


def test_detail_upload_also_honors_explicit_destination(admin_client):
    cfg, target, client = admin_client
    response = client.post("/admin/corpora/inventions/documents", data={"csrf": csrf(client), "target_corpus": "rules"},
                           files={"files": ("선택.md", ("검증 본문"*20).encode(), "text/markdown")}, follow_redirects=False)
    assert response.headers["location"] == "/admin/corpora/rules"
    assert (target.docs_dir() / "선택.md").exists()
    assert not (cfg.docs_dir() / "선택.md").exists()
