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


class EmbeddingUnavailable(RuntimeError):
    """임베딩 공급자를 쓸 수 없다.

    API 키 오류, 재시도 후에도 남은 rate limit, 네트워크 장애가 여기 해당한다.
    RuntimeError를 상속하므로 API 계층이 503으로 매핑한다 — 클라이언트 요청이
    잘못된 게 아니라 서비스가 일시적으로 불가한 상황이기 때문이다.
    """


def _unavailable(exc: Exception) -> EmbeddingUnavailable:
    """공급자 예외를 도메인 예외로 감싼다. 상세는 로그에만 남긴다."""
    code = getattr(exc, "code", None)
    logger.error(
        "임베딩 공급자 호출 실패 (%s%s)",
        type(exc).__name__,
        f", code={code}" if code else "",
        exc_info=True,
    )
    return EmbeddingUnavailable(
        f"임베딩 서비스를 사용할 수 없습니다 ({type(exc).__name__})"
    )


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


def embed_documents(texts: list[str], cfg) -> list[list[float]]:
    """문서 텍스트 목록을 corpus 설정에 맞는 임베딩 벡터로 변환한다.

    gemini-embedding-2는 task_type 대신 텍스트 지시문을 쓴다. 비대칭 검색을 위해
    문서와 질의에 서로 다른 프리픽스를 일관되게 적용하며, 그 문구는 corpus마다 다르다.
    """
    if not texts:
        return []

    prefixed = [cfg.doc_prefix + text for text in texts]
    results: list[list[float]] = []

    for batch_start in range(0, len(prefixed), config.EMBED_BATCH_SIZE):
        batch = prefixed[
            batch_start : batch_start + config.EMBED_BATCH_SIZE
        ]
        # 문자열 리스트를 그대로 넘기면 SDK가 이를 한 문서의 여러 조각으로 합쳐
        # 벡터를 1개만 돌려준다. 문서마다 별도 Content로 감싸야 문서 수만큼 나온다.
        contents = [types.Content(parts=[types.Part(text=text)]) for text in batch]

        def _call(payload=contents):
            return get_client().models.embed_content(
                model=config.EMBED_MODEL,
                contents=payload,
                config=types.EmbedContentConfig(
                    output_dimensionality=cfg.embed_dim
                ),
            )

        try:
            resp = _call_with_backoff(_call)
        except (errors.APIError, httpx.HTTPError, TimeoutError, ConnectionError) as exc:
            raise _unavailable(exc) from exc

        # 개수가 어긋난 채 진행하면 청크와 벡터가 밀려 엉뚱한 문서가 검색된다.
        # 조용히 잘못 색인하느니 여기서 멈춘다.
        if len(resp.embeddings) != len(batch):
            raise EmbeddingUnavailable(
                f"임베딩 응답 개수가 요청과 다릅니다 "
                f"(요청 {len(batch)}개, 응답 {len(resp.embeddings)}개)"
            )

        for embedding in resp.embeddings:
            results.append(embedding.values)

    return results


def embed_query(text: str, cfg) -> list[float]:
    """검색 질의를 corpus 설정에 맞는 임베딩 벡터로 변환한다."""
    prefixed = cfg.query_prefix + text

    def _call():
        return get_client().models.embed_content(
            model=config.EMBED_MODEL,
            contents=prefixed,
            config=types.EmbedContentConfig(
                output_dimensionality=cfg.embed_dim
            ),
        )

    try:
        resp = _call_with_backoff(_call)
    except (errors.APIError, httpx.HTTPError, TimeoutError, ConnectionError) as exc:
        raise _unavailable(exc) from exc

    return resp.embeddings[0].values
