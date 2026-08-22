"""시드 corpus 정의 — 빈 DB에 처음 기동할 때 삽입되는 `inventions` 한 건.

값은 멀티 corpus 이전의 config.py 상수를 그대로 옮긴 것이다. 기존 배포가
마이그레이션 없이 동일하게 동작하도록 환경변수 오버라이드도 유지한다.
"""
from __future__ import annotations

import os

import config
from corpora.kinds import InventionKind
from corpora.models import STATUS_PUBLISHED, CorpusConfig

SEED_CORPUS_ID = "inventions"

_EMBED_DIM = 1536
_BASE_COLLECTION = "inventions"

DOC_PREFIX = (
    "[검색 대상 문서] 다음은 학생 발명품 작품 설명서입니다. "
    "핵심 발명 아이디어를 검색용으로 표현합니다.\n"
)
QUERY_PREFIX = (
    "[검색 질의] 다음 발명 아이디어와 유사한 기존 발명 작품을 찾습니다.\n"
)


def seed_config(now: str) -> CorpusConfig:
    """시드 corpus 설정. 이미 색인이 있는 배포를 깨지 않도록 published로 시작한다."""
    return CorpusConfig(
        id=SEED_CORPUS_ID,
        kind=InventionKind.name,
        label="발명대회 수상작",
        corpus_id=os.getenv(
            "CORPUS_ID",
            "national-student-invention-awards-1979-2017",
        ),
        base_collection=_BASE_COLLECTION,
        doc_prefix=DOC_PREFIX,
        query_prefix=QUERY_PREFIX,
        embed_dim=_EMBED_DIM,
        chunk_size=1000,
        chunk_overlap=150,
        single_chunk_char_hint=5500,
        active_collection=f"{_BASE_COLLECTION}_v1",
        index_version=os.getenv(
            "INDEX_VERSION",
            f"{_BASE_COLLECTION}:{config.EMBED_MODEL}:{_EMBED_DIM}:v1",
        ),
        status=STATUS_PUBLISHED,
        is_seed=True,
        created_at=now,
        created_by=None,
        updated_at=now,
    )
