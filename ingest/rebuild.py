"""무중단 재색인 — 새 버전 컬렉션을 완성한 뒤 corpus의 활성 포인터만 옮긴다.

인플레이스로 컬렉션을 비우고 다시 채우면 재색인이 도는 동안 검색이 빈 결과를 반환하고,
중간에 실패하면 색인이 깨진 채로 남는다. 여기서는 검색이 옛 컬렉션을 계속 바라보다가
전환 시점에 한 번에 새 컬렉션으로 넘어간다.
"""
from __future__ import annotations

import logging
import re
from typing import Callable, Optional

import config
import corpora
from ingest.build_index import build_index
from ingest.store import drop_collection, list_collections

logger = logging.getLogger(__name__)


class RebuildError(RuntimeError):
    """재색인이 전환 조건을 만족하지 못했다. 기존 색인은 그대로 살아 있다."""


def _version_of(collection_name: str, base: str) -> int | None:
    match = re.match(rf"^{re.escape(base)}_v(\d+)$", collection_name)
    return int(match.group(1)) if match else None


def prune_old_collections(cfg, keep: Optional[int] = None) -> list[str]:
    """활성 버전보다 낮은 옛 컬렉션을 정리한다. 롤백용으로 일부는 남긴다."""
    keep = config.KEEP_OLD_COLLECTIONS if keep is None else keep
    active_version = cfg.collection_version

    older: list[tuple[int, str]] = []
    for name in list_collections():
        version = _version_of(name, cfg.base_collection)
        if version is not None and version < active_version:
            older.append((version, name))

    older.sort(reverse=True)
    dropped = [name for _, name in older[keep:]]
    for name in dropped:
        drop_collection(name)
        logger.info("prune_old_collections[%s]: %s 삭제", cfg.id, name)
    return dropped


def rebuild_corpus(
    cfg,
    progress: Optional[Callable[[int, int], None]] = None,
    limit: Optional[int] = None,
) -> tuple[object, dict]:
    """전체 재색인 후 alias 전환. (갱신된 cfg, 통계) 반환.

    실패하거나 결과가 비면 새 컬렉션을 버리고 활성 포인터를 건드리지 않는다.
    """
    next_name = cfg.next_collection_name()
    logger.info(
        "rebuild_corpus[%s]: %s → %s 전환 시작",
        cfg.id, cfg.active_collection, next_name,
    )

    try:
        stats = build_index(
            cfg,
            target_collection=next_name,
            progress=progress,
            limit=limit,
            reset=True,
        )
    except Exception:
        drop_collection(next_name)
        logger.exception(
            "rebuild_corpus[%s]: 색인 실패, %s 폐기 — 기존 색인 유지",
            cfg.id, next_name,
        )
        raise

    if stats["total_chunks"] == 0:
        drop_collection(next_name)
        raise RebuildError(
            "색인된 청크가 없어 전환을 중단했습니다. 기존 색인은 그대로입니다."
        )

    updated = corpora.set_active_collection(cfg, next_name)
    prune_old_collections(updated)
    logger.info(
        "rebuild_corpus[%s]: %s 로 전환 완료 (chunks=%d)",
        cfg.id, next_name, stats["total_chunks"],
    )
    return updated, stats
