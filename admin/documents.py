"""corpus 문서 파일 관리 — 업로드, 목록, 삭제.

업로드는 외부 입력이 파일시스템에 직접 닿는 지점이라 방어가 필요하다.
파일명은 basename만 취해 정규화하고, 확장자를 화이트리스트로 거르고,
최종 경로가 corpus 디렉터리 안에 있는지 resolve 후 다시 확인한다.
"""
from __future__ import annotations

import io
import logging
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path

import config
from corpora.kinds import kind_of

logger = logging.getLogger(__name__)


class UploadError(ValueError):
    """업로드 거부 사유. 라우터가 400으로 바꾼다."""


@dataclass(frozen=True)
class DocumentInfo:
    filename: str
    source_path: str
    size_bytes: int
    modified_at: float
    indexed_chunks: int = 0


@dataclass
class UploadResult:
    saved: list[str]
    rejected: list[tuple[str, str]]  # (파일명, 사유)

    @property
    def saved_count(self) -> int:
        return len(self.saved)


def sanitize_filename(raw_name: str, allowed_extensions: tuple[str, ...]) -> str:
    """안전한 basename을 돌려준다. 위험하면 UploadError.

    `../../etc/passwd` 같은 입력은 basename만 남으므로 디렉터리를 벗어날 수 없다.
    """
    # 경로 구분자를 양쪽 스타일 모두 제거한다 (Windows 클라이언트 대비).
    candidate = (raw_name or "").replace("\\", "/").strip()
    name = unicodedata.normalize("NFC", Path(candidate).name).strip()

    if not name or name in {".", ".."} or name.startswith("."):
        raise UploadError("사용할 수 없는 파일명입니다.")
    if "\x00" in name:
        raise UploadError("사용할 수 없는 파일명입니다.")
    if len(name.encode("utf-8")) > 255:
        raise UploadError("파일명이 너무 깁니다.")

    if Path(name).suffix.lower() not in allowed_extensions:
        allowed = ", ".join(allowed_extensions)
        raise UploadError(f"{allowed} 파일만 올릴 수 있습니다.")
    return name


def _target_path(cfg, filename: str) -> Path:
    """corpus 디렉터리 안의 최종 경로. 이탈이 감지되면 거부한다."""
    docs_dir = cfg.docs_dir()
    docs_dir.mkdir(parents=True, exist_ok=True)
    resolved_dir = docs_dir.resolve()
    target = (resolved_dir / filename).resolve()
    if target.parent != resolved_dir:
        raise UploadError("사용할 수 없는 파일명입니다.")
    return target


def save_upload(cfg, filename: str, content: bytes) -> str:
    """파일 하나를 corpus 디렉터리에 저장하고 저장된 이름을 돌려준다."""
    kind = kind_of(cfg)
    safe_name = sanitize_filename(filename, kind.file_extensions)
    target = _target_path(cfg, safe_name)

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise UploadError("UTF-8 텍스트 파일만 올릴 수 있습니다.") from None

    if not text.strip():
        raise UploadError("내용이 비어 있습니다.")

    target.write_text(unicodedata.normalize("NFC", text), encoding="utf-8")
    return safe_name


def save_uploads(cfg, files: list[tuple[str, bytes]]) -> UploadResult:
    """여러 파일을 저장한다. zip은 풀어서 각 항목을 저장한다.

    한 파일이 거부돼도 나머지는 저장한다 — 수십 개를 올릴 때 하나 때문에
    전부 되돌리면 관리자가 원인을 찾기 어렵다.
    """
    result = UploadResult(saved=[], rejected=[])
    total_bytes = sum(len(content) for _, content in files)
    if total_bytes > config.MAX_UPLOAD_BYTES:
        raise UploadError(
            f"한 번에 올릴 수 있는 용량은 "
            f"{config.MAX_UPLOAD_BYTES // (1024 * 1024)}MB 입니다."
        )

    for filename, content in files:
        base = Path((filename or "").replace("\\", "/")).name
        if base.lower().endswith(".zip"):
            _extract_zip(cfg, base, content, result)
            continue
        try:
            result.saved.append(save_upload(cfg, filename, content))
        except UploadError as exc:
            result.rejected.append((base or "(이름 없음)", str(exc)))

    return result


def _extract_zip(cfg, zip_name: str, content: bytes, result: UploadResult) -> None:
    """zip을 풀어 각 항목을 저장한다. zip slip과 zip bomb을 막는다."""
    kind = kind_of(cfg)
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        result.rejected.append((zip_name, "손상된 zip 파일입니다."))
        return

    with archive:
        # 압축 해제 후 총 크기를 먼저 확인한다 (zip bomb 방어).
        declared = sum(info.file_size for info in archive.infolist())
        if declared > config.MAX_UNZIPPED_BYTES:
            result.rejected.append(
                (zip_name, "압축을 풀었을 때 용량이 너무 큽니다.")
            )
            return

        for info in archive.infolist():
            if info.is_dir():
                continue
            entry_label = f"{zip_name}:{info.filename}"
            try:
                # 엔트리 이름에도 동일한 sanitize를 적용한다 (zip slip 방어).
                safe_name = sanitize_filename(info.filename, kind.file_extensions)
                with archive.open(info) as handle:
                    entry_content = handle.read(config.MAX_UNZIPPED_BYTES + 1)
                if len(entry_content) > config.MAX_UNZIPPED_BYTES:
                    raise UploadError("파일이 너무 큽니다.")
                result.saved.append(save_upload(cfg, safe_name, entry_content))
            except UploadError as exc:
                result.rejected.append((entry_label, str(exc)))
            except Exception:
                logger.exception("zip 항목 처리 실패: %s", entry_label)
                result.rejected.append((entry_label, "처리할 수 없는 항목입니다."))


def list_documents(cfg, query: str = "") -> list[DocumentInfo]:
    """corpus 디렉터리의 문서 목록. query가 있으면 파일명으로 거른다."""
    kind = kind_of(cfg)
    docs_dir = cfg.docs_dir()
    if not docs_dir.exists():
        return []

    needle = unicodedata.normalize("NFC", query or "").strip().lower()
    documents: list[DocumentInfo] = []
    for extension in kind.file_extensions:
        for path in docs_dir.glob(f"*{extension}"):
            name = unicodedata.normalize("NFC", path.name)
            if needle and needle not in name.lower():
                continue
            stat = path.stat()
            documents.append(
                DocumentInfo(
                    filename=name,
                    source_path=f"{cfg.id}/{name}",
                    size_bytes=stat.st_size,
                    modified_at=stat.st_mtime,
                )
            )
    documents.sort(key=lambda doc: doc.filename)
    return documents


def count_documents(cfg) -> int:
    return len(list_documents(cfg))


def delete_documents(cfg, filenames: list[str]) -> tuple[list[str], list[str]]:
    """파일과 해당 색인 청크를 함께 지운다. (삭제됨, 실패) 반환."""
    from ingest.build_index import remove_document

    kind = kind_of(cfg)
    deleted: list[str] = []
    failed: list[str] = []

    for raw_name in filenames:
        try:
            safe_name = sanitize_filename(raw_name, kind.file_extensions)
            target = _target_path(cfg, safe_name)
        except UploadError:
            failed.append(raw_name)
            continue

        try:
            if target.exists():
                target.unlink()
            # 파일이 이미 없어도 색인에는 남아 있을 수 있으므로 청크 삭제는 항상 시도한다.
            remove_document(cfg, f"{cfg.id}/{safe_name}")
            deleted.append(safe_name)
        except Exception:
            logger.exception("문서 삭제 실패: %s", safe_name)
            failed.append(raw_name)

    return deleted, failed


def delete_all_documents(cfg) -> int:
    """corpus 디렉터리를 통째로 비운다. corpus 완전삭제에서만 쓴다.

    재귀 삭제라 경로가 어긋나면 피해가 크다. INVENTIONS_DOCS_DIR 같은 override로
    corpus 문서 경로가 데이터 루트 밖(예: repo의 원본 docs/)을 가리킬 수 있으므로,
    지우기 전에 DOCS_ROOT 안에 있는지 반드시 확인한다.
    """
    import shutil

    docs_dir = cfg.docs_dir()
    if not docs_dir.exists():
        return 0

    resolved = docs_dir.resolve()
    root = Path(config.DOCS_ROOT).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise UploadError(
            f"'{cfg.id}'의 문서 폴더가 데이터 루트 밖에 있어 삭제할 수 없습니다 "
            f"({resolved}). 원본을 직접 가리키고 있을 수 있으니 수동으로 확인하세요."
        )

    n = len(list_documents(cfg))
    shutil.rmtree(resolved, ignore_errors=True)
    return n
