"""FastAPI 발명 아이디어 유사도 검색 API."""
from __future__ import annotations

import asyncio
import logging
import sys
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from ingest import embedder
from ingest.search import search
from api.schemas import SearchRequest, SearchResponse, ResultItem

logger = logging.getLogger(__name__)

# 동시 검색 요청 수 제한 (임베딩 API 백프레셔)
_SEARCH_SEMAPHORE = asyncio.Semaphore(10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 수명주기 — 시작 시 Gemini 클라이언트 웜업."""
    if config.GEMINI_API_KEY:
        try:
            embedder.get_client()
            logger.info("Gemini 임베딩 클라이언트 초기화 완료.")
        except Exception as exc:
            logger.warning("Gemini 클라이언트 초기화 실패: %s", exc)
    else:
        logger.warning(
            "GEMINI_API_KEY 가 설정되지 않았습니다. "
            "/search 호출 시 503 오류가 발생합니다."
        )
    yield


app = FastAPI(
    title="발명 아이디어 유사도 검색 API",
    description="학생 발명 아이디어와 기존 수상작 간의 유사도를 검색합니다.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/search", response_model=SearchResponse)
async def search_endpoint(req: SearchRequest) -> SearchResponse:
    """발명 아이디어 유사도 검색."""
    try:
        await asyncio.wait_for(_SEARCH_SEMAPHORE.acquire(), timeout=2.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail="서버가 바쁩니다. 잠시 후 다시 시도하세요.")

    try:
        try:
            raw_results = search(req.text, req.top_k, req.include_advisor_docs)
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"임베딩 API를 사용할 수 없습니다: {exc}",
            )
        except Exception as exc:
            logger.exception("검색 중 예기치 않은 오류: %s", exc)
            raise HTTPException(status_code=500, detail="검색 중 내부 오류가 발생했습니다.")

        results = [ResultItem(**item) for item in raw_results]
        return SearchResponse(query=req.text, results=results, count=len(results))
    finally:
        _SEARCH_SEMAPHORE.release()
