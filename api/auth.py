"""Rails 등 신뢰된 서버만 검색 API를 호출하도록 하는 서비스 인증."""
from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException, status


def require_service_token(authorization: str | None = Header(default=None)) -> None:
    """Bearer 토큰을 상수 시간 비교한다.

    개발 환경에서는 RAG_API_TOKEN이 없으면 인증을 생략한다. production에서는
    토큰 미설정 자체를 준비되지 않은 서버로 취급해 503을 반환한다.
    """
    expected = os.getenv("RAG_API_TOKEN", "").strip()
    app_env = os.getenv("APP_ENV", "development").strip().lower()

    if not expected:
        if app_env in {"production", "prod"}:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="서비스 인증이 구성되지 않았습니다.",
            )
        return

    scheme, separator, supplied = (authorization or "").partition(" ")
    valid = (
        bool(separator)
        and scheme.lower() == "bearer"
        and secrets.compare_digest(supplied, expected)
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효한 서비스 토큰이 필요합니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )
