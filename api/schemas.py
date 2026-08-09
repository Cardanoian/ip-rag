"""API 요청/응답 Pydantic v2 스키마."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from pydantic import BaseModel, Field, field_validator


class SearchRequest(BaseModel):
    text: str = Field(min_length=1)
    top_k: int = Field(default=config.DEFAULT_TOP_K, ge=1, le=50)
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
    """Co-AI에 노출하는 최소 검색 결과. 저자명과 내부 경로는 제외한다."""

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
    notice: str = (
        "검색 결과는 유사 자료 탐색을 돕는 참고 정보이며, "
        "신규성·특허 가능성을 판정하지 않습니다."
    )


class ReadyResponse(BaseModel):
    status: str
    indexed_chunks: int
    corpus_id: str
    index_version: str
    problems: list[str] = Field(default_factory=list)
