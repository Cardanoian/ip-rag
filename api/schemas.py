"""API 요청/응답 Pydantic v2 스키마."""
from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from pydantic import BaseModel, Field, field_validator

_NOTICE = (
    "검색 결과는 유사 자료 탐색을 돕는 참고 정보이며, "
    "신규성·특허 가능성을 판정하지 않습니다."
)


class SearchRequest(BaseModel):
    text: str = Field(min_length=1)
    top_k: int = Field(default=config.DEFAULT_TOP_K, ge=1, le=50)
    # corpus 종류별 검색 옵션. 발명 corpus는 include_advisor_docs를 읽는다.
    options: dict[str, Any] = Field(default_factory=dict)
    # 하위 호환: 최상위 플래그로도 계속 받는다. options에 머지된다.
    include_advisor_docs: bool = False

    @field_validator("text")
    @classmethod
    def text_not_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("text must not be blank or whitespace-only")
        if len(stripped) > config.MAX_QUERY_CHARS:
            raise ValueError(
                f"text must not exceed {config.MAX_QUERY_CHARS} characters"
            )
        return stripped

    def search_options(self) -> dict[str, Any]:
        """corpus에 넘길 최종 옵션 — 최상위 하위 호환 플래그를 합친다."""
        merged = dict(self.options)
        if self.include_advisor_docs:
            merged["include_advisor_docs"] = True
        return merged


class ResultItem(BaseModel):
    """하위 호환 /search 응답."""

    title: str
    year: int | None
    category: str
    author: str
    doc_type: str
    source_path: str
    similarity: float
    snippet: str


class SearchResponse(BaseModel):
    query: str
    results: list[ResultItem]
    count: int


class PublicResultItem(BaseModel):
    """하위 호환 /v1/search 응답. 저자명과 내부 경로는 제외한다."""

    document_id: str
    title: str
    year: int | None
    category: str
    doc_type: str
    similarity: float = Field(
        ge=0.0,
        le=1.0,
        description="0~1로 재조정한 코사인 매칭 점수. 신규성 판정값이 아님.",
    )
    snippet: str


class SearchResponseV1(BaseModel):
    query: str
    results: list[PublicResultItem]
    count: int
    corpus_id: str
    index_version: str
    score_kind: str = "rescaled_cosine_match"
    notice: str = _NOTICE


class CorpusResultItem(BaseModel):
    """corpus 공통 검색 결과. corpus별 필드는 metadata에 담긴다."""

    document_id: str
    title: str
    similarity: float = Field(
        ge=0.0,
        le=1.0,
        description="0~1로 재조정한 코사인 매칭 점수. 신규성 판정값이 아님.",
    )
    snippet: str
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="corpus 종류가 공개하도록 선언한 메타데이터만 담긴다.",
    )


class CorpusSearchResponse(BaseModel):
    query: str
    corpus: str
    results: list[CorpusResultItem]
    count: int
    corpus_id: str
    index_version: str
    score_kind: str = "rescaled_cosine_match"
    notice: str = _NOTICE


class CorpusInfo(BaseModel):
    """공개 corpus 목록 항목."""

    corpus: str
    label: str
    kind: str
    corpus_id: str
    index_version: str
    indexed_chunks: int


class CorpusListResponse(BaseModel):
    corpora: list[CorpusInfo]
    count: int


class CorpusReadiness(BaseModel):
    corpus: str
    corpus_id: str
    index_version: str
    indexed_chunks: int
    problems: list[str] = Field(default_factory=list)


class ReadyResponse(BaseModel):
    status: str
    indexed_chunks: int
    corpus_id: str
    index_version: str
    problems: list[str] = Field(default_factory=list)
    corpora: list[CorpusReadiness] = Field(default_factory=list)
