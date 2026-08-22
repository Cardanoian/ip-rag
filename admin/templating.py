"""Jinja2 렌더링 헬퍼 — 공통 컨텍스트와 플래시 메시지."""
from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from admin.permissions import csrf_token, current_user

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_FLASH_KEY = "_flash"


def flash(request: Request, message: str, level: str = "info") -> None:
    """다음 렌더에 한 번 표시할 메시지를 세션에 담는다."""
    messages = request.session.get(_FLASH_KEY, [])
    messages.append({"level": level, "message": message})
    request.session[_FLASH_KEY] = messages


def _pop_flashes(request: Request) -> list[dict]:
    messages = request.session.pop(_FLASH_KEY, [])
    return messages


def render(
    request: Request,
    template_name: str,
    context: dict | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    payload = {
        "request": request,
        "user": current_user(request),
        "csrf_token": csrf_token(request),
        "flashes": _pop_flashes(request),
        **(context or {}),
    }
    return templates.TemplateResponse(
        request, template_name, payload, status_code=status_code
    )


def humanize_bytes(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


templates.env.filters["humanize_bytes"] = humanize_bytes
