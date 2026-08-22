"""관리자 계층 — 최고관리자 1명 불변식과 락아웃 방지."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import storage
from admin.auth import (
    ROLE_ADMIN,
    ROLE_SUPERADMIN,
    AuthError,
    create_user,
    delete_user,
    get_by_username,
    has_superadmin,
    list_users,
    set_active,
    transfer_superadmin,
)
from tests.test_admin_auth import _extract_csrf, login


@pytest.fixture()
def app_client(seed_corpus):
    import api.main as api_main

    with TestClient(api_main.app) as client:
        yield client


@pytest.fixture()
def boss():
    return create_user("boss", "boss-password-1234")


@pytest.fixture()
def staff(boss):
    return create_user("staff", "staff-password-1234")


# ---------------------------------------------------------------------------
# 최고관리자 1명 불변식
# ---------------------------------------------------------------------------

def test_first_user_becomes_superadmin():
    user = create_user("first", "first-password-1234")
    assert user.role == ROLE_SUPERADMIN


def test_second_user_becomes_admin(boss):
    user = create_user("second", "second-password-1234")
    assert user.role == ROLE_ADMIN


def test_explicit_second_superadmin_is_refused(boss):
    with pytest.raises(AuthError, match="1명"):
        create_user("rival", "rival-password-1234", role=ROLE_SUPERADMIN)


def test_database_rejects_second_superadmin_directly(boss):
    """애플리케이션 검증을 우회해도 DB의 partial unique index가 막는다."""
    with pytest.raises(sqlite3.IntegrityError):
        with storage.transaction() as cur:
            cur.execute(
                "INSERT INTO admin_users "
                "(username, password_hash, salt, role, is_active, session_version, created_at) "
                "VALUES ('sneaky', 'x', 'y', 'superadmin', 1, 1, '2026-01-01')"
            )

    assert len([u for u in list_users() if u.is_superadmin]) == 1


# ---------------------------------------------------------------------------
# 락아웃 방지
# ---------------------------------------------------------------------------

def test_superadmin_cannot_be_deleted(boss):
    with pytest.raises(AuthError):
        delete_user(boss)
    assert has_superadmin()


def test_superadmin_cannot_be_deactivated(boss):
    with pytest.raises(AuthError):
        set_active(boss, False)
    assert get_by_username("boss").is_active


def test_transfer_requires_active_target(boss, staff):
    set_active(staff, False)
    with pytest.raises(AuthError):
        transfer_superadmin(boss, get_by_username("staff"))


def test_transfer_to_self_is_refused(boss):
    with pytest.raises(AuthError):
        transfer_superadmin(boss, boss)


def test_non_superadmin_cannot_transfer(boss, staff):
    with pytest.raises(AuthError):
        transfer_superadmin(staff, boss)


# ---------------------------------------------------------------------------
# 이양
# ---------------------------------------------------------------------------

def test_transfer_swaps_roles_atomically(boss, staff):
    transfer_superadmin(boss, staff)

    assert get_by_username("staff").role == ROLE_SUPERADMIN
    assert get_by_username("boss").role == ROLE_ADMIN
    # 언제나 정확히 1명이다.
    assert len([u for u in list_users() if u.is_superadmin]) == 1


def test_transfer_can_be_handed_back(boss, staff):
    transfer_superadmin(boss, staff)
    transfer_superadmin(get_by_username("staff"), get_by_username("boss"))

    assert get_by_username("boss").role == ROLE_SUPERADMIN
    assert len([u for u in list_users() if u.is_superadmin]) == 1


def test_failed_transfer_leaves_roles_unchanged(boss, staff, monkeypatch):
    """트랜잭션 중간에 실패하면 원래 상태로 돌아가야 한다."""
    real_transaction = storage.transaction
    calls = {"n": 0}

    class ExplodingCursor:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:  # 대상 승격 직전에 터뜨린다
                raise sqlite3.OperationalError("모의 장애")
            return self._cur.execute(*args, **kwargs)

    from contextlib import contextmanager

    @contextmanager
    def flaky_transaction():
        with real_transaction() as cur:
            yield ExplodingCursor(cur)

    monkeypatch.setattr(storage, "transaction", flaky_transaction)

    with pytest.raises(sqlite3.OperationalError):
        transfer_superadmin(boss, staff)

    # undo()는 conftest의 경로 격리까지 되돌리므로 이 patch만 직접 복원한다.
    monkeypatch.setattr(storage, "transaction", real_transaction)
    assert get_by_username("boss").role == ROLE_SUPERADMIN
    assert get_by_username("staff").role == ROLE_ADMIN


# ---------------------------------------------------------------------------
# 라우트 권한 게이트
# ---------------------------------------------------------------------------

SUPERADMIN_ONLY_GETS = ["/admin/users", "/admin/audit"]
SHARED_GETS = ["/admin/", "/admin/corpora/new", "/admin/account/password"]


@pytest.mark.parametrize("path", SUPERADMIN_ONLY_GETS)
def test_admin_cannot_reach_superadmin_pages(app_client, boss, staff, path):
    login(app_client, "staff", "staff-password-1234")
    assert app_client.get(path).status_code == 403


@pytest.mark.parametrize("path", SUPERADMIN_ONLY_GETS)
def test_superadmin_can_reach_superadmin_pages(app_client, boss, path):
    login(app_client, "boss", "boss-password-1234")
    assert app_client.get(path).status_code == 200


@pytest.mark.parametrize("path", SHARED_GETS)
def test_admin_can_reach_shared_pages(app_client, boss, staff, path):
    """색인 관련 작업은 두 역할이 동등하다."""
    login(app_client, "staff", "staff-password-1234")
    assert app_client.get(path).status_code == 200


def test_admin_can_create_corpus(app_client, boss, staff):
    login(app_client, "staff", "staff-password-1234")
    page = app_client.get("/admin/corpora/new")

    response = app_client.post(
        "/admin/corpora/new",
        data={
            "csrf": _extract_csrf(page.text),
            "corpus_slug": "welfare",
            "label": "학생 복지",
            "kind": "plain",
            "corpus_id": "student-welfare",
            "doc_prefix": "[검색 대상 문서] 복지 안내입니다.",
            "query_prefix": "[검색 질의] 복지 정보를 찾습니다.",
            "embed_dim": 1536,
            "chunk_size": 1000,
            "chunk_overlap": 150,
            "single_chunk_char_hint": 5500,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    import corpora

    assert corpora.get("welfare").created_by == "staff"


def test_admin_cannot_destroy_corpus(app_client, boss, staff, plain_corpus):
    import corpora

    cfg = corpora.set_status(plain_corpus, corpora.STATUS_UNPUBLISHED)
    login(app_client, "staff", "staff-password-1234")
    page = app_client.get(f"/admin/corpora/{cfg.id}")

    response = app_client.post(
        f"/admin/corpora/{cfg.id}/destroy",
        data={"csrf": _extract_csrf(page.text), "confirm": cfg.id},
    )

    assert response.status_code == 403
    assert corpora.get(cfg.id) is not None


def test_superadmin_can_destroy_corpus(app_client, boss, plain_corpus):
    import corpora

    cfg = corpora.set_status(plain_corpus, corpora.STATUS_UNPUBLISHED)
    login(app_client, "boss", "boss-password-1234")
    page = app_client.get(f"/admin/corpora/{cfg.id}")

    response = app_client.post(
        f"/admin/corpora/{cfg.id}/destroy",
        data={"csrf": _extract_csrf(page.text), "confirm": cfg.id},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with pytest.raises(corpora.CorpusNotFound):
        corpora.get(cfg.id)


def test_destroy_requires_exact_confirmation(app_client, boss, plain_corpus):
    import corpora

    cfg = corpora.set_status(plain_corpus, corpora.STATUS_UNPUBLISHED)
    login(app_client, "boss", "boss-password-1234")
    page = app_client.get(f"/admin/corpora/{cfg.id}")

    response = app_client.post(
        f"/admin/corpora/{cfg.id}/destroy",
        data={"csrf": _extract_csrf(page.text), "confirm": "틀린값"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert corpora.get(cfg.id) is not None  # 삭제되지 않았다


# ---------------------------------------------------------------------------
# 계정 관리 라우트
# ---------------------------------------------------------------------------

def test_superadmin_cannot_delete_self_via_route(app_client, boss):
    login(app_client, "boss", "boss-password-1234")
    page = app_client.get("/admin/users")

    app_client.post(
        f"/admin/users/{boss.id}/delete",
        data={"csrf": _extract_csrf(page.text)},
        follow_redirects=False,
    )

    assert get_by_username("boss") is not None


def test_transfer_route_requires_confirmation(app_client, boss, staff):
    login(app_client, "boss", "boss-password-1234")
    page = app_client.get("/admin/users")

    app_client.post(
        f"/admin/users/{staff.id}/transfer-superadmin",
        data={"csrf": _extract_csrf(page.text), "confirm": "틀린아이디"},
        follow_redirects=False,
    )

    assert get_by_username("boss").role == ROLE_SUPERADMIN


def test_transfer_route_demotes_current_superadmin(app_client, boss, staff):
    login(app_client, "boss", "boss-password-1234")
    page = app_client.get("/admin/users")

    response = app_client.post(
        f"/admin/users/{staff.id}/transfer-superadmin",
        data={"csrf": _extract_csrf(page.text), "confirm": "staff"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert get_by_username("staff").role == ROLE_SUPERADMIN
    assert get_by_username("boss").role == ROLE_ADMIN
    # 이양 직후 본인은 계정 관리 화면에 들어갈 수 없다.
    assert app_client.get("/admin/users").status_code == 403
