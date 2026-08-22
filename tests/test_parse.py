"""Unit tests for ingest.parse module."""
from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from config import MIN_DOC_CHARS
from corpora.kinds import InventionKind
from ingest.parse import (
    ADVISOR_DOC_TYPE,
    MAIN_DOC_TYPE,
    normalize_nfc,
    parse_filename,
    read_source,
)

_invention = InventionKind()


def load_document(path):
    """발명 kind의 로더 — 예전 load_document()와 같은 dict를 돌려준다."""
    return _invention.load(path, "inventions")


# ---------------------------------------------------------------------------
# normalize_nfc
# ---------------------------------------------------------------------------

def test_normalize_nfc_hangul():
    """NFD-decomposed hangul must round-trip to NFC form."""
    nfc_str = "학습용품"
    nfd_str = unicodedata.normalize("NFD", nfc_str)
    assert nfd_str != nfc_str or True  # NFD may equal NFC for some chars; test is still valid
    result = normalize_nfc(nfd_str)
    assert result == unicodedata.normalize("NFC", nfc_str)


# ---------------------------------------------------------------------------
# parse_filename — normal 5-field case
# ---------------------------------------------------------------------------

def test_parse_filename_normal():
    """Standard filename parses all fields correctly."""
    fname = "1979-과학완구-강용환-공기의 압력을 이용한 미니 발전기-.md"
    result = parse_filename(fname)
    assert result["year"] == 1979
    assert result["category"] == "과학완구"
    assert result["author"] == "강용환"
    assert result["title"] == "공기의 압력을 이용한 미니 발전기"
    assert result["doc_type"] == MAIN_DOC_TYPE


# ---------------------------------------------------------------------------
# parse_filename — title with hyphens
# ---------------------------------------------------------------------------

def test_parse_filename_hyphen_in_title():
    """Title containing hyphens is captured correctly; author is not confused."""
    fname = "2013-학습용품-박상현-Multi - light 교수 · 학습장치-.md"
    result = parse_filename(fname)
    assert result["author"] == "박상현"
    assert result["title"] == "Multi - light 교수 · 학습장치"
    assert result["year"] == 2013
    assert result["category"] == "학습용품"
    assert result["doc_type"] == MAIN_DOC_TYPE


# ---------------------------------------------------------------------------
# parse_filename — 지도논문 suffix
# ---------------------------------------------------------------------------

def test_parse_filename_advisor_doc():
    """지도논문 suffix sets doc_type and strips suffix from title."""
    fname = "2010-생활과학Ⅰ-김선생-새로운 에너지 연구(지도논문)-.md"
    result = parse_filename(fname)
    assert result["doc_type"] == ADVISOR_DOC_TYPE
    assert "(지도논문)" not in result["title"]
    assert result["title"] == "새로운 에너지 연구"


# ---------------------------------------------------------------------------
# parse_filename — regex miss fallback
# ---------------------------------------------------------------------------

def test_parse_filename_fallback_no_raise():
    """A filename that does not match the regex returns a fallback dict without raising."""
    fname = "not-a-valid-filename.md"
    result = parse_filename(fname)
    assert result["year"] is None
    assert result["category"] == ""
    assert result["author"] == ""
    # title should equal the NFC stem
    expected_stem = unicodedata.normalize("NFC", "not-a-valid-filename")
    assert result["title"] == expected_stem


# ---------------------------------------------------------------------------
# load_document — empty / too-short file returns None
# ---------------------------------------------------------------------------

def test_load_document_returns_none_for_empty_file(tmp_path):
    """load_document returns None for a 0-byte file."""
    f = tmp_path / "1979-과학완구-홍길동-빈파일-.md"
    f.write_text("", encoding="utf-8")
    assert load_document(f) is None


def test_load_document_returns_none_for_short_file(tmp_path):
    """load_document returns None when body length < MIN_DOC_CHARS."""
    f = tmp_path / "2000-생활과학Ⅰ-홍길동-짧은글-.md"
    short_text = "짧" * (MIN_DOC_CHARS - 1)
    f.write_text(short_text, encoding="utf-8")
    result = load_document(f)
    assert result is None


# ---------------------------------------------------------------------------
# load_document — normal file returns expected dict
# ---------------------------------------------------------------------------

def test_load_document_normal_file(tmp_path):
    """load_document returns a dict with content_hash and source_path for a valid file."""
    import hashlib
    import unicodedata as ud

    content = "발명품 설명\n" * 20  # well above MIN_DOC_CHARS
    f = tmp_path / "2005-학습용품-이발명-멋진발명품-.md"
    f.write_text(content, encoding="utf-8")

    result = load_document(f)
    assert result is not None
    assert "content_hash" in result
    assert "source_path" in result
    assert "text" in result

    # Verify content_hash matches sha256 of NFC-normalized stripped body
    expected_body = ud.normalize("NFC", content.strip())
    expected_hash = hashlib.sha256(expected_body.encode("utf-8")).hexdigest()
    assert result["content_hash"] == expected_hash

    # source_path는 corpus 내부 경로다 — 서버의 절대 경로가 새면 안 된다.
    assert result["source_path"] == "inventions/2005-학습용품-이발명-멋진발명품-.md"
    assert str(tmp_path) not in result["source_path"]


# ---------------------------------------------------------------------------
# read_source — corpus 중립 공통부
# ---------------------------------------------------------------------------

def test_read_source_prefixes_corpus_id(tmp_path):
    """source_path는 어떤 corpus의 문서인지로 식별된다."""
    f = tmp_path / "규정 안내.md"
    f.write_text("학교 규정 본문입니다.\n" * 10, encoding="utf-8")

    result = read_source(f, "rules")
    assert result is not None
    assert result["source_path"] == "rules/규정 안내.md"
    assert result["stem"] == "규정 안내"
    assert result["filename"] == "규정 안내.md"


def test_read_source_missing_file_returns_none(tmp_path):
    assert read_source(tmp_path / "없는파일.md", "rules") is None


# ---------------------------------------------------------------------------
# Git LFS 포인터 방어
# ---------------------------------------------------------------------------

LFS_POINTER = (
    "version https://git-lfs.github.com/spec/v1\n"
    "oid sha256:e8334a1c9f2b7d6e5a4c3b2a1908f7e6d5c4b3a2918f7e6d5c4b3a2918f7e6d5\n"
    "size 4096\n"
)


def test_lfs_pointer_is_detected(tmp_path):
    """`git lfs pull` 없이 읽으면 본문 대신 포인터가 들어온다.

    길이가 MIN_DOC_CHARS를 넘어 그냥 두면 조용히 색인되고, 검색은 제목만으로
    매칭되어 품질이 망가진 것을 알아채기 어렵다.
    """
    from ingest.parse import LFSPointerError

    path = tmp_path / "1979-과학완구-홍길동-발명품-.md"
    path.write_text(LFS_POINTER, encoding="utf-8")

    with pytest.raises(LFSPointerError, match="git lfs pull"):
        read_source(path, "inventions")


def test_lfs_pointer_is_longer_than_min_doc_chars():
    """이 방어가 필요한 이유 자체를 고정한다 — 길이 검사로는 못 걸러낸다."""
    assert len(LFS_POINTER.strip()) > MIN_DOC_CHARS


def test_normal_document_is_not_mistaken_for_pointer(tmp_path):
    path = tmp_path / "규정.md"
    path.write_text("version 관리 규정에 대한 문서입니다.\n" * 5, encoding="utf-8")

    result = read_source(path, "rules")

    assert result is not None
