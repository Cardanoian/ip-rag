"""Gemini gemini-embedding-2 임베딩 API 래퍼."""
from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

import httpx
from google import genai
from google.genai import errors, types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

_client: genai.Client | None = None


def get_client() -> genai.Client:
    """API 키를 확인하고 제한시간이 설정된 Client를 1회 생성한다."""
    global _client
    if _client is None:
        api_key = config.require_api_key()
        _client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=config.GEMINI_TIMEOUT_MS),
        )
    return _client


def _is_retryable_error(exc: Exception) -> bool:
    """rate-limit, 서버 오류, 연결/시간초과처럼 일시적인 오류만 재시도한다."""
    if isinstance(
        exc,
        (httpx.TimeoutException, httpx.NetworkError, TimeoutError, ConnectionError),
    ):
        return True
    if isinstance(exc, errors.APIError):
        code = int(getattr(exc, "code", 0) or 0)
        return code in {408, 409, 429} or 500 <= code < 600
    return False


def _call_with_backoff(
    fn,
    *args,
    max_tries: int = 5,
    base_delay: float = 1.0,
    **kwargs,
) -> Any:
    """일시적 오류만 지수 백오프로 최대 max_tries번 재시도한다."""
    delay = base_delay
    for attempt in range(1, max_tries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not _is_retryable_error(exc) or attempt == max_tries:
                raise
            logger.warning(
                "embed API 일시 오류 (시도 %d/%d), %.1fs 후 재시도 (%s)",
                attempt,
                max_tries,
                delay,
                type(exc).__name__,
            )
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def count_tokens(text: str) -> int:
    """토큰 수를 반환하고, 실패하면 보수적 문자 수 휴리스틱을 사용한다."""
    try:
        resp = get_client().models.count_tokens(
            model=config.EMBED_MODEL,
            contents=text,
        )
        return resp.total_tokens
    except Exception as exc:
        logger.debug("count_tokens API 실패, 휴리스틱 폴백 (%s)", type(exc).__name__)
        return int(len(text) / 1.2) + 1


def embed_documents(texts: list[str]) -> list[list[float]]:
    """문서 텍스트 목록을 임베딩 벡터 목록으로 변환한다."""
    if not texts:
        return []

    prefixed = [config.DOC_TASK_PREFIX + text for text in texts]
    results: list[list[float]] = []

    for batch_start in range(0, len(prefixed), config.EMBED_BATCH_SIZE):
        batch = prefixed[
            batch_start : batch_start + config.EMBED_BATCH_SIZE
        ]

        def _call(contents=batch):
            return get_client().models.embed_content(
                model=config.EMBED_MODEL,
                contents=contents,
                config=types.EmbedContentConfig(
                    output_dimensionality=config.EMBED_DIM
                ),
            )

        resp = _call_with_backoff(_call)
        for embedding in resp.embeddings:
            results.append(embedding.values)

    return results


def embed_query(text: str) -> list[float]:
    """검색 질의를 임베딩 벡터로 변환한다."""
    prefixed = config.QUERY_TASK_PREFIX + text

    def _call():
        return get_client().models.embed_content(
            model=config.EMBED_MODEL,
            contents=prefixed,
            config=types.EmbedContentConfig(
                output_dimensionality=config.EMBED_DIM
            ),
        )

    resp = _call_with_backoff(_call)
    return resp.embeddings[0].values
