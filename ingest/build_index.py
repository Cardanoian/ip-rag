"""Chroma 색인 빌더 — docs/ 아래 *.md 파일을 읽어 청크 임베딩 후 ChromaDB에 저장한다.

공개 인터페이스:
  build_index(docs_dir=None, reset=False, limit=None) -> dict
    - docs_dir : 스캔할 디렉터리 (기본: config.DOCS_DIR)
    - reset    : True면 컬렉션 삭제 후 재생성
    - limit    : None이면 전체, 정수면 해당 수만큼만 처리 (테스트용)
    반환 dict 키:
      indexed_docs, skipped_docs, failed_docs, total_chunks, embedded_chunks, reused_docs

CLI:
  python -m ingest.build_index [--reset] [--limit N] [--docs-dir PATH]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

import config
from ingest.chunker import chunk_document, chunk_text
from ingest.embedder import embed_documents
from ingest.parse import load_document
from ingest.store import chunk_id, get_collection

logger = logging.getLogger(__name__)

# 배치 플러시 크기 (청크 단위)
_FLUSH_BATCH = 100
# 진행 로그 간격 (문서 단위)
_LOG_INTERVAL = 200


def build_index(
    docs_dir: Optional[Path | str] = None,
    reset: bool = False,
    limit: Optional[int] = None,
) -> dict:
    """docs_dir 아래 *.md 를 읽어 Chroma 컬렉션에 색인한다.

    Returns:
        dict with keys:
          indexed_docs   - 새로 임베딩·저장한 문서 수
          skipped_docs   - 0바이트/너무 짧아 건너뛴 문서 수
          failed_docs    - 예외로 실패한 문서 수
          total_chunks   - 색인된 청크 합계 (재사용 포함)
          embedded_chunks- 실제로 embed_documents()를 호출한 청크 수
          reused_docs    - content_hash 동일 → 재사용(재임베딩 생략) 문서 수
    """
    target_dir = Path(docs_dir) if docs_dir else config.DOCS_DIR
    collection = get_collection(reset=reset)

    stats = {
        "indexed_docs": 0,
        "skipped_docs": 0,
        "failed_docs": 0,
        "total_chunks": 0,
        "embedded_chunks": 0,
        "reused_docs": 0,
    }

    md_files = sorted(target_dir.glob("*.md"))
    if limit is not None:
        md_files = md_files[:limit]

    logger.info("build_index: %d 파일 스캔 시작 (reset=%s)", len(md_files), reset)

    # 배치 버퍼
    buf_ids: list[str] = []
    buf_texts: list[str]  = []       # 임베딩 입력 텍스트 (제목+분야 프리픽스 포함)
    buf_docs: list[str]   = []       # 저장 원문 (청크 원문)
    buf_metas: list[dict] = []

    def _flush() -> None:
        """버퍼에 쌓인 청크를 embed → collection.add 한 뒤 버퍼를 비운다."""
        if not buf_ids:
            return
        embeddings = embed_documents(buf_texts)
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

    for doc_num, path in enumerate(md_files, start=1):
        # 진행 로그
        if doc_num % _LOG_INTERVAL == 0:
            logger.info(
                "build_index 진행: %d/%d 처리중 | indexed=%d reused=%d skipped=%d failed=%d chunks=%d",
                doc_num, len(md_files),
                stats["indexed_docs"], stats["reused_docs"],
                stats["skipped_docs"], stats["failed_docs"],
                stats["total_chunks"],
            )

        try:
            # 1) 파싱
            doc = load_document(path)
            if doc is None:
                logger.debug("build_index: skip %s (None from load_document)", path.name)
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
                    logger.debug("build_index: reuse %s (%d chunks)", path.name, n_reused_chunks)
                    continue
                # 내용이 바뀌었거나 hash 불일치 → 기존 청크 삭제 (고아 방지)
                collection.delete(where={"source_path": source_path})
                logger.debug("build_index: orphan delete %s (%d old chunks)", path.name, len(existing["ids"]))

            # 3) 청킹 (char-heuristic branch, count_tokens=None 의도적)
            chunks = chunk_document(doc["text"], count_tokens=None)
            if not chunks:
                logger.warning("build_index: %s → 청크 없음, 건너뜀", path.name)
                stats["skipped_docs"] += 1
                continue

            # 3b) 트렁케이션 가드(§3.3): 어떤 청크도 안전 문자 상한(=단일청크 한도,
            #     SINGLE_CHUNK_CHAR_HINT ≈ 8192토큰 대비 충분한 마진)을 넘지 않도록 보장.
            #     현재 청킹 로직상 사실상 발생하지 않지만, '무음 잘림 방지'를 코드로 강제한다.
            if any(len(c) > config.SINGLE_CHUNK_CHAR_HINT for c in chunks):
                logger.warning(
                    "build_index: %s → 청크가 안전 상한 초과, 재분할(무음 잘림 방지)", path.name
                )
                guarded: list[str] = []
                for c in chunks:
                    guarded.extend(chunk_text(c) if len(c) > config.SINGLE_CHUNK_CHAR_HINT else [c])
                chunks = guarded

            title: str    = str(doc.get("title") or "")
            category: str = str(doc.get("category") or "")
            author: str   = str(doc.get("author") or "")
            doc_type: str = str(doc.get("doc_type") or "")
            year_raw      = doc.get("year")
            year: int     = int(year_raw) if year_raw is not None else -1

            n_chunks = len(chunks)

            # 4) 버퍼 축적
            for i, chunk in enumerate(chunks):
                # 임베딩 입력: 제목+분야 프리픽스 (embedder가 DOC_TASK_PREFIX를 추가함)
                embed_text = f"제목: {title} | 분야: {category}\n{chunk}"

                meta: dict = {
                    "year": year,
                    "category": category,
                    "author": author,
                    "title": title,
                    "doc_type": doc_type,
                    "source_path": source_path,
                    "chunk_index": i,
                    "n_chunks": n_chunks,
                    "content_hash": content_hash,
                }

                buf_ids.append(chunk_id(source_path, i))
                buf_texts.append(embed_text)
                buf_docs.append(chunk)          # 스니펫용 원문
                buf_metas.append(meta)

                if len(buf_ids) >= _FLUSH_BATCH:
                    _flush()

            stats["indexed_docs"] += 1
            stats["total_chunks"] += n_chunks

        except Exception as exc:
            logger.error("build_index: 처리 실패 %s: %s", path.name, exc, exc_info=True)
            stats["failed_docs"] += 1

    # 나머지 버퍼 플러시
    _flush()

    logger.info(
        "build_index 완료: indexed=%d reused=%d skipped=%d failed=%d "
        "total_chunks=%d embedded_chunks=%d",
        stats["indexed_docs"], stats["reused_docs"],
        stats["skipped_docs"], stats["failed_docs"],
        stats["total_chunks"], stats["embedded_chunks"],
    )
    return stats


def main() -> None:
    """CLI 진입점: python -m ingest.build_index"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    parser = argparse.ArgumentParser(
        description="docs/ 아래 *.md를 읽어 Chroma DB에 색인한다."
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
        help="스캔할 문서 디렉터리 (기본: config.DOCS_DIR).",
    )
    args = parser.parse_args()

    stats = build_index(
        docs_dir=args.docs_dir,
        reset=args.reset,
        limit=args.limit,
    )
    print(stats)


if __name__ == "__main__":
    main()
