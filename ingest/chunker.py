"""길이 적응형 하이브리드 청킹 모듈.

임베더와 독립적으로 동작한다 — API 호출 없음, 임베더 임포트 없음.
토큰 정보는 선택적 callable(count_tokens)로 주입받는다.
청킹 파라미터는 corpus 설정에서 오므로 splitter를 파라미터 조합별로 캐시한다.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Callable, Optional

from langchain_text_splitters import RecursiveCharacterTextSplitter

from config import MAX_INPUT_TOKENS


@lru_cache(maxsize=32)
def _get_splitter(chunk_size: int, chunk_overlap: int):
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
        length_function=len,
    )


def chunk_text(text: str, cfg) -> list[str]:
    """텍스트를 corpus 청킹 설정으로 분할한다.

    Args:
        text: 분할할 원문 텍스트.
        cfg: chunk_size/chunk_overlap을 가진 corpus 설정.

    Returns:
        비어있지 않은 청크 문자열 리스트.
    """
    splitter = _get_splitter(cfg.chunk_size, cfg.chunk_overlap)
    return [c for c in splitter.split_text(text) if c]


def chunk_document(
    text: str,
    cfg,
    count_tokens: Optional[Callable[[str], int]] = None,
) -> list[str]:
    """길이 적응형 청킹: 짧은 문서는 통째로, 긴 문서는 분할한다.

    분기 기준:
    - count_tokens callable이 제공되면 토큰 수로 판단
      (n_tokens <= MAX_INPUT_TOKENS → 단일청크).
    - 제공되지 않으면 문자 수 휴리스틱 사용
      (len(text) <= cfg.single_chunk_char_hint → 단일청크).

    Args:
        text: 처리할 문서 텍스트.
        cfg: corpus 설정.
        count_tokens: 텍스트를 받아 토큰 수를 반환하는 callable (선택적).

    Returns:
        청크 문자열 리스트. 빈/공백 입력이면 [].
    """
    stripped = text.strip() if text else ""
    if not stripped:
        return []

    if count_tokens is not None:
        single_chunk_eligible = count_tokens(stripped) <= MAX_INPUT_TOKENS
    else:
        single_chunk_eligible = len(stripped) <= cfg.single_chunk_char_hint

    if single_chunk_eligible:
        return [stripped]
    return chunk_text(stripped, cfg)
