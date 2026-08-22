"""어드민 인증 — 해시, 세션, CSRF, 서비스 토큰과의 분리."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from admin.auth import (
    AuthError,
    authenticate,
    clear_failures,
    create_user,
    get_by_username,
    hash_password,
    set_active,
    set_password,
    verify_password,
)


@pytest.fixture()
def app_client(seed_corpus):
    import api.main as api_main

    with TestClient(api_main.app) as client:
        yield client


def login(client, username="boss", password="test-password-1234"):
    """CSRF 토큰을 폼에서 꺼내 로그인한다."""
    page = client.get("/admin/login")
    token = _extract_csrf(page.text)
    return client.post(
        "/admin/login",
        data={"username": username, "password": password, "csrf": token, "next": "/admin/"},
        follow_redirects=False,
    )


def _extract_csrf(html: str) -> str:
    marker = 'name="csrf" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


# ---------------------------------------------------------------------------
# 비밀번호 해시
# ---------------------------------------------------------------------------

def test_hash_is_salted_and_verifiable():
    hash_a, salt_a = hash_password("동일한비밀번호123")
    hash_b, salt_b = hash_password("동일한비밀번호123")

    # 같은 비밀번호라도 salt가 달라 해시가 달라야 한다.
    assert salt_a != salt_b
    assert hash_a != hash_b
    assert verify_password("동일한비밀번호123", hash_a, salt_a)
    assert verify_password("동일한비밀번호123", hash_b, salt_b)


def test_verify_rejects_wrong_password():
    password_hash, salt = hash_password("올바른비밀번호123")
    assert not verify_password("틀린비밀번호123", password_hash, salt)


def test_verify_survives_malformed_salt():
    password_hash, _ = hash_password("비밀번호12345")
    assert not verify_password("비밀번호12345", password_hash, "not-hex")


def test_short_password_is_rejected():
    with pytest.raises(AuthError):
        create_user("someone", "short")


@pytest.mark.parametrize("bad", ["ab", "has space", "x" * 40, "!!!"])
def test_invalid_username_is_rejected(bad):
    with pytest.raises(AuthError):
        create_user(bad, "valid-password-1234")


# ---------------------------------------------------------------------------
# 로그인
# ---------------------------------------------------------------------------

def test_authenticate_success():
    create_user("boss", "test-password-1234")
    user = authenticate("boss", "test-password-1234")
    assert user.username == "boss"
    assert user.is_superadmin


def test_authenticate_rejects_wrong_password():
    create_user("boss", "test-password-1234")
    with pytest.raises(AuthError):
        authenticate("boss", "wrong-password-99")


def test_authenticate_rejects_unknown_user():
    with pytest.raises(AuthError):
        authenticate("nobody", "test-password-1234")


def test_authenticate_rejects_inactive_user():
    create_user("boss", "test-password-1234")
    staff = create_user("staff", "staff-password-1234")
    set_active(staff, False)

    with pytest.raises(AuthError):
        authenticate("staff", "staff-password-1234")


def test_repeated_failures_lock_out():
    create_user("boss", "test-password-1234")
    clear_failures("boss")

    for _ in range(8):
        with pytest.raises(AuthError):
            authenticate("boss", "wrong-password")

    # 잠긴 뒤에는 올바른 비밀번호도 거부된다.
    with pytest.raises(AuthError, match="너무 많"):
        authenticate("boss", "test-password-1234")
    clear_failures("boss")


# ---------------------------------------------------------------------------
# 세션
# ---------------------------------------------------------------------------

def test_login_flow_sets_session(app_client):
    create_user("boss", "test-password-1234")

    response = login(app_client)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/"
    assert app_client.get("/admin/").status_code == 200


def test_unauthenticated_is_redirected_to_login(app_client):
    response = app_client.get("/admin/", follow_redirects=False)
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


def test_logout_clears_session(app_client):
    create_user("boss", "test-password-1234")
    login(app_client)

    page = app_client.get("/admin/")
    app_client.post("/admin/logout", data={"csrf": _extract_csrf(page.text)})

    assert app_client.get("/admin/", follow_redirects=False).status_code == 303


def test_password_change_invalidates_existing_session(app_client):
    """비밀번호를 바꾸면 다른 기기의 로그인도 끊겨야 한다."""
    create_user("boss", "test-password-1234")
    login(app_client)
    assert app_client.get("/admin/").status_code == 200

    set_password(get_by_username("boss"), "new-password-56789")

    assert app_client.get("/admin/", follow_redirects=False).status_code == 303


def test_deactivation_invalidates_existing_session(app_client):
    create_user("boss", "test-password-1234")
    staff = create_user("staff", "staff-password-1234")
    login(app_client, "staff", "staff-password-1234")
    assert app_client.get("/admin/").status_code == 200

    set_active(get_by_username("staff"), False)

    assert app_client.get("/admin/", follow_redirects=False).status_code == 303


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def test_login_without_csrf_is_rejected(app_client):
    create_user("boss", "test-password-1234")
    app_client.get("/admin/login")

    response = app_client.post(
        "/admin/login",
        data={"username": "boss", "password": "test-password-1234"},
    )

    assert response.status_code == 403


def test_post_with_wrong_csrf_is_rejected(app_client, plain_corpus):
    create_user("boss", "test-password-1234")
    login(app_client)

    response = app_client.post(
        f"/admin/corpora/{plain_corpus.id}/reindex",
        data={"csrf": "forged-token", "mode": "incremental"},
    )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 서비스 토큰과의 분리
# ---------------------------------------------------------------------------

def test_bearer_token_does_not_grant_admin_access(app_client, monkeypatch):
    """Rails용 서비스 토큰으로 어드민 화면에 들어올 수 없어야 한다."""
    monkeypatch.setenv("RAG_API_TOKEN", "service-token-value")
    create_user("boss", "test-password-1234")

    response = app_client.get(
        "/admin/",
        headers={"Authorization": "Bearer service-token-value"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


def test_admin_session_does_not_grant_search_api_access(app_client, monkeypatch):
    """반대 방향도 마찬가지 — 어드민 세션이 검색 API 인증을 대신하지 않는다."""
    monkeypatch.setenv("RAG_API_TOKEN", "service-token-value")
    create_user("boss", "test-password-1234")
    login(app_client)

    response = app_client.post(
        "/v1/corpora/inventions/search", json={"text": "질의"}
    )

    assert response.status_code == 401
