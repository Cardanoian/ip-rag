"""신뢰된 서버의 서비스 토큰을 corpus별 권한 주체로 검증한다."""
from fastapi import Header, Request
from api.services import Principal, authenticate


def require_service_token(request: Request, authorization: str | None = Header(default=None)) -> Principal:
    principal = authenticate(authorization)
    request.state.service_id = principal.service_id
    return principal
