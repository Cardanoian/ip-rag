"""FastAPI 유사 자료 검색 API — corpus별 컬렉션을 서빙한다."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
import time
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from api.services import Principal, check_rate, failure, configured
from api.sources import router as source_router, worker as source_worker
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
    if config.is_production() and not configured():
        logger.error("production 환경에 서비스 인증이 설정되지 않았습니다.")
    if config.session_secret_is_ephemeral:
        logger.error(
            "SESSION_SECRET이 설정되지 않아 임시 키로 기동했습니다. "
            "서버를 재시작할 때마다 어드민 로그인이 풀립니다."
        )
    stop = asyncio.Event()
    service_worker = asyncio.create_task(source_worker(stop))
    try:
        yield
    finally:
        stop.set()
        await service_worker


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
app.include_router(source_router)


@app.middleware("http")
async def request_trace(request: Request, call_next):
    request.state.request_id = uuid.uuid4().hex
    started = time.monotonic()
    if "/sources/" in request.url.path and request.method in {"PUT", "PATCH", "DELETE"}:
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 6 * 1024 * 1024:
                return JSONResponse({"detail": "요청 크기 제한을 초과했습니다", "error": {"code": "request_too_large"}}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    if request.url.path.startswith("/v1/"):
        corpus_name = request.path_params.get("corpus_id", request.path_params.get("corpus", "-"))
        if corpus_name != "-" and not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,30}", corpus_name):
            corpus_name = "invalid"
        logger.info("rag_request request_id=%s service=%s corpus=%s status=%s duration_ms=%d",
                    request.state.request_id, getattr(request.state, "service_id", "unknown"),
                    corpus_name, response.status_code, int((time.monotonic() - started) * 1000))
    return response


@app.exception_handler(StarletteHTTPException)
async def api_error(request: Request, exc):
    if not request.url.path.startswith(("/v1/", "/search")):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": exc.detail}
    defaults = {401: "invalid_service_token", 403: "operation_not_allowed", 404: "corpus_not_available", 422: "invalid_request"}
    return JSONResponse({"detail": detail.get("message", "요청을 처리하지 못했습니다"),
                         "error": {"code": detail.get("code", defaults.get(exc.status_code, "request_failed"))},
                         "request_id": getattr(request.state, "request_id", "")},
                        status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def invalid_input(request: Request, exc):
    return JSONResponse({"detail": "입력이 API 계약에 맞지 않습니다", "error": {"code": "invalid_request"},
                         "request_id": getattr(request.state, "request_id", "")}, status_code=422)



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


def _resolve_published(corpus_id: str, principal: Principal):
    principal.authorize(corpus_id)
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
    """공통 엔진 의존성만 확인한다. 자료집 정보는 인증된 개별 점검에서 제공한다."""
    import storage
    problems = []
    if not os.getenv("GEMINI_API_KEY"):
        problems.append("GEMINI_API_KEY is missing")
    if config.session_secret_is_ephemeral:
        problems.append("SESSION_SECRET is missing")
    with storage.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM service_tokens WHERE active=1")
        has_tokens = cur.fetchone()["n"] > 0
    if config.is_production() and not os.getenv("RAG_API_TOKEN") and not has_tokens:
        problems.append("service authentication is missing")
    try:
        from ingest.store import get_client
        await asyncio.to_thread(get_client)
    except Exception:
        problems.append("search store is unavailable")
    if problems:
        response.status_code = 503
    return ReadyResponse(status="not_ready" if problems else "ready", indexed_chunks=0,
                         corpus_id="", index_version="", problems=problems, corpora=[])


@app.get("/v1/corpora/{corpus_id}/ready")
async def corpus_ready(corpus_id: str, response: Response, principal: Principal = Depends(require_service_token)):
    cfg = _resolve_published(corpus_id, principal)
    count = await asyncio.to_thread(count_documents, cfg.active_collection)
    if not os.getenv("GEMINI_API_KEY") or count == 0:
        response.status_code = 503
    return {"status": "ready" if response.status_code != 503 else "not_ready",
            "corpus": cfg.id, "indexed_chunks": count, "index_version": cfg.index_version}


_SERVICE_SEMAPHORES = {}


async def _service_search(principal, cfg, req):
    key = (asyncio.get_running_loop(), principal.service_id)
    limit = max(1, int(os.getenv("SEARCH_SERVICE_CONCURRENCY", "5")))
    semaphore = _SERVICE_SEMAPHORES.setdefault(key, asyncio.Semaphore(limit))
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=config.SEARCH_QUEUE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise failure(429, "rate_limited", "서비스 검색 대기 한도를 초과했습니다", {"Retry-After": "1"})
    try:
        return await _search_with_backpressure(cfg, req)
    finally:
        semaphore.release()


async def _search_with_backpressure(cfg, req: SearchRequest) -> list[dict]:
    """동기 Gemini/Chroma 작업을 이벤트 루프 밖에서 실행한다."""
    try:
        await asyncio.wait_for(
            _SEARCH_SEMAPHORE.acquire(),
            timeout=config.SEARCH_QUEUE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise failure(503, "search_busy", "서버가 바쁩니다. 잠시 후 다시 시도하세요.")

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
            logger.error("임베딩 또는 검색 저장소를 사용할 수 없음")
            raise failure(503, "dependency_unavailable", "검색 서비스를 일시적으로 사용할 수 없습니다.")
        except Exception as exc:
            logger.error("검색 중 예기치 않은 오류: %s", type(exc).__name__)
            raise HTTPException(
                status_code=500,
                detail="검색 중 내부 오류가 발생했습니다.",
            )
    finally:
        _SEARCH_SEMAPHORE.release()


@app.get("/v1/corpora", response_model=CorpusListResponse)
async def list_corpora(principal: Principal = Depends(require_service_token)) -> CorpusListResponse:
    """검색 가능한 corpus 목록. 초안·비공개 corpus는 나오지 않는다."""
    items: list[CorpusInfo] = []
    for cfg in corpora.list_published():
        if not principal.allows(cfg.id):
            continue
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
async def search_corpus(corpus_id: str, req: SearchRequest, principal: Principal = Depends(require_service_token)) -> CorpusSearchResponse:
    """corpus 지정 유사 자료 검색."""
    cfg = _resolve_published(corpus_id, principal)
    check_rate(principal)
    raw_results = await _service_search(principal, cfg, req)
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
        notice=("검색 결과는 승인된 학교 업무 자료에서 찾은 참고 정보입니다. 원문과 적용 시점을 확인하세요."
                if cfg.kind == "easyflow" else CorpusSearchResponse.model_fields["notice"].default),
    )


@app.post(
    "/v1/search",
    response_model=SearchResponseV1,
    deprecated=True,
    dependencies=[Depends(require_service_token)],
)
async def search_v1(req: SearchRequest, principal: Principal = Depends(require_service_token)) -> SearchResponseV1:
    """하위 호환 API. 신규 연동은 /v1/corpora/{corpus_id}/search 를 사용한다."""
    cfg = _resolve_published(corpora.SEED_CORPUS_ID, principal)
    check_rate(principal)
    raw_results = await _service_search(principal, cfg, req)
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
async def search_endpoint(req: SearchRequest, principal: Principal = Depends(require_service_token)) -> SearchResponse:
    """하위 호환 API. 내부 경로와 저자명을 포함하므로 신규 연동에 쓰지 않는다."""
    cfg = _resolve_published(corpora.SEED_CORPUS_ID, principal)
    check_rate(principal)
    raw_results = await _service_search(principal, cfg, req)
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
