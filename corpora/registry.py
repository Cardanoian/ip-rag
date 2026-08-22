"""corpus 레지스트리 — SQLite `corpora` 테이블에 대한 읽기·쓰기.

검색 API가 요청마다 이 레지스트리를 조회하므로 인메모리 캐시를 두고,
쓰기가 일어날 때만 무효화한다.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

import storage
from corpora.kinds import get_kind
from corpora.models import (
    ALL_STATUSES,
    STATUS_DRAFT,
    STATUS_PUBLISHED,
    STATUS_UNPUBLISHED,
    CorpusConfig,
    CorpusValidationError,
    validate_chunking,
    validate_embed_dim,
    validate_slug,
)
from corpora.seed import SEED_CORPUS_ID, seed_config

logger = logging.getLogger(__name__)

_COLUMNS = (
    "id, kind, label, corpus_id, base_collection, doc_prefix, query_prefix, "
    "embed_dim, chunk_size, chunk_overlap, single_chunk_char_hint, "
    "active_collection, index_version, status, is_seed, needs_rebuild, "
    "docs_dir_override, created_at, created_by, updated_at"
)

_cache: dict[str, CorpusConfig] | None = None
_cache_lock = threading.Lock()


class CorpusNotFound(KeyError):
    """등록되지 않은 corpus id."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_config(row) -> CorpusConfig:
    return CorpusConfig(
        id=row["id"],
        kind=row["kind"],
        label=row["label"],
        corpus_id=row["corpus_id"],
        base_collection=row["base_collection"],
        doc_prefix=row["doc_prefix"],
        query_prefix=row["query_prefix"],
        embed_dim=row["embed_dim"],
        chunk_size=row["chunk_size"],
        chunk_overlap=row["chunk_overlap"],
        single_chunk_char_hint=row["single_chunk_char_hint"],
        active_collection=row["active_collection"],
        index_version=row["index_version"],
        status=row["status"],
        is_seed=bool(row["is_seed"]),
        needs_rebuild=bool(row["needs_rebuild"]),
        docs_dir_override=row["docs_dir_override"],
        created_at=row["created_at"],
        created_by=row["created_by"],
        updated_at=row["updated_at"],
    )


def _insert(cur, cfg: CorpusConfig) -> None:
    cur.execute(
        f"INSERT INTO corpora ({_COLUMNS}) VALUES ("
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            cfg.id,
            cfg.kind,
            cfg.label,
            cfg.corpus_id,
            cfg.base_collection,
            cfg.doc_prefix,
            cfg.query_prefix,
            cfg.embed_dim,
            cfg.chunk_size,
            cfg.chunk_overlap,
            cfg.single_chunk_char_hint,
            cfg.active_collection,
            cfg.index_version,
            cfg.status,
            int(cfg.is_seed),
            int(cfg.needs_rebuild),
            cfg.docs_dir_override,
            cfg.created_at,
            cfg.created_by,
            cfg.updated_at,
        ),
    )


def invalidate_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def _load_all() -> dict[str, CorpusConfig]:
    global _cache
    with _cache_lock:
        if _cache is not None:
            return _cache
    ensure_seed()
    with storage.cursor() as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM corpora ORDER BY is_seed DESC, id")
        loaded = {row["id"]: _row_to_config(row) for row in cur.fetchall()}
    with _cache_lock:
        _cache = loaded
    return loaded


def ensure_seed() -> None:
    """corpora 테이블이 비어 있으면 시드 corpus 한 건을 넣는다."""
    with storage.transaction() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM corpora")
        if cur.fetchone()["n"] > 0:
            return
        _insert(cur, seed_config(_now()))
        logger.info("corpora 테이블이 비어 있어 시드 corpus %r 생성", SEED_CORPUS_ID)
    invalidate_cache()


def get(corpus_id: str) -> CorpusConfig:
    try:
        return _load_all()[corpus_id]
    except KeyError:
        raise CorpusNotFound(corpus_id) from None


def get_published(corpus_id: str) -> CorpusConfig:
    """검색 API 전용 — 비공개 corpus는 존재하지 않는 것으로 취급한다."""
    cfg = get(corpus_id)
    if not cfg.is_published:
        raise CorpusNotFound(corpus_id)
    return cfg


def list_all(status: str | None = None) -> list[CorpusConfig]:
    items = list(_load_all().values())
    if status is not None:
        items = [cfg for cfg in items if cfg.status == status]
    return items


def list_published() -> list[CorpusConfig]:
    return list_all(status=STATUS_PUBLISHED)


def create(
    *,
    corpus_slug: str,
    label: str,
    kind: str,
    corpus_id: str,
    doc_prefix: str,
    query_prefix: str,
    embed_dim: int,
    chunk_size: int,
    chunk_overlap: int,
    single_chunk_char_hint: int,
    created_by: str | None,
) -> CorpusConfig:
    """새 corpus를 draft 상태로 만든다."""
    slug = validate_slug(corpus_slug)
    if slug == SEED_CORPUS_ID:
        raise CorpusValidationError(f"{SEED_CORPUS_ID}는 예약된 주소입니다.")

    kind_impl = get_kind(kind)
    if not kind_impl.creatable_by_admin:
        raise CorpusValidationError(
            f"{kind_impl.label} 종류는 새로 만들 수 없습니다."
        )

    label = (label or "").strip()
    if not label:
        raise CorpusValidationError("이름을 입력하세요.")

    corpus_id = (corpus_id or "").strip() or slug
    doc_prefix = (doc_prefix or "").strip()
    query_prefix = (query_prefix or "").strip()
    if not doc_prefix or not query_prefix:
        raise CorpusValidationError("문서·질의 지시문을 모두 입력하세요.")
    # 지시문은 본문 앞에 붙으므로 개행으로 끝나야 경계가 분명하다.
    doc_prefix = doc_prefix + "\n"
    query_prefix = query_prefix + "\n"

    dim = validate_embed_dim(embed_dim)
    size, overlap, hint = validate_chunking(
        chunk_size, chunk_overlap, single_chunk_char_hint
    )

    import config

    now = _now()
    cfg = CorpusConfig(
        id=slug,
        kind=kind_impl.name,
        label=label,
        corpus_id=corpus_id,
        base_collection=slug,
        doc_prefix=doc_prefix,
        query_prefix=query_prefix,
        embed_dim=dim,
        chunk_size=size,
        chunk_overlap=overlap,
        single_chunk_char_hint=hint,
        active_collection=f"{slug}_v1",
        index_version=f"{slug}:{config.EMBED_MODEL}:{dim}:v1",
        status=STATUS_DRAFT,
        is_seed=False,
        created_at=now,
        created_by=created_by,
        updated_at=now,
    )

    try:
        with storage.transaction() as cur:
            _insert(cur, cfg)
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise CorpusValidationError(
                f"이미 사용 중인 corpus 주소입니다: {slug}"
            ) from None
        raise

    cfg.docs_dir().mkdir(parents=True, exist_ok=True)
    invalidate_cache()
    return cfg


def update(cfg: CorpusConfig, **changes) -> CorpusConfig:
    """설정 변경. 호출자가 이미 검증한 값만 넘긴다."""
    if not changes:
        return cfg

    updated = cfg.with_updates(updated_at=_now(), **changes)
    assignments = ", ".join(f"{field} = ?" for field in changes)
    values = [
        int(value) if isinstance(value, bool) else value
        for value in changes.values()
    ]

    with storage.transaction() as cur:
        cur.execute(
            f"UPDATE corpora SET {assignments}, updated_at = ? WHERE id = ?",
            [*values, updated.updated_at, cfg.id],
        )
    invalidate_cache()
    return updated


def set_active_collection(cfg: CorpusConfig, collection_name: str) -> CorpusConfig:
    """alias 전환 — 새 컬렉션이 완성된 뒤에만 호출한다."""
    version = collection_name.rsplit("_v", 1)[-1]
    index_version = f"{cfg.base_collection}:{cfg.embed_dim}:v{version}"
    return update(
        cfg,
        active_collection=collection_name,
        index_version=index_version,
        needs_rebuild=False,
    )


def set_status(cfg: CorpusConfig, status: str) -> CorpusConfig:
    if status not in ALL_STATUSES:
        raise CorpusValidationError(f"알 수 없는 상태입니다: {status}")
    if status == STATUS_PUBLISHED and cfg.status == STATUS_DRAFT:
        # 색인이 비어 있는 corpus를 공개하면 검색이 빈 결과를 반환한다.
        from ingest.store import count_documents

        if count_documents(cfg.active_collection) == 0:
            raise CorpusValidationError(
                "색인이 비어 있어 공개할 수 없습니다. 먼저 문서를 올리고 색인하세요."
            )
    return update(cfg, status=status)


def delete(cfg: CorpusConfig) -> None:
    """DB 행과 잡 이력을 제거한다. 파일·컬렉션 삭제는 호출자 책임이다."""
    if cfg.is_seed:
        raise CorpusValidationError("기본 corpus는 삭제할 수 없습니다.")
    if cfg.status != STATUS_UNPUBLISHED:
        raise CorpusValidationError(
            "완전삭제는 비공개 상태에서만 할 수 있습니다."
        )
    with storage.transaction() as cur:
        cur.execute("DELETE FROM jobs WHERE corpus_id = ?", (cfg.id,))
        cur.execute("DELETE FROM corpora WHERE id = ?", (cfg.id,))
    invalidate_cache()
