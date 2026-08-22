"""세션 인증·권한 게이트·CSRF.

서명 쿠키에는 사용자 id와 session_version만 담는다. 권한은 매 요청 DB에서 다시 읽으므로
계정을 비활성화하거나 비밀번호를 리셋하면 로그인 중인 세션도 즉시 끊긴다.
"""
from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

from admin.auth import AdminUser, get_by_id

_SESSION_USER_ID = "admin_user_id"
_SESSION_VERSION = "admin_session_version"
_SESSION_CSRF = "admin_csrf"


class NotAuthenticated(Exception):
    """로그인이 필요하다. 예외 핸들러가 로그인 화면으로 보낸다."""

    def __init__(self, next_url: str = "/admin/"):
        self.next_url = next_url


def login_session(request: Request, user: AdminUser) -> None:
    request.session.clear()
    request.session[_SESSION_USER_ID] = user.id
    request.session[_SESSION_VERSION] = user.session_version
    request.session[_SESSION_CSRF] = secrets.token_urlsafe(32)


def logout_session(request: Request) -> None:
    request.session.clear()


def current_user(request: Request) -> AdminUser | None:
    """세션이 가리키는 사용자. 무효한 세션이면 None이며 쿠키를 비운다."""
    user_id = request.session.get(_SESSION_USER_ID)
    if not user_id:
        return None

    user = get_by_id(user_id)
    if user is None or not user.is_active:
        request.session.clear()
        return None
    if user.session_version != request.session.get(_SESSION_VERSION):
        # 비밀번호 변경·비활성화로 세션이 무효화됐다.
        request.session.clear()
        return None
    return user


def require_admin(request: Request) -> AdminUser:
    """로그인한 관리자 — 문서·색인·콘솔 등 대부분의 라우트에 적용한다."""
    user = current_user(request)
    if user is None:
        raise NotAuthenticated(next_url=str(request.url.path))
    return user


def require_superadmin(request: Request) -> AdminUser:
    """최고관리자 전용 — 계정 관리와 corpus 완전삭제."""
    user = require_admin(request)
    if not user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="최고관리자만 사용할 수 있는 기능입니다.",
        )
    return user


def csrf_token(request: Request) -> str:
    token = request.session.get(_SESSION_CSRF)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[_SESSION_CSRF] = token
    return token


def verify_csrf(request: Request, submitted: str | None) -> None:
    expected = request.session.get(_SESSION_CSRF)
    if not expected or not submitted or not secrets.compare_digest(
        submitted, expected
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="요청이 만료되었습니다. 화면을 새로고침한 뒤 다시 시도하세요.",
        )
