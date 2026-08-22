"""감사 로그 — 누가 무엇을 바꿨는지 남긴다.

색인은 되돌리기 어렵고 계정 권한은 민감하다. 사후에 추적할 수 있어야 한다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditEntry:
    id: int
    actor: str | None
    action: str
    target: str | None
    detail: dict
    created_at: str


def record(
    actor: str | None,
    action: str,
    target: str | None = None,
    **detail,
) -> None:
    """감사 기록. 로깅 실패가 본 작업을 막지 않도록 예외를 삼킨다."""
    try:
        with storage.transaction() as cur:
            cur.execute(
                "INSERT INTO audit_log (actor, action, target, detail_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    actor,
                    action,
                    target,
                    json.dumps(detail, ensure_ascii=False) if detail else None,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
    except Exception:
        logger.exception("감사 로그 기록 실패: %s %s", action, target)


def recent(limit: int = 200) -> list[AuditEntry]:
    with storage.cursor() as cur:
        cur.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()

    entries: list[AuditEntry] = []
    for row in rows:
        try:
            detail = json.loads(row["detail_json"]) if row["detail_json"] else {}
        except json.JSONDecodeError:
            detail = {}
        entries.append(
            AuditEntry(
                id=row["id"],
                actor=row["actor"],
                action=row["action"],
                target=row["target"],
                detail=detail,
                created_at=row["created_at"],
            )
        )
    return entries
