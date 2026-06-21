"""임베딩 추상화 — Gemini gemini-embedding-2 API 래퍼.

공개 인터페이스:
  get_client()           -> genai.Client  (모듈 수준 캐시, lazy 초기화)
  count_tokens(text)     -> int
  embed_documents(texts) -> list[list[float]]
  embed_query(text)      -> list[float]

주의:
  - import 시점에 API 키를 읽지 않는다. 함수 호출 시 get_client()가 최초 1회 초기화.
  - 네트워크 호출이 있는 모든 함수는 함수 내부에서만 수행(테스트 monkeypatch 가능).
"""
from __future__ import annotations

import time
import logging
from typing import Any

from google import genai
from google.genai import types

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# --- 모듈 수준 클라이언트 캐시 ---
_client: genai.Client | None = None


def get_client() -> genai.Client:
    """API 키를 확인하고 genai.Client를 1회만 생성해 캐시한다.

    import 시점이 아닌 첫 호출 시 초기화되므로, API 키 없이 모듈을 임포트해도 오류가
    발생하지 않는다.
    """
    global _client
    if _client is None:
        api_key = config.require_api_key()
        _client = genai.Client(api_key=api_key)
    return _client


# --- 재시도/백오프 헬퍼 ---

def _call_with_backoff(fn, *args, max_tries: int = 5, base_delay: float = 1.0, **kwargs) -> Any:
    """지수 백오프로 fn(*args, **kwargs)를 최대 max_tries번 재시도한다.

    일시적 오류(연결·rate-limit 등) 발생 시 재시도하고, max_tries 초과 시 마지막 예외를
    다시 던진다.
    """
    delay = base_delay
    last_exc: Exception | None = None
    for attempt in range(1, max_tries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt == max_tries:
                break
            logger.warning(
                "embed API 오류 (시도 %d/%d), %.1fs 후 재시도: %s",
                attempt, max_tries, delay, exc,
            )
            time.sleep(delay)
            delay *= 2
    raise last_exc  # type: ignore[misc]


# --- 공개 함수 ---

def count_tokens(text: str) -> int:
    """텍스트의 토큰 수를 반환한다.

    Gemini count_tokens API를 우선 시도하고, 실패하면 보수적 문자 수 휴리스틱으로
    폴백한다.

    폴백 공식: int(len(text) / 1.2) + 1
      한국어 서브워드 토큰/문자 비율이 ~1.2 미만이므로, 1.2로 나누면 토큰을 과대
      추정한다(안전 방향). 빈 문자열 방지를 위해 +1 적용.
    """
    try:
        resp = get_client().models.count_tokens(
            model=config.EMBED_MODEL,
            contents=text,
        )
        return resp.total_tokens
    except Exception as exc:
        logger.debug("count_tokens API 실패, 휴리스틱 폴백: %s", exc)
        return int(len(text) / 1.2) + 1


def embed_documents(texts: list[str]) -> list[list[float]]:
    """문서 텍스트 목록을 임베딩 벡터 목록으로 변환한다.

    각 텍스트 앞에 DOC_TASK_PREFIX를 붙여 비대칭 검색용 지시문을 포함한다.
    EMBED_BATCH_SIZE 단위로 배치 API를 호출하며, 각 호출은 지수 백오프로 재시도된다.
    반환 순서는 입력 순서와 동일하게 보장된다.

    Args:
        texts: 임베딩할 원본 텍스트 목록 (task prefix 미포함).

    Returns:
        각 텍스트에 대응하는 float 벡터(길이 EMBED_DIM)의 리스트.
        빈 입력이면 빈 리스트를 즉시 반환.
    """
    if not texts:
        return []

    prefixed = [config.DOC_TASK_PREFIX + t for t in texts]
    results: list[list[float]] = []

    batch_size = config.EMBED_BATCH_SIZE
    for batch_start in range(0, len(prefixed), batch_size):
        batch = prefixed[batch_start : batch_start + batch_size]

        def _call(b=batch):
            return get_client().models.embed_content(
                model=config.EMBED_MODEL,
                contents=b,
                config=types.EmbedContentConfig(output_dimensionality=config.EMBED_DIM),
            )

        resp = _call_with_backoff(_call)
        for embedding in resp.embeddings:
            results.append(embedding.values)

    return results


def embed_query(text: str) -> list[float]:
    """질의 텍스트를 임베딩 벡터로 변환한다.

    텍스트 앞에 QUERY_TASK_PREFIX를 붙여 질의용 지시문을 포함한다.
    단일 embed_content 호출(지수 백오프 적용).

    Args:
        text: 임베딩할 질의 텍스트 (task prefix 미포함).

    Returns:
        길이 EMBED_DIM의 float 벡터.
    """
    prefixed = config.QUERY_TASK_PREFIX + text

    def _call():
        return get_client().models.embed_content(
            model=config.EMBED_MODEL,
            contents=prefixed,
            config=types.EmbedContentConfig(output_dimensionality=config.EMBED_DIM),
        )

    resp = _call_with_backoff(_call)
    return resp.embeddings[0].values
