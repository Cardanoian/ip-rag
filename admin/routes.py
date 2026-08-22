"""어드민 라우터 — 로그인, corpus 관리, 문서·색인, 계정 관리.

권한은 두 단계다. 대부분의 라우트는 `require_admin`(로그인한 모든 관리자)이고,
계정 관리와 corpus 완전삭제만 `require_superadmin`이다.
모든 POST는 CSRF 토큰을 검증한다.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import config
import corpora
from admin import audit, documents, jobs
from admin.auth import (
    AdminUser,
    AuthError,
    authenticate,
    create_user,
    delete_user,
    generate_password,
    get_by_id,
    list_users,
    set_active,
    set_password,
    transfer_superadmin,
    verify_password,
)
from admin.permissions import (
    login_session,
    logout_session,
    require_admin,
    require_superadmin,
    verify_csrf,
)
from admin.templating import flash, render
from corpora.models import (
    CorpusValidationError,
    rebuild_required_changes,
    validate_chunking,
    validate_embed_dim,
)
from ingest.store import count_documents as count_indexed_chunks
from ingest.store import drop_collection, list_collections

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _redirect(url: str) -> RedirectResponse:
    """POST-redirect-GET. 303이라야 브라우저가 GET으로 따라간다."""
    return RedirectResponse(url=url, status_code=303)


def _get_corpus(corpus_id: str):
    try:
        return corpora.get(corpus_id)
    except corpora.CorpusNotFound:
        raise HTTPException(status_code=404, detail="등록되지 않은 corpus입니다.")


# ---------------------------------------------------------------------------
# 로그인
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/admin/") -> HTMLResponse:
    from admin.permissions import current_user

    if current_user(request) is not None:
        return _redirect("/admin/")
    return render(request, "login.html", {"next_url": next})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf: str = Form(""),
    next: str = Form("/admin/"),
):
    verify_csrf(request, csrf)
    try:
        user = authenticate(username, password)
    except AuthError as exc:
        audit.record(None, "login.failed", username)
        return render(
            request,
            "login.html",
            {"error": str(exc), "username": username, "next_url": next},
            status_code=401,
        )

    login_session(request, user)
    audit.record(user.username, "login.succeeded", user.username)
    # 오픈 리다이렉트 방지: 내부 어드민 경로만 허용한다.
    target = next if next.startswith("/admin/") else "/admin/"
    return _redirect(target)


@router.post("/logout")
async def logout(request: Request, csrf: str = Form("")):
    verify_csrf(request, csrf)
    logout_session(request)
    return _redirect("/admin/login")


# ---------------------------------------------------------------------------
# 대시보드
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: AdminUser = Depends(require_admin),
) -> HTMLResponse:
    overview = []
    for cfg in corpora.list_all():
        overview.append(
            {
                "cfg": cfg,
                "documents": documents.count_documents(cfg),
                "chunks": count_indexed_chunks(cfg.active_collection),
                "active_job": jobs.active_job_for(cfg.id),
            }
        )
    return render(
        request,
        "dashboard.html",
        {"overview": overview, "recent_jobs": jobs.list_recent(10)},
    )


# ---------------------------------------------------------------------------
# corpus 생성·설정
# ---------------------------------------------------------------------------


@router.get("/corpora/new", response_class=HTMLResponse)
async def corpus_new_form(
    request: Request,
    user: AdminUser = Depends(require_admin),
) -> HTMLResponse:
    return render(
        request,
        "corpus_new.html",
        {"kinds": corpora.creatable_kinds(), "form": {}},
    )


@router.post("/corpora/new")
async def corpus_create(
    request: Request,
    user: AdminUser = Depends(require_admin),
    csrf: str = Form(""),
    corpus_slug: str = Form(...),
    label: str = Form(...),
    kind: str = Form("plain"),
    corpus_id: str = Form(""),
    doc_prefix: str = Form(...),
    query_prefix: str = Form(...),
    embed_dim: int = Form(1536),
    chunk_size: int = Form(1000),
    chunk_overlap: int = Form(150),
    single_chunk_char_hint: int = Form(5500),
):
    verify_csrf(request, csrf)
    form = {
        "corpus_slug": corpus_slug,
        "label": label,
        "kind": kind,
        "corpus_id": corpus_id,
        "doc_prefix": doc_prefix,
        "query_prefix": query_prefix,
        "embed_dim": embed_dim,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "single_chunk_char_hint": single_chunk_char_hint,
    }
    try:
        cfg = corpora.create(
            corpus_slug=corpus_slug,
            label=label,
            kind=kind,
            corpus_id=corpus_id,
            doc_prefix=doc_prefix,
            query_prefix=query_prefix,
            embed_dim=embed_dim,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            single_chunk_char_hint=single_chunk_char_hint,
            created_by=user.username,
        )
    except (CorpusValidationError, KeyError) as exc:
        return render(
            request,
            "corpus_new.html",
            {
                "kinds": corpora.creatable_kinds(),
                "form": form,
                "error": str(exc).strip("'"),
            },
            status_code=400,
        )

    audit.record(user.username, "corpus.created", cfg.id, label=cfg.label, kind=cfg.kind)
    flash(request, f"'{cfg.label}' corpus를 만들었습니다. 문서를 올려 색인하세요.", "success")
    return _redirect(f"/admin/corpora/{cfg.id}")


@router.get("/corpora/{corpus_id}", response_class=HTMLResponse)
async def corpus_detail(
    corpus_id: str,
    request: Request,
    q: str = "",
    user: AdminUser = Depends(require_admin),
) -> HTMLResponse:
    cfg = _get_corpus(corpus_id)
    all_documents = documents.list_documents(cfg)
    document_list = (
        all_documents if not q.strip() else documents.list_documents(cfg, q)
    )
    return render(
        request,
        "corpus_detail.html",
        {
            "cfg": cfg,
            "kind": corpora.kind_of(cfg),
            "documents": document_list,
            "document_count": len(all_documents),
            "repairable_filenames": documents.count_repairable_filenames(
                cfg, all_documents
            ),
            "query": q,
            "chunks": count_indexed_chunks(cfg.active_collection),
            "jobs": jobs.list_for_corpus(cfg.id),
            "active_job": jobs.active_job_for(cfg.id),
            "max_upload_mb": config.MAX_UPLOAD_BYTES // (1024 * 1024),
        },
    )


@router.get("/corpora/{corpus_id}/settings", response_class=HTMLResponse)
async def corpus_settings_form(
    corpus_id: str,
    request: Request,
    user: AdminUser = Depends(require_admin),
) -> HTMLResponse:
    cfg = _get_corpus(corpus_id)
    return render(request, "corpus_settings.html", {"cfg": cfg})


@router.post("/corpora/{corpus_id}/settings")
async def corpus_settings_save(
    corpus_id: str,
    request: Request,
    user: AdminUser = Depends(require_admin),
    csrf: str = Form(""),
    label: str = Form(...),
    corpus_id_value: str = Form(...),
    doc_prefix: str = Form(...),
    query_prefix: str = Form(...),
    embed_dim: int = Form(...),
    chunk_size: int = Form(...),
    chunk_overlap: int = Form(...),
    single_chunk_char_hint: int = Form(...),
):
    verify_csrf(request, csrf)
    cfg = _get_corpus(corpus_id)

    try:
        dim = validate_embed_dim(embed_dim)
        size, overlap, hint = validate_chunking(
            chunk_size, chunk_overlap, single_chunk_char_hint
        )
        label = (label or "").strip()
        if not label:
            raise CorpusValidationError("이름을 입력하세요.")
        doc_prefix = (doc_prefix or "").strip()
        query_prefix = (query_prefix or "").strip()
        if not doc_prefix or not query_prefix:
            raise CorpusValidationError("문서·질의 지시문을 모두 입력하세요.")
    except CorpusValidationError as exc:
        return render(
            request,
            "corpus_settings.html",
            {"cfg": cfg, "error": str(exc)},
            status_code=400,
        )

    changes = {
        "label": label,
        "corpus_id": (corpus_id_value or "").strip() or cfg.corpus_id,
        "doc_prefix": doc_prefix + "\n",
        "query_prefix": query_prefix + "\n",
        "embed_dim": dim,
        "chunk_size": size,
        "chunk_overlap": overlap,
        "single_chunk_char_hint": hint,
    }
    candidate = cfg.with_updates(**changes)
    stale_fields = rebuild_required_changes(cfg, candidate)
    if stale_fields:
        changes["needs_rebuild"] = True

    updated = corpora.update(cfg, **changes)
    audit.record(
        user.username,
        "corpus.settings_updated",
        cfg.id,
        rebuild_required=sorted(stale_fields),
    )

    if "embed_dim" in stale_fields:
        # 차원이 어긋나면 다음 검색 질의부터 즉시 깨진다. 전환 재색인을 바로 건다.
        try:
            job = jobs.enqueue(updated.id, jobs.KIND_REBUILD, user.username)
            flash(
                request,
                f"임베딩 차원이 바뀌어 전체 재색인을 시작했습니다 (작업 #{job.id}). "
                "완료 전까지는 이전 색인으로 검색됩니다.",
                "warning",
            )
        except jobs.JobError as exc:
            flash(request, f"재색인을 시작하지 못했습니다: {exc}", "error")
    elif stale_fields:
        flash(
            request,
            "설정을 저장했습니다. 색인에 영향을 주는 항목이 바뀌었으니 "
            "전체 재색인을 실행하세요.",
            "warning",
        )
    else:
        flash(request, "설정을 저장했습니다.", "success")

    return _redirect(f"/admin/corpora/{updated.id}")


@router.post("/corpora/{corpus_id}/publish")
async def corpus_publish(
    corpus_id: str,
    request: Request,
    user: AdminUser = Depends(require_admin),
    csrf: str = Form(""),
):
    verify_csrf(request, csrf)
    cfg = _get_corpus(corpus_id)
    try:
        corpora.set_status(cfg, corpora.STATUS_PUBLISHED)
    except CorpusValidationError as exc:
        flash(request, str(exc), "error")
        return _redirect(f"/admin/corpora/{corpus_id}")

    audit.record(user.username, "corpus.published", cfg.id)
    flash(request, f"'{cfg.label}'을(를) 공개했습니다. 검색 API에서 조회됩니다.", "success")
    return _redirect(f"/admin/corpora/{corpus_id}")


@router.post("/corpora/{corpus_id}/unpublish")
async def corpus_unpublish(
    corpus_id: str,
    request: Request,
    user: AdminUser = Depends(require_admin),
    csrf: str = Form(""),
):
    verify_csrf(request, csrf)
    cfg = _get_corpus(corpus_id)
    corpora.set_status(cfg, corpora.STATUS_UNPUBLISHED)
    audit.record(user.username, "corpus.unpublished", cfg.id)
    flash(
        request,
        f"'{cfg.label}'을(를) 비공개로 전환했습니다. 데이터는 그대로 남아 있습니다.",
        "success",
    )
    return _redirect(f"/admin/corpora/{corpus_id}")


@router.post("/corpora/{corpus_id}/destroy")
async def corpus_destroy(
    corpus_id: str,
    request: Request,
    user: AdminUser = Depends(require_superadmin),
    csrf: str = Form(""),
    confirm: str = Form(""),
):
    """완전삭제 — 비공개 상태에서 corpus 주소를 정확히 입력해야 실행된다."""
    verify_csrf(request, csrf)
    cfg = _get_corpus(corpus_id)

    if confirm.strip() != cfg.id:
        flash(request, "확인을 위해 corpus 주소를 정확히 입력하세요.", "error")
        return _redirect(f"/admin/corpora/{corpus_id}")

    active = jobs.active_job_for(cfg.id)
    if active is not None:
        flash(request, f"진행 중인 색인 작업(#{active.id})이 끝난 뒤 삭제하세요.", "error")
        return _redirect(f"/admin/corpora/{corpus_id}")

    try:
        corpora.delete(cfg)
    except CorpusValidationError as exc:
        flash(request, str(exc), "error")
        return _redirect(f"/admin/corpora/{corpus_id}")

    # DB 행이 지워진 뒤에 파일과 컬렉션을 정리한다. 순서가 반대면 삭제가 중간에
    # 실패했을 때 색인 없는 corpus가 남는다.
    removed_docs = documents.delete_all_documents(cfg)
    removed_collections = [
        name
        for name in list_collections()
        if name == cfg.base_collection or name.startswith(f"{cfg.base_collection}_v")
    ]
    for name in removed_collections:
        drop_collection(name)

    audit.record(
        user.username,
        "corpus.destroyed",
        cfg.id,
        documents=removed_docs,
        collections=removed_collections,
    )
    flash(
        request,
        f"'{cfg.label}'을(를) 완전히 삭제했습니다 "
        f"(문서 {removed_docs}건, 컬렉션 {len(removed_collections)}개).",
        "success",
    )
    return _redirect("/admin/")


# ---------------------------------------------------------------------------
# 문서
# ---------------------------------------------------------------------------


@router.post("/corpora/{corpus_id}/documents")
async def upload_documents(
    corpus_id: str,
    request: Request,
    user: AdminUser = Depends(require_admin),
    csrf: str = Form(""),
    files: list[UploadFile] = None,
):
    verify_csrf(request, csrf)
    cfg = _get_corpus(corpus_id)

    payload: list[tuple[str, bytes]] = []
    for upload in files or []:
        if not upload.filename:
            continue
        payload.append((upload.filename, await upload.read()))

    if not payload:
        flash(request, "올릴 파일을 선택하세요.", "error")
        return _redirect(f"/admin/corpora/{corpus_id}")

    try:
        # zip 해제와 수천 개 파일 쓰기는 통째로 블로킹이다. 단일 워커라
        # 이벤트 루프에서 그대로 돌리면 그동안 검색도 /health 도 멈춘다.
        result = await asyncio.to_thread(documents.save_uploads, cfg, payload)
    except documents.UploadError as exc:
        flash(request, str(exc), "error")
        return _redirect(f"/admin/corpora/{corpus_id}")

    audit.record(
        user.username,
        "documents.uploaded",
        cfg.id,
        saved=result.saved_count,
        rejected=len(result.rejected),
    )

    if result.saved:
        flash(request, f"{result.saved_count}개 파일을 올렸습니다.", "success")
    for name, reason in result.rejected[:10]:
        flash(request, f"거부됨 — {name}: {reason}", "error")
    if len(result.rejected) > 10:
        flash(request, f"그 외 {len(result.rejected) - 10}건이 더 거부되었습니다.", "error")

    return _redirect(f"/admin/corpora/{corpus_id}")


@router.post("/corpora/{corpus_id}/documents/repair-filenames")
async def repair_document_filenames(
    corpus_id: str,
    request: Request,
    user: AdminUser = Depends(require_admin),
    csrf: str = Form(""),
):
    """예전 ZIP 업로드에서 CP437로 잘못 저장된 UTF-8/CP949 이름을 복구한다."""
    verify_csrf(request, csrf)
    cfg = _get_corpus(corpus_id)
    active = jobs.active_job_for(cfg.id)
    if active is not None:
        flash(
            request,
            f"진행 중인 색인 작업(#{active.id})이 끝난 뒤 파일명을 복구하세요.",
            "error",
        )
        return _redirect(f"/admin/corpora/{corpus_id}")

    result = await asyncio.to_thread(documents.repair_legacy_zip_filenames, cfg)

    if result.renamed:
        corpora.update(cfg, needs_rebuild=True)
    audit.record(
        user.username,
        "documents.filenames_repaired",
        cfg.id,
        renamed=len(result.renamed),
        skipped=len(result.skipped),
    )

    if result.renamed:
        flash(
            request,
            f"한글 파일명 {len(result.renamed)}개를 복구했습니다. "
            "검색 결과의 제목도 고치려면 전체 재색인을 실행하세요.",
            "success",
        )
    else:
        flash(request, "복구할 파일명이 없습니다.", "info")
    if result.skipped:
        flash(
            request,
            f"이름 충돌 또는 파일 오류로 {len(result.skipped)}개는 복구하지 못했습니다.",
            "error",
        )
    return _redirect(f"/admin/corpora/{corpus_id}")


@router.post("/corpora/{corpus_id}/documents/delete-all")
async def delete_all_documents_route(
    corpus_id: str,
    request: Request,
    user: AdminUser = Depends(require_admin),
    csrf: str = Form(""),
    confirm: str = Form(""),
):
    """개별 체크박스나 폼 필드 개수 제한 없이 corpus 문서 전체를 삭제한다."""
    verify_csrf(request, csrf)
    cfg = _get_corpus(corpus_id)

    if confirm.strip() != cfg.id:
        flash(request, "전체 삭제 확인을 위해 corpus 주소를 정확히 입력하세요.", "error")
        return _redirect(f"/admin/corpora/{corpus_id}")

    active = jobs.active_job_for(cfg.id)
    if active is not None:
        flash(
            request,
            f"진행 중인 색인 작업(#{active.id})이 끝난 뒤 전체 삭제하세요.",
            "error",
        )
        return _redirect(f"/admin/corpora/{corpus_id}")

    result = await asyncio.to_thread(documents.purge_documents, cfg)
    if cfg.needs_rebuild:
        corpora.update(cfg, needs_rebuild=False)
    audit.record(
        user.username,
        "documents.all_deleted",
        cfg.id,
        documents=result.removed_documents,
        chunks=result.removed_chunks,
        collections=result.removed_collections,
    )
    flash(
        request,
        f"문서 {result.removed_documents}개와 검색 색인 "
        f"{result.removed_chunks}개를 모두 삭제했습니다.",
        "success",
    )
    return _redirect(f"/admin/corpora/{corpus_id}")


@router.post("/corpora/{corpus_id}/documents/delete")
async def delete_documents_route(
    corpus_id: str,
    request: Request,
    user: AdminUser = Depends(require_admin),
    csrf: str = Form(""),
    filenames: list[str] = Form(default=[]),
):
    verify_csrf(request, csrf)
    cfg = _get_corpus(corpus_id)

    if not filenames:
        flash(request, "삭제할 문서를 선택하세요.", "error")
        return _redirect(f"/admin/corpora/{corpus_id}")

    deleted, failed = documents.delete_documents(cfg, filenames)
    audit.record(
        user.username,
        "documents.deleted",
        cfg.id,
        deleted=len(deleted),
        failed=len(failed),
        names=deleted[:20],
    )

    if deleted:
        flash(request, f"{len(deleted)}개 문서를 삭제했습니다.", "success")
    if failed:
        flash(request, f"{len(failed)}개 문서를 삭제하지 못했습니다.", "error")
    return _redirect(f"/admin/corpora/{corpus_id}")


# ---------------------------------------------------------------------------
# 색인 잡
# ---------------------------------------------------------------------------


@router.post("/corpora/{corpus_id}/reindex")
async def reindex(
    corpus_id: str,
    request: Request,
    user: AdminUser = Depends(require_admin),
    csrf: str = Form(""),
    mode: str = Form(jobs.KIND_INCREMENTAL),
):
    verify_csrf(request, csrf)
    cfg = _get_corpus(corpus_id)

    try:
        job = jobs.enqueue(cfg.id, mode, user.username)
    except jobs.JobError as exc:
        flash(request, str(exc), "error")
        return _redirect(f"/admin/corpora/{corpus_id}")

    if mode == jobs.KIND_REBUILD:
        flash(
            request,
            f"전체 재색인을 시작했습니다 (작업 #{job.id}). "
            "완료될 때까지 검색은 기존 색인으로 정상 동작합니다.",
            "success",
        )
    else:
        flash(request, f"변경분 색인을 시작했습니다 (작업 #{job.id}).", "success")
    return _redirect(f"/admin/corpora/{corpus_id}")


@router.get("/jobs/{job_id}")
async def job_status(
    job_id: int,
    user: AdminUser = Depends(require_admin),
) -> JSONResponse:
    """진행률 폴링용 JSON."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    return JSONResponse(
        {
            "id": job.id,
            "corpus": job.corpus_id,
            "kind": job.kind,
            "status": job.status,
            "is_active": job.is_active,
            "progress_current": job.progress_current,
            "progress_total": job.progress_total,
            "progress_percent": job.progress_percent,
            "stats": job.stats,
            "error": job.error,
        }
    )


# ---------------------------------------------------------------------------
# 검색 테스트 콘솔
# ---------------------------------------------------------------------------


@router.get("/corpora/{corpus_id}/console", response_class=HTMLResponse)
async def console_form(
    corpus_id: str,
    request: Request,
    user: AdminUser = Depends(require_admin),
) -> HTMLResponse:
    cfg = _get_corpus(corpus_id)
    return render(request, "console.html", {"cfg": cfg, "results": None, "query": ""})


@router.post("/corpora/{corpus_id}/console", response_class=HTMLResponse)
async def console_search(
    corpus_id: str,
    request: Request,
    user: AdminUser = Depends(require_admin),
    csrf: str = Form(""),
    query: str = Form(...),
    top_k: int = Form(5),
    include_advisor_docs: bool = Form(False),
) -> HTMLResponse:
    """공개 전 corpus도 여기서는 검색할 수 있다 — 색인 품질 점검이 목적이다."""
    verify_csrf(request, csrf)
    cfg = _get_corpus(corpus_id)

    import asyncio

    from ingest.search import search

    context = {"cfg": cfg, "query": query, "top_k": top_k, "results": None}
    if not query.strip():
        context["error"] = "질의를 입력하세요."
        return render(request, "console.html", context, status_code=400)

    try:
        results = await asyncio.to_thread(
            search,
            cfg,
            query.strip(),
            max(1, min(int(top_k), 50)),
            {"include_advisor_docs": include_advisor_docs},
        )
    except Exception as exc:
        logger.exception("검색 콘솔 실패: %s", cfg.id)
        context["error"] = f"검색에 실패했습니다 — {type(exc).__name__}: {exc}"
        return render(request, "console.html", context, status_code=500)

    context["results"] = results
    return render(request, "console.html", context)


# ---------------------------------------------------------------------------
# 본인 계정
# ---------------------------------------------------------------------------


@router.get("/account/password", response_class=HTMLResponse)
async def password_form(
    request: Request,
    user: AdminUser = Depends(require_admin),
) -> HTMLResponse:
    return render(request, "password.html", {})


@router.post("/account/password")
async def password_change(
    request: Request,
    user: AdminUser = Depends(require_admin),
    csrf: str = Form(""),
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    verify_csrf(request, csrf)

    import storage

    with storage.cursor() as cur:
        cur.execute(
            "SELECT password_hash, salt FROM admin_users WHERE id = ?", (user.id,)
        )
        row = cur.fetchone()

    if row is None or not verify_password(
        current_password, row["password_hash"], row["salt"]
    ):
        return render(
            request,
            "password.html",
            {"error": "현재 비밀번호가 올바르지 않습니다."},
            status_code=400,
        )
    if new_password != confirm_password:
        return render(
            request,
            "password.html",
            {"error": "새 비밀번호가 서로 다릅니다."},
            status_code=400,
        )

    try:
        set_password(user, new_password)
    except AuthError as exc:
        return render(
            request, "password.html", {"error": str(exc)}, status_code=400
        )

    audit.record(user.username, "account.password_changed", user.username)
    # session_version이 올라갔으므로 현재 세션도 무효다. 다시 로그인시킨다.
    logout_session(request)
    return _redirect("/admin/login")


# ---------------------------------------------------------------------------
# 계정 관리 (최고관리자 전용)
# ---------------------------------------------------------------------------


@router.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    user: AdminUser = Depends(require_superadmin),
) -> HTMLResponse:
    return render(request, "users.html", {"users": list_users()})


@router.post("/users")
async def users_create(
    request: Request,
    user: AdminUser = Depends(require_superadmin),
    csrf: str = Form(""),
    username: str = Form(...),
    password: str = Form(""),
):
    verify_csrf(request, csrf)
    generated = not password.strip()
    if generated:
        password = generate_password()

    try:
        created = create_user(
            username, password, role="admin", created_by=user.username
        )
    except AuthError as exc:
        flash(request, str(exc), "error")
        return _redirect("/admin/users")

    audit.record(user.username, "user.created", created.username)
    if generated:
        flash(
            request,
            f"'{created.username}' 계정을 만들었습니다. "
            f"임시 비밀번호: {password} (이 화면을 벗어나면 다시 볼 수 없습니다)",
            "success",
        )
    else:
        flash(request, f"'{created.username}' 계정을 만들었습니다.", "success")
    return _redirect("/admin/users")


def _target_user(user_id: int) -> AdminUser:
    target = get_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="계정을 찾을 수 없습니다.")
    return target


@router.post("/users/{user_id}/deactivate")
async def users_toggle_active(
    user_id: int,
    request: Request,
    user: AdminUser = Depends(require_superadmin),
    csrf: str = Form(""),
    activate: bool = Form(False),
):
    verify_csrf(request, csrf)
    target = _target_user(user_id)

    if target.id == user.id:
        flash(request, "자기 계정은 비활성화할 수 없습니다.", "error")
        return _redirect("/admin/users")

    try:
        set_active(target, activate)
    except AuthError as exc:
        flash(request, str(exc), "error")
        return _redirect("/admin/users")

    action = "user.activated" if activate else "user.deactivated"
    audit.record(user.username, action, target.username)
    flash(
        request,
        f"'{target.username}' 계정을 {'활성화' if activate else '비활성화'}했습니다.",
        "success",
    )
    return _redirect("/admin/users")


@router.post("/users/{user_id}/delete")
async def users_delete(
    user_id: int,
    request: Request,
    user: AdminUser = Depends(require_superadmin),
    csrf: str = Form(""),
):
    verify_csrf(request, csrf)
    target = _target_user(user_id)

    if target.id == user.id:
        flash(request, "자기 계정은 삭제할 수 없습니다.", "error")
        return _redirect("/admin/users")

    try:
        delete_user(target)
    except AuthError as exc:
        flash(request, str(exc), "error")
        return _redirect("/admin/users")

    audit.record(user.username, "user.deleted", target.username)
    flash(request, f"'{target.username}' 계정을 삭제했습니다.", "success")
    return _redirect("/admin/users")


@router.post("/users/{user_id}/reset-password")
async def users_reset_password(
    user_id: int,
    request: Request,
    user: AdminUser = Depends(require_superadmin),
    csrf: str = Form(""),
):
    verify_csrf(request, csrf)
    target = _target_user(user_id)

    temporary = generate_password()
    set_password(target, temporary)
    audit.record(user.username, "user.password_reset", target.username)
    flash(
        request,
        f"'{target.username}'의 임시 비밀번호: {temporary} "
        "(이 화면을 벗어나면 다시 볼 수 없습니다). 기존 로그인 세션은 모두 끊겼습니다.",
        "success",
    )
    return _redirect("/admin/users")


@router.post("/users/{user_id}/transfer-superadmin")
async def users_transfer(
    user_id: int,
    request: Request,
    user: AdminUser = Depends(require_superadmin),
    csrf: str = Form(""),
    confirm: str = Form(""),
):
    """최고관리자 이양. 실행하면 본인은 일반관리자로 내려간다."""
    verify_csrf(request, csrf)
    target = _target_user(user_id)

    if confirm.strip() != target.username:
        flash(request, "확인을 위해 넘길 계정의 아이디를 정확히 입력하세요.", "error")
        return _redirect("/admin/users")

    try:
        transfer_superadmin(user, target)
    except AuthError as exc:
        flash(request, str(exc), "error")
        return _redirect("/admin/users")

    audit.record(user.username, "user.superadmin_transferred", target.username)
    flash(
        request,
        f"최고관리자를 '{target.username}'에게 넘겼습니다. "
        "이제 회원님은 일반관리자입니다.",
        "success",
    )
    return _redirect("/admin/")


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    user: AdminUser = Depends(require_superadmin),
) -> HTMLResponse:
    return render(request, "audit.html", {"entries": audit.recent(300)})
