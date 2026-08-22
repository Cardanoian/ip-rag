"""Chroma 색인 빌더 — corpus 문서 디렉터리를 읽어 청크 임베딩 후 ChromaDB에 저장한다.

공개 인터페이스:
  build_index(cfg, target_collection=None, docs_dir=None, limit=None,
              progress=None, reset=False) -> dict
    - cfg              : 대상 corpus 설정 (CorpusConfig)
    - target_collection: 색인할 컬렉션명 (기본: cfg.active_collection).
                         alias 전환 재색인은 여기에 새 버전 이름을 넘긴다.
    - docs_dir         : 스캔할 디렉터리 (기본: cfg.docs_dir())
    - limit            : None이면 전체, 정수면 해당 수만큼만 처리 (테스트용)
    - progress         : progress(current, total) 콜백 (어드민 진행률 표시용)
    - reset            : True면 컬렉션 삭제 후 재생성
    반환 dict 키:
      indexed_docs, skipped_docs, failed_docs, total_chunks, embedded_chunks,
      reused_docs, lfs_pointer_docs

CLI:
  python -m ingest.build_index [--corpus ID] [--reset] [--limit N] [--docs-dir PATH]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Callable, Optional

import config
from corpora.kinds import kind_of
from ingest.chunker import chunk_document, chunk_text
from ingest.embedder import embed_documents
from ingest.parse import LFSPointerError
from ingest.store import chunk_id, get_collection

logger = logging.getLogger(__name__)

# 배치 플러시 크기 (청크 단위)
_FLUSH_BATCH = 100
# 진행 로그 간격 (문서 단위)
_LOG_INTERVAL = 200


def iter_documents(cfg, docs_dir: Optional[Path] = None) -> list[Path]:
    """corpus 문서 디렉터리에서 색인 대상 파일을 정렬해 돌려준다."""
    target_dir = Path(docs_dir) if docs_dir else cfg.docs_dir()
    if not target_dir.exists():
        return []
    kind = kind_of(cfg)
    files: list[Path] = []
    for extension in kind.file_extensions:
        files.extend(target_dir.glob(f"*{extension}"))
    return sorted(files)


def build_index(
    cfg,
    target_collection: Optional[str] = None,
    docs_dir: Optional[Path | str] = None,
    limit: Optional[int] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    reset: bool = False,
) -> dict:
    """corpus 문서를 읽어 Chroma 컬렉션에 색인한다.

    Returns:
        dict with keys:
          indexed_docs   - 새로 임베딩·저장한 문서 수
          skipped_docs   - 0바이트/너무 짧아 건너뛴 문서 수
          failed_docs    - 예외로 실패한 문서 수
          total_chunks   - 색인된 청크 합계 (재사용 포함)
          embedded_chunks- 실제로 embed_documents()를 호출한 청크 수
          reused_docs    - content_hash 동일 → 재사용(재임베딩 생략) 문서 수
          lfs_pointer_docs - `git lfs pull` 이 안 돼 본문 대신 포인터가 든 문서 수
    """
    kind = kind_of(cfg)
    collection_name = target_collection or cfg.active_collection
    collection = get_collection(collection_name, reset=reset)

    stats = {
        "indexed_docs": 0,
        "skipped_docs": 0,
        "failed_docs": 0,
        "total_chunks": 0,
        "embedded_chunks": 0,
        "reused_docs": 0,
        "lfs_pointer_docs": 0,
    }

    doc_files = iter_documents(cfg, docs_dir)
    if limit is not None:
        doc_files = doc_files[:limit]
    total = len(doc_files)

    logger.info(
        "build_index[%s]: %d 파일 스캔 시작 (collection=%s, reset=%s)",
        cfg.id, total, collection_name, reset,
    )

    # 배치 버퍼
    buf_ids: list[str] = []
    buf_texts: list[str]  = []       # 임베딩 입력 텍스트 (kind가 구성)
    buf_docs: list[str]   = []       # 저장 원문 (청크 원문)
    buf_metas: list[dict] = []

    def _flush() -> None:
        """버퍼에 쌓인 청크를 embed → collection.add 한 뒤 버퍼를 비운다."""
        if not buf_ids:
            return
        embeddings = embed_documents(buf_texts, cfg)
        collection.add(
            ids=buf_ids,
            embeddings=embeddings,
            documents=buf_docs,
            metadatas=buf_metas,
        )
        stats["embedded_chunks"] += len(buf_ids)
        buf_ids.clear()
        buf_texts.clear()
        buf_docs.clear()
        buf_metas.clear()

    for doc_num, path in enumerate(doc_files, start=1):
        if progress is not None:
            progress(doc_num - 1, total)

        if doc_num % _LOG_INTERVAL == 0:
            logger.info(
                "build_index[%s] 진행: %d/%d | indexed=%d reused=%d skipped=%d failed=%d chunks=%d",
                cfg.id, doc_num, total,
                stats["indexed_docs"], stats["reused_docs"],
                stats["skipped_docs"], stats["failed_docs"],
                stats["total_chunks"],
            )

        try:
            # 1) 파싱 — kind가 corpus 종류에 맞게 로드한다
            try:
                doc = kind.load(path, cfg.id)
            except LFSPointerError as exc:
                # 포인터를 색인하면 본문 없이 제목만으로 검색되어 품질이 망가진다.
                # 조용히 넘기지 않고 따로 센 뒤 마지막에 크게 경고한다.
                stats["lfs_pointer_docs"] += 1
                stats["skipped_docs"] += 1
                if stats["lfs_pointer_docs"] == 1:
                    logger.error("build_index: %s", exc)
                continue
            if doc is None:
                logger.debug("build_index: skip %s (loader가 None 반환)", path.name)
                stats["skipped_docs"] += 1
                continue

            source_path: str = doc["source_path"]
            content_hash: str = doc["content_hash"]

            # 2) 아이덴포턴시 체크: 기존 레코드의 content_hash 비교
            existing = collection.get(
                where={"source_path": source_path},
                include=["metadatas"],
            )
            if existing["ids"]:
                existing_hash = existing["metadatas"][0].get("content_hash", "")
                if existing_hash == content_hash:
                    # 변경 없음 → 재사용
                    n_reused_chunks = len(existing["ids"])
                    stats["reused_docs"] += 1
                    stats["total_chunks"] += n_reused_chunks
                    logger.debug(
                        "build_index: reuse %s (%d chunks)", path.name, n_reused_chunks
                    )
                    continue
                # 내용이 바뀌었거나 hash 불일치 → 기존 청크 삭제 (고아 방지)
                collection.delete(where={"source_path": source_path})
                logger.debug(
                    "build_index: orphan delete %s (%d old chunks)",
                    path.name, len(existing["ids"]),
                )

            # 3) 청킹 (char-heuristic branch, count_tokens=None 의도적)
            chunks = chunk_document(doc["text"], cfg, count_tokens=None)
            if not chunks:
                logger.warning("build_index: %s → 청크 없음, 건너뜀", path.name)
                stats["skipped_docs"] += 1
                continue

            # 3b) 트렁케이션 가드: 어떤 청크도 안전 문자 상한(=단일청크 한도)을 넘지
            #     않도록 보장한다. 현재 청킹 로직상 사실상 발생하지 않지만,
            #     '무음 잘림 방지'를 코드로 강제한다.
            if any(len(c) > cfg.single_chunk_char_hint for c in chunks):
                logger.warning(
                    "build_index: %s → 청크가 안전 상한 초과, 재분할(무음 잘림 방지)",
                    path.name,
                )
                guarded: list[str] = []
                for c in chunks:
                    guarded.extend(
                        chunk_text(c, cfg)
                        if len(c) > cfg.single_chunk_char_hint
                        else [c]
                    )
                chunks = guarded

            # 4) 메타데이터 — kind가 선언한 필드만 저장한다
            base_meta: dict = {}
            for field in kind.metadata_fields:
                value = doc.get(field)
                if value is None:
                    # Chroma 메타데이터는 None을 담지 못한다. 연도는 -1 센티넬,
                    # 나머지는 빈 문자열로 눕힌 뒤 응답 단계에서 되돌린다.
                    value = -1 if field == "year" else ""
                base_meta[field] = value

            n_chunks = len(chunks)

            # 5) 버퍼 축적
            for i, chunk in enumerate(chunks):
                meta = dict(base_meta)
                meta.update(
                    {
                        "source_path": source_path,
                        "chunk_index": i,
                        "n_chunks": n_chunks,
                        "content_hash": content_hash,
                    }
                )

                buf_ids.append(chunk_id(source_path, i))
                buf_texts.append(kind.embed_text(doc, chunk))
                buf_docs.append(chunk)          # 스니펫용 원문
                buf_metas.append(meta)

                if len(buf_ids) >= _FLUSH_BATCH:
                    _flush()

            stats["indexed_docs"] += 1
            stats["total_chunks"] += n_chunks

        except Exception as exc:
            logger.error(
                "build_index: 처리 실패 %s: %s", path.name, exc, exc_info=True
            )
            stats["failed_docs"] += 1

    # 나머지 버퍼 플러시
    _flush()

    if progress is not None:
        progress(total, total)

    logger.info(
        "build_index[%s] 완료: indexed=%d reused=%d skipped=%d failed=%d "
        "total_chunks=%d embedded_chunks=%d",
        cfg.id,
        stats["indexed_docs"], stats["reused_docs"],
        stats["skipped_docs"], stats["failed_docs"],
        stats["total_chunks"], stats["embedded_chunks"],
    )
    if stats["lfs_pointer_docs"]:
        logger.error(
            "build_index[%s]: %d개 문서가 Git LFS 포인터라 색인에서 제외되었습니다. "
            "`git lfs pull`로 실제 본문을 받은 뒤 다시 색인하세요.",
            cfg.id, stats["lfs_pointer_docs"],
        )
    return stats


def remove_document(cfg, source_path: str, collection_name: str | None = None) -> int:
    """문서 하나의 모든 청크를 색인에서 제거한다. 삭제된 청크 수를 반환."""
    collection = get_collection(collection_name or cfg.active_collection)
    existing = collection.get(where={"source_path": source_path}, include=[])
    n_removed = len(existing["ids"])
    if n_removed:
        collection.delete(where={"source_path": source_path})
    return n_removed


def main() -> None:
    """CLI 진입점: python -m ingest.build_index"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    import corpora

    parser = argparse.ArgumentParser(
        description="corpus 문서를 읽어 Chroma DB에 색인한다."
    )
    parser.add_argument(
        "--corpus",
        default=corpora.SEED_CORPUS_ID,
        metavar="ID",
        help=f"대상 corpus id (기본: {corpora.SEED_CORPUS_ID}).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="기존 컬렉션을 삭제하고 처음부터 재색인한다.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="처리할 최대 문서 수 (기본: 전체). 테스트·비용 절감용.",
    )
    parser.add_argument(
        "--docs-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="스캔할 문서 디렉터리 (기본: corpus의 문서 디렉터리).",
    )
    args = parser.parse_args()

    try:
        cfg = corpora.get(args.corpus)
    except corpora.CorpusNotFound:
        available = ", ".join(c.id for c in corpora.list_all())
        parser.error(f"등록되지 않은 corpus: {args.corpus} (등록됨: {available})")
        return

    stats = build_index(
        cfg,
        docs_dir=args.docs_dir,
        reset=args.reset,
        limit=args.limit,
    )
    print(stats)


if __name__ == "__main__":
    main()
