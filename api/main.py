"""FastAPI 유사 자료 검색 API — corpus별 컬렉션을 서빙한다."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import corpora
from admin import jobs as admin_jobs
from admin.permissions import NotAuthenticated
from admin.routes import router as admin_router
from api.auth import require_service_token
from api.schemas import (
    CorpusInfo,
    CorpusListResponse,
    CorpusReadiness,
    CorpusResultItem,
    CorpusSearchResponse,
    PublicResultItem,
    ReadyResponse,
    ResultItem,
    SearchRequest,
    SearchResponse,
    SearchResponseV1,
)
from ingest import embedder
from ingest.search import search
from ingest.store import count_documents

# uvicorn은 자기 로거만 설정하므로 애플리케이션 로그(색인 진행, 임베딩 경고,
# 감사 실패)가 아무 데도 출력되지 않는다. 루트 로거에 핸들러가 없을 때만
# 동작하므로 --log-config 로 직접 설정한 경우는 그대로 존중된다.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)

# 동시 검색 요청 수 제한 (임베딩 API 백프레셔)
_SEARCH_SEMAPHORE = asyncio.Semaphore(config.SEARCH_CONCURRENCY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 수명주기 — corpus 레지스트리 준비와 Gemini 클라이언트 웜업."""
    corpora.ensure_seed()
    # 이전 프로세스가 색인 중에 죽었다면 그 잡은 영원히 running으로 남는다.
    admin_jobs.reset_interrupted()

    if os.getenv("GEMINI_API_KEY"):
        try:
            embedder.get_client()
            logger.info("Gemini 임베딩 클라이언트 초기화 완료.")
        except Exception as exc:
            logger.warning("Gemini 클라이언트 초기화 실패 (%s)", type(exc).__name__)
    else:
        logger.warning(
            "GEMINI_API_KEY 가 설정되지 않았습니다. "
            "검색 호출 시 503 오류가 발생합니다."
        )
    if config.is_production() and not os.getenv("RAG_API_TOKEN"):
        logger.error("production 환경에 RAG_API_TOKEN이 설정되지 않았습니다.")
    if config.session_secret_is_ephemeral:
        logger.error(
            "SESSION_SECRET이 설정되지 않아 임시 키로 기동했습니다. "
            "서버를 재시작할 때마다 어드민 로그인이 풀립니다."
        )
    yield


app = FastAPI(
    title="유사 자료 검색 API",
    description=(
        "corpus별로 등록된 자료에서 유사 문서를 검색합니다. "
        "검색 결과는 신규성 또는 특허 가능성 판정이 아닙니다."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

# 어드민 세션 쿠키. 검색 API의 Bearer 토큰 인증과는 완전히 별개다.
app.add_middleware(
    SessionMiddleware,
    secret_key=config.resolve_session_secret(),
    session_cookie="admin_session",
    max_age=config.ADMIN_SESSION_MAX_AGE,
    same_site="lax",
    https_only=config.is_production(),
)

app.include_router(admin_router)


@app.exception_handler(NotAuthenticated)
async def _handle_not_authenticated(
    request: Request, exc: NotAuthenticated
) -> RedirectResponse:
    """어드민 화면은 401 대신 로그인 폼으로 보낸다."""
    return RedirectResponse(url=f"/admin/login?next={exc.next_url}", status_code=303)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
async def health() -> dict:
    """프로세스 생존 여부만 확인하는 liveness probe."""
    return {"status": "ok"}


def _resolve_published(corpus_id: str):
    """공개된 corpus만 검색 대상이다. 비공개·초안은 존재하지 않는 것으로 취급한다."""
    try:
        return corpora.get_published(corpus_id)
    except corpora.CorpusNotFound:
        raise HTTPException(
            status_code=404,
            detail=f"등록되지 않은 corpus입니다: {corpus_id}",
        ) from None


@app.get("/ready", response_model=ReadyResponse)
async def ready(response: Response) -> ReadyResponse:
    """임베딩 키와 검색 색인이 실제 요청을 받을 준비가 됐는지 확인한다.

    공개된 corpus만 검사한다. 아직 색인 전인 초안 corpus는 헬스체크를 깨뜨리지 않는다.
    """
    problems: list[str] = []
    if not os.getenv("GEMINI_API_KEY"):
        problems.append("GEMINI_API_KEY is missing")
    if config.is_production() and not os.getenv("RAG_API_TOKEN"):
        problems.append("RAG_API_TOKEN is missing")
    if config.session_secret_is_ephemeral:
        problems.append("SESSION_SECRET is missing")

    published = corpora.list_published()
    if not published:
        problems.append("no published corpus")

    readiness: list[CorpusReadiness] = []
    total_chunks = 0
    for cfg in published:
        corpus_problems: list[str] = []
        indexed = 0
        try:
            indexed = await asyncio.to_thread(count_documents, cfg.active_collection)
            if indexed == 0:
                corpus_problems.append("search index is empty")
        except Exception:
            logger.exception("준비 상태 확인 중 ChromaDB 접근 실패: %s", cfg.id)
            corpus_problems.append("search index is unavailable")

        total_chunks += indexed
        problems.extend(f"{cfg.id}: {problem}" for problem in corpus_problems)
        readiness.append(
            CorpusReadiness(
                corpus=cfg.id,
                corpus_id=cfg.corpus_id,
                index_version=cfg.index_version,
                indexed_chunks=indexed,
                problems=corpus_problems,
            )
        )

    if problems:
        response.status_code = 503

    # 최상위 corpus_id/index_version은 기존 연동 호환을 위해 시드 corpus 값을 쓴다.
    seed = next(
        (cfg for cfg in published if cfg.id == corpora.SEED_CORPUS_ID),
        published[0] if published else None,
    )
    return ReadyResponse(
        status="ready" if not problems else "not_ready",
        indexed_chunks=total_chunks,
        corpus_id=seed.corpus_id if seed else "",
        index_version=seed.index_version if seed else "",
        problems=problems,
        corpora=readiness,
    )


async def _search_with_backpressure(cfg, req: SearchRequest) -> list[dict]:
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
                cfg,
                req.text,
                req.top_k,
                req.search_options(),
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


@app.get("/v1/corpora", response_model=CorpusListResponse)
async def list_corpora() -> CorpusListResponse:
    """검색 가능한 corpus 목록. 초안·비공개 corpus는 나오지 않는다."""
    items: list[CorpusInfo] = []
    for cfg in corpora.list_published():
        indexed = await asyncio.to_thread(count_documents, cfg.active_collection)
        items.append(
            CorpusInfo(
                corpus=cfg.id,
                label=cfg.label,
                kind=cfg.kind,
                corpus_id=cfg.corpus_id,
                index_version=cfg.index_version,
                indexed_chunks=indexed,
            )
        )
    return CorpusListResponse(corpora=items, count=len(items))


@app.post(
    "/v1/corpora/{corpus_id}/search",
    response_model=CorpusSearchResponse,
    dependencies=[Depends(require_service_token)],
)
async def search_corpus(corpus_id: str, req: SearchRequest) -> CorpusSearchResponse:
    """corpus 지정 유사 자료 검색."""
    cfg = _resolve_published(corpus_id)
    raw_results = await _search_with_backpressure(cfg, req)
    results = [
        CorpusResultItem(
            document_id=item["document_id"],
            title=item["title"],
            similarity=item["similarity"],
            snippet=item["snippet"],
            metadata=item["metadata"],
        )
        for item in raw_results
    ]
    return CorpusSearchResponse(
        query=req.text,
        corpus=cfg.id,
        results=results,
        count=len(results),
        corpus_id=cfg.corpus_id,
        index_version=cfg.index_version,
    )


@app.post(
    "/v1/search",
    response_model=SearchResponseV1,
    deprecated=True,
    dependencies=[Depends(require_service_token)],
)
async def search_v1(req: SearchRequest) -> SearchResponseV1:
    """하위 호환 API. 신규 연동은 /v1/corpora/{corpus_id}/search 를 사용한다."""
    cfg = _resolve_published(corpora.SEED_CORPUS_ID)
    raw_results = await _search_with_backpressure(cfg, req)
    results = [
        PublicResultItem(
            document_id=item["document_id"],
            title=item["title"],
            year=item["metadata"].get("year"),
            category=item["metadata"].get("category", ""),
            doc_type=item["metadata"].get("doc_type", ""),
            similarity=item["similarity"],
            snippet=item["snippet"],
        )
        for item in raw_results
    ]
    return SearchResponseV1(
        query=req.text,
        results=results,
        count=len(results),
        corpus_id=cfg.corpus_id,
        index_version=cfg.index_version,
    )


@app.post(
    "/search",
    response_model=SearchResponse,
    deprecated=True,
    dependencies=[Depends(require_service_token)],
)
async def search_endpoint(req: SearchRequest) -> SearchResponse:
    """하위 호환 API. 내부 경로와 저자명을 포함하므로 신규 연동에 쓰지 않는다."""
    cfg = _resolve_published(corpora.SEED_CORPUS_ID)
    raw_results = await _search_with_backpressure(cfg, req)
    results = []
    for item in raw_results:
        meta = item.get("_raw_metadata", {})
        year = meta.get("year")
        results.append(
            ResultItem(
                title=item["title"],
                year=None if year == -1 else year,
                category=meta.get("category", ""),
                author=meta.get("author", ""),
                doc_type=meta.get("doc_type", ""),
                source_path=item["source_path"],
                similarity=item["similarity"],
                snippet=item["snippet"],
            )
        )
    return SearchResponse(query=req.text, results=results, count=len(results))
