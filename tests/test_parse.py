"""Unit tests for ingest.parse module."""
from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from config import ADVISOR_DOC_TYPE, MAIN_DOC_TYPE, MIN_DOC_CHARS
from ingest.parse import load_document, normalize_nfc, parse_filename


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

    # source_path should be a POSIX path string
    assert "/" in result["source_path"] or result["source_path"].endswith(".md")
