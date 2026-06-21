"""API 요청/응답 Pydantic v2 스키마."""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from pydantic import BaseModel, field_validator, Field


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
