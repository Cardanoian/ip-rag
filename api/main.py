"""FastAPI 발명 아이디어 유사 자료 검색 API."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import RedirectResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from api.auth import require_service_token
from api.schemas import (
    PublicResultItem,
    ReadyResponse,
    ResultItem,
    SearchRequest,
    SearchResponse,
    SearchResponseV1,
)
from ingest import embedder
from ingest.search import search
from ingest.store import get_collection

logger = logging.getLogger(__name__)

# 동시 검색 요청 수 제한 (임베딩 API 백프레셔)
_SEARCH_SEMAPHORE = asyncio.Semaphore(config.SEARCH_CONCURRENCY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 수명주기 — 시작 시 Gemini 클라이언트 웜업."""
    if os.getenv("GEMINI_API_KEY"):
        try:
            embedder.get_client()
            logger.info("Gemini 임베딩 클라이언트 초기화 완료.")
        except Exception as exc:
            logger.warning("Gemini 클라이언트 초기화 실패 (%s)", type(exc).__name__)
    else:
        logger.warning(
            "GEMINI_API_KEY 가 설정되지 않았습니다. "
            "/v1/search 호출 시 503 오류가 발생합니다."
        )
    if config.APP_ENV in {"production", "prod"} and not os.getenv("RAG_API_TOKEN"):
        logger.error("production 환경에 RAG_API_TOKEN이 설정되지 않았습니다.")
    yield


app = FastAPI(
    title="발명 아이디어 유사 자료 검색 API",
    description=(
        "학생 발명 아이디어와 기존 수상작 간의 유사 자료를 검색합니다. "
        "검색 결과는 신규성 또는 특허 가능성 판정이 아닙니다."
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
async def health() -> dict:
    """프로세스 생존 여부만 확인하는 liveness probe."""
    return {"status": "ok"}


@app.get("/ready", response_model=ReadyResponse)
async def ready(response: Response) -> ReadyResponse:
    """임베딩 키와 검색 색인이 실제 요청을 받을 준비가 됐는지 확인한다."""
    problems: list[str] = []
    if not os.getenv("GEMINI_API_KEY"):
        problems.append("GEMINI_API_KEY is missing")
    if config.APP_ENV in {"production", "prod"} and not os.getenv("RAG_API_TOKEN"):
        problems.append("RAG_API_TOKEN is missing")

    indexed_chunks = 0
    try:
        indexed_chunks = await asyncio.to_thread(get_collection().count)
        if indexed_chunks == 0:
            problems.append("search index is empty")
    except Exception:
        logger.exception("준비 상태 확인 중 ChromaDB 접근 실패")
        problems.append("search index is unavailable")

    if problems:
        response.status_code = 503
    return ReadyResponse(
        status="ready" if not problems else "not_ready",
        indexed_chunks=indexed_chunks,
        corpus_id=config.CORPUS_ID,
        index_version=config.INDEX_VERSION,
        problems=problems,
    )


async def _search_with_backpressure(req: SearchRequest) -> list[dict]:
    """동기 Gemini/Chroma 작업을 이벤트 루프 밖에서 실행한다."""
    try:
        await asyncio.wait_for(
            _SEARCH_SEMAPHORE.acquire(),
            timeout=config.SEARCH_QUEUE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="서버가 바쁩니다. 잠시 후 다시 시도하세요.",
        )

    try:
        try:
            return await asyncio.to_thread(
                search,
                req.text,
                req.top_k,
                req.include_advisor_docs,
            )
        except RuntimeError:
            logger.exception("임베딩 또는 검색 저장소를 사용할 수 없음")
            raise HTTPException(
                status_code=503,
                detail="검색 서비스를 일시적으로 사용할 수 없습니다.",
            )
        except Exception as exc:
            logger.exception("검색 중 예기치 않은 오류: %s", type(exc).__name__)
            raise HTTPException(
                status_code=500,
                detail="검색 중 내부 오류가 발생했습니다.",
            )
    finally:
        _SEARCH_SEMAPHORE.release()


def _public_document_id(source_path: str) -> str:
    """내부 파일 경로 대신 반환할 안정적인 불투명 ID."""
    return hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:24]


@app.post(
    "/v1/search",
    response_model=SearchResponseV1,
    dependencies=[Depends(require_service_token)],
)
async def search_v1(req: SearchRequest) -> SearchResponseV1:
    """Co-AI용 최소 개인정보 유사 자료 검색 API."""
    raw_results = await _search_with_backpressure(req)
    results = [
        PublicResultItem(
            document_id=_public_document_id(item["source_path"]),
            title=item["title"],
            year=item["year"],
            category=item["category"],
            doc_type=item["doc_type"],
            similarity=item["similarity"],
            snippet=item["snippet"],
        )
        for item in raw_results
    ]
    return SearchResponseV1(
        query=req.text,
        results=results,
        count=len(results),
        corpus_id=config.CORPUS_ID,
        index_version=config.INDEX_VERSION,
    )


@app.post(
    "/search",
    response_model=SearchResponse,
    deprecated=True,
    dependencies=[Depends(require_service_token)],
)
async def search_endpoint(req: SearchRequest) -> SearchResponse:
    """하위 호환용 API. 신규 연동은 /v1/search를 사용한다."""
    raw_results = await _search_with_backpressure(req)
    results = [ResultItem(**item) for item in raw_results]
    return SearchResponse(query=req.text, results=results, count=len(results))
