"""corpus 인스턴스 모델 — SQLite `corpora` 행 하나에 대응한다.

corpus의 *동작*(파싱, 임베딩 입력 구성, 검색 필터)은 kind가 소유하고,
corpus의 *파라미터*(이름, 프리픽스, 청킹, 차원)는 이 모델이 담는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

import config

# corpus id는 URL 세그먼트이자 디렉터리명이자 Chroma 컬렉션명의 일부다.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
# 컬렉션 이름은 항상 "{base}_v{n}" 형태다.
COLLECTION_VERSION_RE = re.compile(r"^(?P<base>.+)_v(?P<version>\d+)$")

STATUS_DRAFT = "draft"
STATUS_PUBLISHED = "published"
STATUS_UNPUBLISHED = "unpublished"
ALL_STATUSES = (STATUS_DRAFT, STATUS_PUBLISHED, STATUS_UNPUBLISHED)

# 값이 바뀌면 기존 벡터가 무의미해지는 필드. 어드민이 재색인을 유도한다.
REBUILD_REQUIRED_FIELDS = frozenset(
    {
        "doc_prefix",
        "embed_dim",
        "chunk_size",
        "chunk_overlap",
        "single_chunk_char_hint",
    }
)


class CorpusValidationError(ValueError):
    """어드민 입력 검증 실패. 라우터가 400으로 바꾼다."""


@dataclass(frozen=True)
class CorpusConfig:
    id: str
    kind: str
    label: str
    corpus_id: str
    base_collection: str
    doc_prefix: str
    query_prefix: str
    embed_dim: int
    chunk_size: int
    chunk_overlap: int
    single_chunk_char_hint: int
    active_collection: str
    index_version: str
    status: str = STATUS_DRAFT
    is_seed: bool = False
    needs_rebuild: bool = False
    docs_dir_override: str | None = None
    created_at: str = ""
    created_by: str | None = None
    updated_at: str = ""

    @property
    def is_published(self) -> bool:
        return self.status == STATUS_PUBLISHED

    @property
    def collection_version(self) -> int:
        match = COLLECTION_VERSION_RE.match(self.active_collection)
        return int(match.group("version")) if match else 1

    def next_collection_name(self) -> str:
        """alias 전환용 다음 버전 컬렉션명."""
        return f"{self.base_collection}_v{self.collection_version + 1}"

    def docs_dir(self) -> Path:
        """원본 문서 디렉터리. override가 있으면 기존 배포 경로를 그대로 쓴다."""
        if self.docs_dir_override:
            return Path(self.docs_dir_override)
        return Path(config.DOCS_ROOT) / self.id

    def with_updates(self, **changes) -> "CorpusConfig":
        return replace(self, **changes)


def validate_slug(value: str) -> str:
    """corpus id 검증 — 디렉터리 탈출과 컬렉션명 충돌을 원천 차단한다."""
    slug = (value or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise CorpusValidationError(
            "corpus 주소는 영문 소문자·숫자·하이픈 2~31자여야 하며 "
            "문자 또는 숫자로 시작해야 합니다."
        )
    return slug


def validate_embed_dim(value: int) -> int:
    dim = int(value)
    if not (config.EMBED_DIM_MIN <= dim <= config.EMBED_DIM_MAX):
        raise CorpusValidationError(
            f"임베딩 차원은 {config.EMBED_DIM_MIN}~{config.EMBED_DIM_MAX} 사이여야 합니다."
        )
    return dim


def validate_chunking(
    chunk_size: int,
    chunk_overlap: int,
    single_chunk_char_hint: int,
) -> tuple[int, int, int]:
    size = int(chunk_size)
    overlap = int(chunk_overlap)
    hint = int(single_chunk_char_hint)

    if size < 100:
        raise CorpusValidationError("청크 크기는 100자 이상이어야 합니다.")
    if overlap < 0:
        raise CorpusValidationError("청크 겹침은 0 이상이어야 합니다.")
    if overlap >= size:
        raise CorpusValidationError("청크 겹침은 청크 크기보다 작아야 합니다.")
    if hint < size:
        raise CorpusValidationError(
            "단일 청크 한도는 청크 크기보다 커야 합니다."
        )
    return size, overlap, hint


def rebuild_required_changes(
    before: CorpusConfig,
    after: CorpusConfig,
) -> set[str]:
    """설정 변경 중 전체 재색인이 필요한 필드만 골라낸다."""
    return {
        field
        for field in REBUILD_REQUIRED_FIELDS
        if getattr(before, field) != getattr(after, field)
    }
