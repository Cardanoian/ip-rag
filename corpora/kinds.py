"""corpus 종류 — 코드가 소유하는 동작 플러그인.

파서·임베딩 입력 구성·검색 필터는 함수라서 DB에 담을 수 없다. 그래서 여기에 묶어두고
DB의 `corpora.kind` 컬럼이 이 중 하나를 가리킨다. 관리자는 kind를 고를 뿐 코드를 건드리지 않는다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ingest.parse import MAIN_DOC_TYPE, parse_filename, read_source


class CorpusKind:
    """kind 기본 구현. 하위 클래스는 load/embed_text/build_where만 다르게 한다."""

    name: str = ""
    label: str = ""
    description: str = ""
    file_extensions: tuple[str, ...] = (".md",)
    # 어드민 corpus 생성 폼에 노출할지. 기존 데이터 전용 kind는 False.
    creatable_by_admin: bool = False
    # 색인에 저장할 메타 키 (source_path/chunk_index/n_chunks/content_hash는 항상 저장된다)
    metadata_fields: tuple[str, ...] = ("title",)
    # 검색 응답에 노출할 메타 키. 여기 없는 값은 외부로 나가지 않는다.
    public_fields: tuple[str, ...] = ("title",)

    def load(self, path: Path, corpus_id: str) -> dict | None:
        raise NotImplementedError

    def embed_text(self, doc: dict, chunk: str) -> str:
        """임베딩 입력 텍스트. corpus 지시문 프리픽스는 embedder가 따로 붙인다."""
        raise NotImplementedError

    def build_where(self, cfg, options: dict[str, Any]) -> dict | None:
        """Chroma where 필터. 필터가 없으면 None."""
        return None

    def public_metadata(self, metadata: dict) -> dict:
        """검색 응답에 나갈 메타데이터만 남긴다 — 개인정보 게이트."""
        result: dict[str, Any] = {}
        for field in self.public_fields:
            if field not in metadata:
                continue
            value = metadata[field]
            # 색인은 None을 담을 수 없어 year를 -1로 저장한다. 응답에서는 null로 되돌린다.
            if field == "year" and value == -1:
                value = None
            result[field] = value
        return result


class InventionKind(CorpusKind):
    """발명대회 수상작 corpus 전용. 파일명이 곧 메타데이터인 기존 자료 구조."""

    name = "invention"
    label = "발명대회 수상작"
    description = (
        "`{연도}-{분야}-{저자}-{제목}-.md` 파일명 규칙에서 메타데이터를 추출합니다. "
        "기존 수상작 자료 전용이며 새로 만들 수 없습니다."
    )
    file_extensions = (".md",)
    creatable_by_admin = False
    metadata_fields = ("year", "category", "author", "title", "doc_type")
    # author는 의도적으로 제외한다 — 과거 참가 학생 이름이 외부로 나가면 안 된다.
    public_fields = ("title", "year", "category", "doc_type")

    def load(self, path: Path, corpus_id: str) -> dict | None:
        source = read_source(path, corpus_id)
        if source is None:
            return None
        source.update(parse_filename(path.name))
        return source

    def embed_text(self, doc: dict, chunk: str) -> str:
        title = doc.get("title") or ""
        category = doc.get("category") or ""
        return f"제목: {title} | 분야: {category}\n{chunk}"

    def build_where(self, cfg, options: dict[str, Any]) -> dict | None:
        """기본은 작품설명서만. 지도논문은 요청이 있을 때만 포함한다."""
        if options.get("include_advisor_docs"):
            return None
        return {"doc_type": MAIN_DOC_TYPE}


class PlainKind(CorpusKind):
    """일반 텍스트 corpus — 관리자가 만드는 모든 신규 corpus.

    파일명 규칙도 frontmatter도 요구하지 않는다. 제목은 파일명(확장자 제외),
    본문은 파일 내용 전체다. 어떤 텍스트를 올려도 파싱 실패가 없다.
    """

    name = "plain"
    label = "일반 텍스트"
    description = (
        "`.md` 또는 `.txt` 파일을 그대로 색인합니다. "
        "제목은 파일명이 되고 본문 전체가 검색 대상이 됩니다."
    )
    file_extensions = (".md", ".txt")
    creatable_by_admin = True
    metadata_fields = ("title",)
    public_fields = ("title",)

    def load(self, path: Path, corpus_id: str) -> dict | None:
        source = read_source(path, corpus_id)
        if source is None:
            return None
        source["title"] = source["stem"]
        return source

    def embed_text(self, doc: dict, chunk: str) -> str:
        title = doc.get("title") or ""
        return f"제목: {title}\n{chunk}"


_KINDS: dict[str, CorpusKind] = {
    InventionKind.name: InventionKind(),
    PlainKind.name: PlainKind(),
}

DEFAULT_KIND = PlainKind.name


def get_kind(name: str) -> CorpusKind:
    """kind 이름으로 구현을 찾는다. 미등록이면 KeyError."""
    try:
        return _KINDS[name]
    except KeyError:
        raise KeyError(f"알 수 없는 corpus 종류입니다: {name!r}") from None


def kind_of(cfg) -> CorpusKind:
    return get_kind(cfg.kind)


def creatable_kinds() -> list[CorpusKind]:
    """어드민 생성 폼에 노출할 kind 목록."""
    return [kind for kind in _KINDS.values() if kind.creatable_by_admin]
