"""파일 읽기 공통부 + 발명 corpus 전용 파일명 파서.

corpus kind가 이 모듈의 함수를 조합해 문서를 로드한다. kind별 분기는 여기 두지 않는다.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from pathlib import Path

from config import MIN_DOC_CHARS

logger = logging.getLogger(__name__)

ADVISOR_DOC_TYPE = "지도논문"
MAIN_DOC_TYPE = "작품설명서"

_FILENAME_RE = re.compile(r"^(\d{4})-([^-]+)-([^-]+)-(.+)-\.md$")
_ADVISOR_SUFFIX_RE = re.compile(r"\s*\(지도논문\)\s*$")

# Git LFS로 관리되는 파일을 `git lfs pull` 없이 읽으면 본문 대신 이런 포인터가 나온다.
# 길이가 MIN_DOC_CHARS를 넘어서 그냥 두면 조용히 색인되고, 검색은 제목만으로
# 매칭되어 품질이 망가진 걸 알아채기 어렵다.
_LFS_POINTER_PREFIX = "version https://git-lfs.github.com/spec/v1"


class LFSPointerError(ValueError):
    """본문이 아니라 Git LFS 포인터다. `git lfs pull`이 필요하다."""


def normalize_nfc(text: str) -> str:
    """유니코드 NFC 정규화."""
    return unicodedata.normalize("NFC", text)


def read_source(path: str | Path, corpus_id: str) -> dict | None:
    """파일을 읽어 본문·식별자·해시를 돌려준다. 너무 짧거나 못 읽으면 None.

    source_path는 corpus 내부에서만 의미를 갖는 `{corpus_id}/{파일명}` 형태다.
    서버의 실제 절대 경로가 색인이나 API 응답에 새어나가지 않는다.
    """
    path = Path(path)

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("read_source: cannot read %s: %s", path, exc)
        return None

    body = normalize_nfc(raw.strip())
    if body.startswith(_LFS_POINTER_PREFIX):
        raise LFSPointerError(
            f"{path.name}: 본문 대신 Git LFS 포인터가 들어 있습니다. "
            "`git lfs pull`로 실제 내용을 받은 뒤 다시 색인하세요."
        )

    if len(body) < MIN_DOC_CHARS:
        logger.info(
            "read_source: skip %s (body %d chars < %d)",
            path.name,
            len(body),
            MIN_DOC_CHARS,
        )
        return None

    filename = normalize_nfc(path.name)
    return {
        "text": body,
        "filename": filename,
        "stem": normalize_nfc(path.stem),
        "source_path": f"{corpus_id}/{filename}",
        "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def parse_filename(filename: str) -> dict:
    """발명 corpus 파일명에서 메타데이터를 추출한다.

    형식: `{연도}-{분야}-{저자}-{제목}-.md`
    규칙에서 벗어나면 stem을 제목으로 쓰는 fallback을 적용한다.
    """
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
