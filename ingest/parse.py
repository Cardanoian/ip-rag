"""파일명·본문 파싱 및 메타데이터 추출."""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from pathlib import Path

from config import ADVISOR_DOC_TYPE, MAIN_DOC_TYPE, MIN_DOC_CHARS, PROJECT_ROOT

logger = logging.getLogger(__name__)

_FILENAME_RE = re.compile(r"^(\d{4})-([^-]+)-([^-]+)-(.+)-\.md$")
_ADVISOR_SUFFIX_RE = re.compile(r"\s*\(지도논문\)\s*$")


def normalize_nfc(text: str) -> str:
    """유니코드 NFC 정규화."""
    return unicodedata.normalize("NFC", text)


def parse_filename(filename: str) -> dict:
    """파일명 basename에서 메타데이터를 추출한다."""
    basename = Path(filename).name
    match = _FILENAME_RE.match(basename)

    if match:
        year = int(match.group(1))
        category = normalize_nfc(match.group(2))
        author = normalize_nfc(match.group(3))
        raw_title = normalize_nfc(match.group(4))

        if _ADVISOR_SUFFIX_RE.search(raw_title):
            doc_type = ADVISOR_DOC_TYPE
            title = normalize_nfc(_ADVISOR_SUFFIX_RE.sub("", raw_title))
        else:
            doc_type = MAIN_DOC_TYPE
            title = raw_title

        return {
            "year": year,
            "category": category,
            "author": author,
            "title": title,
            "doc_type": doc_type,
        }

    stem = normalize_nfc(Path(basename).stem)
    doc_type = (
        ADVISOR_DOC_TYPE
        if _ADVISOR_SUFFIX_RE.search(stem)
        else MAIN_DOC_TYPE
    )
    logger.warning("parse_filename: regex miss for %r, using fallback", basename)
    return {
        "year": None,
        "category": "",
        "author": "",
        "title": stem,
        "doc_type": doc_type,
    }


def load_document(path: str | Path) -> dict | None:
    """파일을 읽어 메타데이터와 본문을 포함한 dict를 반환한다."""
    path = Path(path)

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("load_document: cannot read %s: %s", path, exc)
        return None

    body = normalize_nfc(raw.strip())
    if len(body) < MIN_DOC_CHARS:
        logger.info(
            "load_document: skip %s (body %d chars < %d)",
            path.name,
            len(body),
            MIN_DOC_CHARS,
        )
        return None

    try:
        relative_path = path.resolve().relative_to(PROJECT_ROOT)
        source_path = relative_path.as_posix()
    except ValueError:
        # build_index는 docs_dir 바로 아래 *.md만 스캔한다. 배포 서버의 절대
        # 경로가 ID나 응답에 섞이지 않도록 corpus 내부 경로로 평탄화한다.
        source_path = f"docs/{normalize_nfc(path.name)}"

    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    metadata = parse_filename(path.name)
    metadata.update(
        {
            "text": body,
            "source_path": source_path,
            "content_hash": content_hash,
        }
    )
    return metadata
