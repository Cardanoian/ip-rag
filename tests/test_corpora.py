"""corpus 레지스트리와 kind 테스트."""
from __future__ import annotations

import pytest

import config
import corpora
from corpora.kinds import InventionKind, PlainKind, get_kind
from corpora.models import CorpusValidationError, rebuild_required_changes


# ---------------------------------------------------------------------------
# 시드 부트스트랩
# ---------------------------------------------------------------------------

def test_seed_is_created_on_empty_db():
    """빈 DB로 처음 뜨면 기존 발명 corpus 한 건이 생긴다."""
    corpora.ensure_seed()

    all_corpora = corpora.list_all()
    assert len(all_corpora) == 1

    seed = all_corpora[0]
    assert seed.id == corpora.SEED_CORPUS_ID
    assert seed.kind == "invention"
    assert seed.is_seed is True
    # 문서도 색인도 없는 상태이므로 초안으로 시작한다.
    assert seed.status == corpora.STATUS_DRAFT


def test_ensure_seed_is_idempotent():
    corpora.ensure_seed()
    corpora.ensure_seed()
    assert len(corpora.list_all()) == 1


def test_seed_carries_previous_config_values(seed_corpus):
    """멀티 corpus 이전의 상수가 그대로 옮겨져야 기존 색인과 호환된다."""
    assert seed_corpus.embed_dim == 1536
    assert seed_corpus.chunk_size == 1000
    assert seed_corpus.chunk_overlap == 150
    assert seed_corpus.single_chunk_char_hint == 5500
    assert "학생 발명품" in seed_corpus.doc_prefix
    assert seed_corpus.doc_prefix.endswith("\n")


def test_unknown_corpus_raises(seed_corpus):
    with pytest.raises(corpora.CorpusNotFound):
        corpora.get("없는corpus")


# ---------------------------------------------------------------------------
# 생성 검증
# ---------------------------------------------------------------------------

def _create(**overrides):
    kwargs = {
        "corpus_slug": "rules",
        "label": "학교 규정",
        "kind": "plain",
        "corpus_id": "school-rules",
        "doc_prefix": "[검색 대상 문서] 규정입니다.",
        "query_prefix": "[검색 질의] 규정을 찾습니다.",
        "embed_dim": 1536,
        "chunk_size": 1000,
        "chunk_overlap": 150,
        "single_chunk_char_hint": 5500,
        "created_by": "tester",
    }
    kwargs.update(overrides)
    return corpora.create(**kwargs)


def test_create_starts_as_draft(seed_corpus):
    cfg = _create()
    assert cfg.status == corpora.STATUS_DRAFT
    assert cfg.is_seed is False
    assert cfg.active_collection == "rules_v1"
    assert cfg.docs_dir().exists()


@pytest.mark.parametrize(
    "bad_slug",
    ["", "a", "has space", "../escape", "under_score", "-leading", "슬러그"],
)
def test_create_rejects_bad_slug(seed_corpus, bad_slug):
    """corpus id는 URL·디렉터리명으로 쓰이므로 엄격히 검증한다."""
    with pytest.raises(CorpusValidationError):
        _create(corpus_slug=bad_slug)


def test_create_normalizes_slug_case(seed_corpus):
    """대문자로 입력해도 거부하지 않고 소문자로 눕힌다."""
    cfg = _create(corpus_slug="School-Rules")
    assert cfg.id == "school-rules"


def test_create_rejects_reserved_seed_slug(seed_corpus):
    with pytest.raises(CorpusValidationError):
        _create(corpus_slug=corpora.SEED_CORPUS_ID)


def test_create_rejects_duplicate_slug(seed_corpus):
    _create()
    with pytest.raises(CorpusValidationError):
        _create()


def test_create_rejects_non_creatable_kind(seed_corpus):
    """발명 corpus는 기존 자료 전용이라 어드민에서 만들 수 없다."""
    with pytest.raises(CorpusValidationError):
        _create(kind="invention")


@pytest.mark.parametrize("bad_dim", [0, 64, 5000])
def test_create_rejects_out_of_range_dimension(seed_corpus, bad_dim):
    with pytest.raises(CorpusValidationError):
        _create(embed_dim=bad_dim)


def test_create_rejects_overlap_larger_than_chunk(seed_corpus):
    with pytest.raises(CorpusValidationError):
        _create(chunk_size=500, chunk_overlap=500)


def test_create_rejects_blank_prefix(seed_corpus):
    with pytest.raises(CorpusValidationError):
        _create(doc_prefix="   ")


def test_prefix_gets_trailing_newline(seed_corpus):
    """지시문은 본문 앞에 붙으므로 개행으로 경계를 만든다."""
    cfg = _create(doc_prefix="[문서] 규정", query_prefix="[질의] 규정")
    assert cfg.doc_prefix == "[문서] 규정\n"
    assert cfg.query_prefix == "[질의] 규정\n"


def test_corpus_id_defaults_to_slug(seed_corpus):
    cfg = _create(corpus_id="")
    assert cfg.corpus_id == "rules"


# ---------------------------------------------------------------------------
# plain kind 로더 — 텍스트 덩어리 처리
# ---------------------------------------------------------------------------

def test_plain_loader_uses_filename_as_title(plain_corpus, tmp_path):
    path = tmp_path / "학교폭력 예방 지침.md"
    path.write_text("지침 본문입니다.\n" * 10, encoding="utf-8")

    doc = PlainKind().load(path, plain_corpus.id)

    assert doc is not None
    assert doc["title"] == "학교폭력 예방 지침"
    assert doc["source_path"] == f"{plain_corpus.id}/학교폭력 예방 지침.md"


def test_plain_loader_accepts_txt(plain_corpus, tmp_path):
    path = tmp_path / "안내문.txt"
    path.write_text("안내 본문입니다.\n" * 10, encoding="utf-8")

    doc = PlainKind().load(path, plain_corpus.id)

    assert doc is not None
    assert doc["title"] == "안내문"


def test_plain_loader_never_fails_on_arbitrary_text(plain_corpus, tmp_path):
    """어떤 형식이든 색인된다 — 파싱 실패라는 개념이 없다."""
    path = tmp_path / "1979-이상한-형식-.md"
    path.write_text("아무 규칙 없는 텍스트 덩어리입니다. " * 10, encoding="utf-8")

    doc = PlainKind().load(path, plain_corpus.id)

    assert doc is not None
    assert doc["title"] == "1979-이상한-형식-"


def test_plain_loader_skips_too_short(plain_corpus, tmp_path):
    path = tmp_path / "짧은글.md"
    path.write_text("짧다", encoding="utf-8")
    assert PlainKind().load(path, plain_corpus.id) is None


def test_plain_embed_text_includes_title(plain_corpus):
    text = PlainKind().embed_text({"title": "제17조"}, "본문 내용")
    assert "제17조" in text
    assert "본문 내용" in text


def test_plain_kind_has_no_search_filter(plain_corpus):
    assert PlainKind().build_where(plain_corpus, {"include_advisor_docs": True}) is None


# ---------------------------------------------------------------------------
# public_fields 게이트
# ---------------------------------------------------------------------------

def test_invention_public_metadata_excludes_author():
    metadata = {
        "title": "발명품",
        "year": 2005,
        "category": "과학완구",
        "author": "학생 이름",
        "doc_type": "작품설명서",
        "source_path": "inventions/x.md",
        "content_hash": "deadbeef",
    }
    public = InventionKind().public_metadata(metadata)

    assert "author" not in public
    assert "source_path" not in public
    assert "content_hash" not in public
    assert public["title"] == "발명품"


def test_year_sentinel_maps_back_to_none():
    public = InventionKind().public_metadata({"title": "x", "year": -1})
    assert public["year"] is None


def test_plain_public_metadata_is_title_only():
    public = PlainKind().public_metadata({"title": "제17조", "source_path": "rules/x.md"})
    assert public == {"title": "제17조"}


# ---------------------------------------------------------------------------
# kind 레지스트리
# ---------------------------------------------------------------------------

def test_get_unknown_kind_raises():
    with pytest.raises(KeyError):
        get_kind("nonexistent")


def test_only_plain_is_creatable():
    names = {kind.name for kind in corpora.creatable_kinds()}
    assert names == {"plain"}


# ---------------------------------------------------------------------------
# 재색인 필요 필드 판정
# ---------------------------------------------------------------------------

def test_prefix_and_chunking_changes_require_rebuild(seed_corpus):
    changed = seed_corpus.with_updates(doc_prefix="새 지시문\n", chunk_size=500)
    assert rebuild_required_changes(seed_corpus, changed) == {
        "doc_prefix",
        "chunk_size",
    }


def test_label_and_query_prefix_changes_do_not_require_rebuild(seed_corpus):
    """질의 지시문은 검색 시점에만 쓰이므로 기존 벡터가 그대로 유효하다."""
    changed = seed_corpus.with_updates(
        label="새 이름",
        query_prefix="새 질의 지시문\n",
        corpus_id="new-id",
    )
    assert rebuild_required_changes(seed_corpus, changed) == set()


def test_embed_dim_change_requires_rebuild(seed_corpus):
    changed = seed_corpus.with_updates(embed_dim=768)
    assert "embed_dim" in rebuild_required_changes(seed_corpus, changed)


# ---------------------------------------------------------------------------
# 컬렉션 버전
# ---------------------------------------------------------------------------

def test_next_collection_name_increments_version(seed_corpus):
    assert seed_corpus.active_collection == "inventions_v1"
    assert seed_corpus.next_collection_name() == "inventions_v2"

    v7 = seed_corpus.with_updates(active_collection="inventions_v7")
    assert v7.collection_version == 7
    assert v7.next_collection_name() == "inventions_v8"


def test_docs_dir_is_per_corpus(seed_corpus, plain_corpus):
    assert seed_corpus.docs_dir() != plain_corpus.docs_dir()
    assert seed_corpus.docs_dir() == config.DOCS_ROOT / "inventions"


def test_docs_dir_override_wins(monkeypatch, seed_corpus, tmp_path):
    """기존 배포가 옛 문서 경로를 그대로 쓸 수 있어야 한다."""
    legacy = tmp_path / "legacy-docs"
    updated = corpora.update(seed_corpus, docs_dir_override=str(legacy))
    assert updated.docs_dir() == legacy


# ---------------------------------------------------------------------------
# 캐시 일관성
# ---------------------------------------------------------------------------

def test_update_is_visible_immediately(seed_corpus):
    corpora.update(seed_corpus, label="바뀐 이름")
    assert corpora.get(seed_corpus.id).label == "바뀐 이름"
