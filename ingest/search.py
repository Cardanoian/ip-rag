"""질의 임베딩 → Chroma 검색 → 문서 단위 집계."""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any

import config
from corpora.kinds import kind_of
from ingest.embedder import embed_query
from ingest.store import get_collection


def _collapse_snippet(text: str, max_chars: int = 200) -> str:
    """연속 공백/개행을 단일 공백으로 접은 뒤 max_chars로 자른다."""
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) > max_chars:
        return collapsed[:max_chars]
    return collapsed


def public_document_id(source_path: str) -> str:
    """내부 파일 경로 대신 반환할 안정적인 불투명 ID."""
    return hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:24]


def search(
    cfg,
    query: str,
    top_k: int = config.DEFAULT_TOP_K,
    options: dict[str, Any] | None = None,
) -> list[dict]:
    """corpus에서 유사 자료를 문서 단위로 반환한다.

    Args:
        cfg: 대상 corpus 설정.
        query: 검색 질의.
        top_k: 반환할 문서 수.
        options: corpus 종류별 검색 옵션 (예: include_advisor_docs).

    Returns:
        similarity 내림차순 문서 dict 리스트. metadata는 kind의 public_fields만 담는다.
    """
    kind = kind_of(cfg)
    qvec = embed_query(query, cfg)
    where = kind.build_where(cfg, options or {})

    col = get_collection(cfg.active_collection)
    raw = col.query(
        query_embeddings=[qvec],
        n_results=top_k * config.OVERFETCH_MULTIPLIER,
        where=where,
        include=["metadatas", "documents", "distances"],
    )

    distances: list[float] = raw["distances"][0]
    metadatas: list[dict] = raw["metadatas"][0]
    documents: list[str] = raw["documents"][0]

    # Chroma cosine distance는 1-cosine이다. 아래 값은 원시 cosine을
    # 사람이 보기 쉬운 0~1 범위로 다시 조정한 매칭 점수다.
    def _to_sim(distance: float) -> float:
        return max(0.0, min(1.0, 1.0 - distance / 2.0))

    groups: dict[str, list[tuple[float, str, dict]]] = {}
    for distance, metadata, document in zip(distances, metadatas, documents):
        source_path = metadata["source_path"]
        groups.setdefault(source_path, []).append(
            (_to_sim(distance), document, metadata)
        )

    works: list[dict] = []
    for source_path, chunks in groups.items():
        similarities = [chunk[0] for chunk in chunks]
        max_similarity = max(similarities)
        n_chunks: int = chunks[0][2].get("n_chunks", 1)

        penalty = config.LENGTH_NORM_C * math.log(max(n_chunks, 1))
        score = max_similarity - penalty
        mean_similarity = sum(similarities) / len(similarities)

        best_chunk = max(chunks, key=lambda chunk: chunk[0])
        metadata = best_chunk[2]

        works.append(
            {
                "document_id": public_document_id(source_path),
                "title": metadata.get("title", ""),
                "source_path": source_path,
                "similarity": round(score, 4),
                "snippet": _collapse_snippet(best_chunk[1]),
                "metadata": kind.public_metadata(metadata),
                "_raw_metadata": metadata,
                "_mean_sim": mean_similarity,
            }
        )

    works.sort(
        key=lambda work: (work["similarity"], work["_mean_sim"]),
        reverse=True,
    )
    works = works[:top_k]
    for work in works:
        del work["_mean_sim"]

    return works
