"""질의 임베딩 → Chroma 검색 → 작품 단위 집계."""
from __future__ import annotations

import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from ingest.embedder import embed_query
from ingest.store import get_collection


def _collapse_snippet(text: str, max_chars: int = 200) -> str:
    """연속 공백/개행을 단일 공백으로 접은 뒤 max_chars로 자른다."""
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) > max_chars:
        return collapsed[:max_chars]
    return collapsed


def search(
    query: str,
    top_k: int = config.DEFAULT_TOP_K,
    include_advisor_docs: bool = False,
) -> list[dict]:
    """발명 작품 유사 자료를 작품 단위로 반환한다."""
    qvec = embed_query(query)

    where: dict | None = (
        None if include_advisor_docs else {"doc_type": config.MAIN_DOC_TYPE}
    )

    col = get_collection()
    n_fetch = top_k * config.OVERFETCH_MULTIPLIER
    raw = col.query(
        query_embeddings=[qvec],
        n_results=n_fetch,
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
    for distance, metadata, document in zip(
        distances,
        metadatas,
        documents,
    ):
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
        year = metadata.get("year")

        works.append(
            {
                "title": metadata.get("title", ""),
                "year": None if year == -1 else year,
                "category": metadata.get("category", ""),
                "author": metadata.get("author", ""),
                "doc_type": metadata.get("doc_type", ""),
                "source_path": source_path,
                "similarity": round(score, 4),
                "snippet": _collapse_snippet(best_chunk[1]),
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
