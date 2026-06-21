"""파일명·본문 파싱 및 메타데이터 추출.

공개 API:
  normalize_nfc(text)   -- 유니코드 NFC 정규화
  parse_filename(filename) -- 파일명에서 메타데이터 dict 추출
  load_document(path)   -- 파일을 읽어 메타데이터+본문 dict 반환 (단문/빈 파일은 None)
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from pathlib import Path

from config import ADVISOR_DOC_TYPE, DOCS_DIR, MAIN_DOC_TYPE, MIN_DOC_CHARS, PROJECT_ROOT

logger = logging.getLogger(__name__)

# 파일명 패턴: {4자리연도}-{분야}-{저자}-{제목}-.md
# 연도·분야·저자에는 하이픈 없음 → [^-]+ 로 고정, 제목만 그리디 캡처
_FILENAME_RE = re.compile(r"^(\d{4})-([^-]+)-([^-]+)-(.+)-\.md$")

# 지도논문 접미사 (앞뒤 공백 포함 제거)
_ADVISOR_SUFFIX_RE = re.compile(r"\s*\(지도논문\)\s*$")


def normalize_nfc(text: str) -> str:
    """유니코드 NFC 정규화."""
    return unicodedata.normalize("NFC", text)


def parse_filename(filename: str) -> dict:
    """파일명 basename에서 메타데이터를 추출한다.

    Returns:
        dict with keys: year (int|None), category (str), author (str),
                        title (str), doc_type (str)
    """
    basename = Path(filename).name
    m = _FILENAME_RE.match(basename)

    if m:
        year = int(m.group(1))
        category = normalize_nfc(m.group(2))
        author = normalize_nfc(m.group(3))
        raw_title = normalize_nfc(m.group(4))

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
    else:
        # fallback: 파싱 실패
        stem = normalize_nfc(Path(basename).stem)
        # 접미사로 doc_type 판정
        if _ADVISOR_SUFFIX_RE.search(stem):
            doc_type = ADVISOR_DOC_TYPE
        else:
            doc_type = MAIN_DOC_TYPE
        logger.warning("parse_filename: regex miss for %r, using fallback", basename)
        return {
            "year": None,
            "category": "",
            "author": "",
            "title": stem,
            "doc_type": doc_type,
        }


def load_document(path: str | Path) -> dict | None:
    """파일을 읽어 메타데이터와 본문을 포함한 dict를 반환한다.

    Args:
        path: 파일 경로 (str 또는 pathlib.Path)

    Returns:
        메타데이터+본문 dict, 또는 본문이 너무 짧으면 None.
    """
    path = Path(path)

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("load_document: cannot read %s: %s", path, exc)
        return None

    body = normalize_nfc(raw.strip())

    if len(body) < MIN_DOC_CHARS:
        logger.info("load_document: skip %s (body %d chars < %d)", path.name, len(body), MIN_DOC_CHARS)
        return None

    # source_path: POSIX 상대 경로 (프로젝트 루트 기준)
    try:
        rel = path.resolve().relative_to(PROJECT_ROOT)
        source_path = rel.as_posix()
    except ValueError:
        source_path = path.as_posix()

    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    meta = parse_filename(path.name)
    meta.update(
        {
            "text": body,
            "source_path": source_path,
            "content_hash": content_hash,
        }
    )
    return meta
