"""업로드 방어 — path traversal, zip slip, 확장자·크기 제한.

업로드는 외부 입력이 파일시스템에 직접 닿는 지점이라 별도로 검증한다.
"""
from __future__ import annotations

import io
import zipfile

import pytest

import config
from admin.documents import (
    UploadError,
    count_repairable_filenames,
    delete_documents,
    detect_zip_filename,
    list_documents,
    purge_documents,
    repair_legacy_zip_filenames,
    sanitize_filename,
    save_upload,
    save_uploads,
)

BODY = "규정 본문입니다. 충분히 길게 작성합니다.\n" * 5
MD_TXT = (".md", ".txt")


# ---------------------------------------------------------------------------
# 파일명 sanitize
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "attack",
    [
        "../../etc/passwd.md",
        "../../../root/.ssh/authorized_keys.md",
        "/etc/shadow.md",
        "..\\..\\windows\\system32\\evil.md",
        "subdir/nested.md",
    ],
)
def test_path_traversal_is_stripped_to_basename(attack):
    """경로 구성요소는 전부 버리고 파일명만 남긴다."""
    safe = sanitize_filename(attack, MD_TXT)
    assert "/" not in safe
    assert "\\" not in safe
    assert ".." not in safe


@pytest.mark.parametrize("bad", ["", "   ", ".", "..", ".hidden.md", "no-extension"])
def test_dangerous_names_are_rejected(bad):
    with pytest.raises(UploadError):
        sanitize_filename(bad, MD_TXT)


def test_null_byte_is_rejected():
    with pytest.raises(UploadError):
        sanitize_filename("evil\x00.md", MD_TXT)


def test_overlong_name_is_rejected():
    with pytest.raises(UploadError):
        sanitize_filename("가" * 200 + ".md", MD_TXT)


@pytest.mark.parametrize("bad_ext", ["exe", "sh", "py", "pdf", "hwp", "html"])
def test_extension_whitelist(bad_ext):
    with pytest.raises(UploadError):
        sanitize_filename(f"문서.{bad_ext}", MD_TXT)


def test_allowed_extensions_pass():
    assert sanitize_filename("규정.md", MD_TXT) == "규정.md"
    assert sanitize_filename("안내.txt", MD_TXT) == "안내.txt"
    assert sanitize_filename("대문자.MD", MD_TXT) == "대문자.MD"


def test_invention_corpus_rejects_txt(seed_corpus):
    """corpus 종류마다 허용 확장자가 다르다."""
    with pytest.raises(UploadError):
        save_upload(seed_corpus, "메모.txt", BODY.encode("utf-8"))


# ---------------------------------------------------------------------------
# 저장 위치
# ---------------------------------------------------------------------------

def test_traversal_upload_lands_inside_corpus_dir(plain_corpus, tmp_path):
    saved = save_upload(plain_corpus, "../../탈출시도.md", BODY.encode("utf-8"))

    target = plain_corpus.docs_dir() / saved
    assert target.exists()
    assert target.resolve().parent == plain_corpus.docs_dir().resolve()
    # 상위 디렉터리에 파일이 생기지 않았다.
    assert not (plain_corpus.docs_dir().parent / "탈출시도.md").exists()


def test_uploads_are_isolated_per_corpus(plain_corpus, seed_corpus):
    save_upload(plain_corpus, "규정.md", BODY.encode("utf-8"))

    assert len(list_documents(plain_corpus)) == 1
    assert len(list_documents(seed_corpus)) == 0


def test_non_utf8_is_rejected(plain_corpus):
    with pytest.raises(UploadError):
        save_upload(plain_corpus, "깨진파일.md", b"\xff\xfe\x00\x01invalid")


def test_empty_content_is_rejected(plain_corpus):
    with pytest.raises(UploadError):
        save_upload(plain_corpus, "빈파일.md", b"   \n  ")


# ---------------------------------------------------------------------------
# 다중 업로드
# ---------------------------------------------------------------------------

def test_bad_file_does_not_block_good_ones(plain_corpus):
    """수십 개를 올릴 때 하나 때문에 전부 되돌리면 원인을 찾기 어렵다."""
    result = save_uploads(plain_corpus, [
        ("좋은문서1.md", BODY.encode("utf-8")),
        ("나쁜문서.exe", BODY.encode("utf-8")),
        ("좋은문서2.md", BODY.encode("utf-8")),
    ])

    assert set(result.saved) == {"좋은문서1.md", "좋은문서2.md"}
    assert len(result.rejected) == 1
    assert result.rejected[0][0] == "나쁜문서.exe"


def test_total_size_limit(plain_corpus, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 100)

    with pytest.raises(UploadError, match="용량"):
        save_uploads(plain_corpus, [("큰파일.md", b"x" * 200)])


# ---------------------------------------------------------------------------
# zip
# ---------------------------------------------------------------------------

def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _unflagged_zip_bytes(filename: str, encoding: str) -> bytes:
    """ASCII 이름으로 만든 ZIP 헤더를 원하는 원시 파일명 바이트로 바꾼다."""
    raw_name = filename.encode(encoding)
    placeholder = b"x" * len(raw_name)
    payload = _zip_bytes({placeholder.decode("ascii"): BODY.encode("utf-8")})
    # 로컬 헤더와 중앙 디렉터리의 동일한 이름 두 곳이 바뀐다. 길이는 같아서
    # 다른 ZIP 필드를 다시 계산할 필요가 없다.
    assert payload.count(placeholder) == 2
    return payload.replace(placeholder, raw_name)


def test_zip_is_extracted(plain_corpus):
    payload = _zip_bytes({
        "규정1.md": BODY.encode("utf-8"),
        "규정2.md": BODY.encode("utf-8"),
    })

    result = save_uploads(plain_corpus, [("묶음.zip", payload)])

    assert set(result.saved) == {"규정1.md", "규정2.md"}
    assert len(list_documents(plain_corpus)) == 2


@pytest.mark.parametrize(
    ("filename", "encoding"),
    [
        ("학교 규정.md", "utf-8"),
        ("학교 규정.md", "cp949"),
        ("café.md", "cp437"),
    ],
)
def test_zip_detects_unflagged_filename_encoding(
    plain_corpus, filename, encoding
):
    payload = _unflagged_zip_bytes(filename, encoding)

    result = save_uploads(plain_corpus, [("묶음.zip", payload)])

    assert result.saved == [filename]
    assert (plain_corpus.docs_dir() / filename).exists()


def test_zip_detection_does_not_mistake_cp437_umlaut_for_cp949():
    decoded, encoding = detect_zip_filename("über.md".encode("cp437").decode("cp437"))

    assert decoded == "über.md"
    assert encoding == "cp437"


def test_zip_slip_is_blocked(plain_corpus):
    """zip 엔트리 이름으로도 디렉터리를 벗어날 수 없어야 한다."""
    payload = _zip_bytes({"../../../탈출.md": BODY.encode("utf-8")})

    result = save_uploads(plain_corpus, [("악성.zip", payload)])

    escaped = plain_corpus.docs_dir().parent.parent / "탈출.md"
    assert not escaped.exists()
    # basename만 남아 corpus 디렉터리 안에 저장된다.
    for name in result.saved:
        assert (plain_corpus.docs_dir() / name).resolve().parent == (
            plain_corpus.docs_dir().resolve()
        )


def test_zip_nested_paths_are_flattened(plain_corpus):
    payload = _zip_bytes({"폴더/하위/규정.md": BODY.encode("utf-8")})

    result = save_uploads(plain_corpus, [("묶음.zip", payload)])

    assert result.saved == ["규정.md"]


def test_zip_rejects_disallowed_entries(plain_corpus):
    payload = _zip_bytes({
        "정상.md": BODY.encode("utf-8"),
        "악성.exe": b"MZ binary",
    })

    result = save_uploads(plain_corpus, [("혼합.zip", payload)])

    assert result.saved == ["정상.md"]
    assert len(result.rejected) == 1


def test_zip_bomb_is_refused(plain_corpus, monkeypatch):
    monkeypatch.setattr(config, "MAX_UNZIPPED_BYTES", 1000)
    payload = _zip_bytes({"큰파일.md": b"x" * 50_000})

    result = save_uploads(plain_corpus, [("폭탄.zip", payload)])

    assert result.saved == []
    assert "용량이 너무 큽니다" in result.rejected[0][1]


def test_corrupt_zip_is_reported(plain_corpus):
    result = save_uploads(plain_corpus, [("깨진.zip", b"not a zip file at all")])

    assert result.saved == []
    assert "손상된" in result.rejected[0][1]


# ---------------------------------------------------------------------------
# 과거 ZIP 파일명 복구
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("encoding", ["utf-8", "cp949"])
def test_repairs_previously_misdecoded_zip_filename(plain_corpus, encoding):
    correct_name = "학교 규정.md"
    broken_name = correct_name.encode(encoding).decode("cp437")
    broken_path = plain_corpus.docs_dir() / broken_name
    broken_path.parent.mkdir(parents=True, exist_ok=True)
    broken_path.write_text(BODY, encoding="utf-8")

    assert count_repairable_filenames(plain_corpus) == 1

    result = repair_legacy_zip_filenames(plain_corpus)

    assert result.renamed == [(broken_name, correct_name)]
    assert result.skipped == []
    assert not broken_path.exists()
    assert (plain_corpus.docs_dir() / correct_name).exists()


def test_repair_leaves_real_cp437_filename_unchanged(plain_corpus):
    save_upload(plain_corpus, "café.md", BODY.encode("utf-8"))

    result = repair_legacy_zip_filenames(plain_corpus)

    assert result.renamed == []
    assert result.skipped == []
    assert (plain_corpus.docs_dir() / "café.md").exists()


def test_repair_does_not_overwrite_existing_filename(plain_corpus):
    correct_name = "학교 규정.md"
    broken_name = correct_name.encode("cp949").decode("cp437")
    save_upload(plain_corpus, correct_name, BODY.encode("utf-8"))
    broken_path = plain_corpus.docs_dir() / broken_name
    broken_path.write_text("다른 본문", encoding="utf-8")

    result = repair_legacy_zip_filenames(plain_corpus)

    assert result.renamed == []
    assert len(result.skipped) == 1
    assert broken_path.exists()
    assert (plain_corpus.docs_dir() / correct_name).read_text(encoding="utf-8") == BODY


# ---------------------------------------------------------------------------
# 삭제
# ---------------------------------------------------------------------------

def test_delete_removes_file_and_chunks(plain_corpus, patch_embed):
    from ingest.build_index import build_index
    from ingest.store import get_collection

    save_upload(plain_corpus, "규정.md", BODY.encode("utf-8"))
    build_index(plain_corpus)
    assert get_collection(plain_corpus.active_collection).count() > 0

    deleted, failed = delete_documents(plain_corpus, ["규정.md"])

    assert deleted == ["규정.md"]
    assert failed == []
    assert list_documents(plain_corpus) == []
    assert get_collection(plain_corpus.active_collection).count() == 0


def test_delete_rejects_traversal(plain_corpus, tmp_path):
    """삭제 경로로도 corpus 디렉터리를 벗어날 수 없어야 한다."""
    outsider = plain_corpus.docs_dir().parent / "건드리면안됨.md"
    outsider.parent.mkdir(parents=True, exist_ok=True)
    outsider.write_text("남의 파일", encoding="utf-8")

    delete_documents(plain_corpus, ["../건드리면안됨.md"])

    assert outsider.exists()


def test_delete_missing_file_still_clears_index(plain_corpus, patch_embed):
    """파일이 이미 없어도 색인에 남은 청크는 지워야 한다."""
    from ingest.build_index import build_index
    from ingest.store import get_collection

    save_upload(plain_corpus, "규정.md", BODY.encode("utf-8"))
    build_index(plain_corpus)
    (plain_corpus.docs_dir() / "규정.md").unlink()

    deleted, failed = delete_documents(plain_corpus, ["규정.md"])

    assert deleted == ["규정.md"]
    assert get_collection(plain_corpus.active_collection).count() == 0


# ---------------------------------------------------------------------------
# 목록
# ---------------------------------------------------------------------------

def test_list_filters_by_query(plain_corpus):
    save_upload(plain_corpus, "학교폭력 예방.md", BODY.encode("utf-8"))
    save_upload(plain_corpus, "복지 안내.md", BODY.encode("utf-8"))

    results = list_documents(plain_corpus, "학교폭력")

    assert [doc.filename for doc in results] == ["학교폭력 예방.md"]


def test_list_includes_both_extensions(plain_corpus):
    save_upload(plain_corpus, "규정.md", BODY.encode("utf-8"))
    save_upload(plain_corpus, "안내.txt", BODY.encode("utf-8"))

    assert len(list_documents(plain_corpus)) == 2


# ---------------------------------------------------------------------------
# 재귀 삭제 방어 — 원본 문서 폴더를 지우는 사고 방지
# ---------------------------------------------------------------------------

def test_delete_all_refuses_path_outside_data_root(plain_corpus, tmp_path):
    """override 로 corpus 가 repo 원본 docs/ 를 가리키면 rmtree 를 막아야 한다."""
    import corpora
    from admin.documents import delete_all_documents

    outside = tmp_path / "repo-원본-docs"
    outside.mkdir()
    (outside / "소중한문서.md").write_text(BODY, encoding="utf-8")

    cfg = corpora.update(plain_corpus, docs_dir_override=str(outside))

    with pytest.raises(UploadError, match="데이터 루트 밖"):
        delete_all_documents(cfg)

    assert (outside / "소중한문서.md").exists()


def test_delete_all_refuses_docs_root_itself(plain_corpus):
    """corpus 폴더가 아니라 루트 자체를 가리키면 전체 corpus가 날아간다."""
    import corpora
    from admin.documents import delete_all_documents

    cfg = corpora.update(plain_corpus, docs_dir_override=str(config.DOCS_ROOT))

    with pytest.raises(UploadError):
        delete_all_documents(cfg)


def test_delete_all_works_for_normal_corpus(plain_corpus):
    from admin.documents import delete_all_documents

    save_upload(plain_corpus, "규정.md", BODY.encode("utf-8"))
    docs_dir = plain_corpus.docs_dir()

    n = delete_all_documents(plain_corpus)

    assert n == 1
    assert not docs_dir.exists()


def test_purge_removes_unlimited_documents_and_search_index(
    plain_corpus, patch_embed
):
    from ingest.build_index import build_index
    from ingest.store import collection_exists, count_documents

    for number in range(25):
        save_upload(
            plain_corpus,
            f"규정-{number}.md",
            f"{number}\n{BODY}".encode("utf-8"),
        )
    build_index(plain_corpus)
    indexed_chunks = count_documents(plain_corpus.active_collection)
    assert indexed_chunks > 0

    result = purge_documents(plain_corpus)

    assert result.removed_documents == 25
    assert result.removed_chunks == indexed_chunks
    assert result.removed_collections == (plain_corpus.active_collection,)
    assert list_documents(plain_corpus) == []
    assert not collection_exists(plain_corpus.active_collection)
