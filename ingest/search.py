"""검색 모듈 — 질의 임베딩 → Chroma 검색 → 작품 단위 집계.

공개 인터페이스:
  search(query, top_k, include_advisor_docs) -> list[dict]

반환 dict 키: title, year, category, author, doc_type, source_path,
              similarity (float, 소수점 4자리), snippet (str, ~200자)
"""
from __future__ import annotations

import math
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from ingest.embedder import embed_query
from ingest.store import get_collection


def _collapse_snippet(text: str, max_chars: int = 200) -> str:
    """연속 공백/개행을 단일 공백으로 접은 뒤 max_chars 로 자른다."""
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) > max_chars:
        return collapsed[:max_chars]
    return collapsed


def search(
    query: str,
    top_k: int = config.DEFAULT_TOP_K,
    include_advisor_docs: bool = False,
) -> list[dict]:
    """발명 작품 유사도 검색.

    Args:
        query: 검색할 텍스트.
        top_k: 반환할 작품(작업) 수.
        include_advisor_docs: True 이면 지도논문도 검색 대상에 포함.

    Returns:
        작품 단위 dict 리스트 (similarity 내림차순, 최대 top_k 개).
        over-fetch 후에도 작품 수가 top_k 미만이면 확보된 수만 반환.
    """
    # 1. 질의 임베딩
    qvec = embed_query(query)

    # 2. where 필터
    where: dict | None = (
        None if include_advisor_docs else {"doc_type": config.MAIN_DOC_TYPE}
    )

    # 3. Chroma over-fetch
    col = get_collection()
    n_fetch = top_k * config.OVERFETCH_MULTIPLIER
    raw = col.query(
        query_embeddings=[qvec],
        n_results=n_fetch,
        where=where,
        include=["metadatas", "documents", "distances"],
    )

    # Chroma 결과는 query 1개이므로 인덱스 0을 꺼낸다.
    distances: list[float] = raw["distances"][0]
    metadatas: list[dict] = raw["metadatas"][0]
    documents: list[str] = raw["documents"][0]

    # 4. cosine distance → similarity, 클램프 [0, 1]
    # Chroma cosine distance ∈ [0, 2], sim = 1 - d/2
    def _to_sim(d: float) -> float:
        return max(0.0, min(1.0, 1.0 - d / 2.0))

    # 5. source_path 기준 작품 단위 집계 (length-normalized max)
    # groups: source_path -> list of (sim, document, metadata)
    groups: dict[str, list[tuple[float, str, dict]]] = {}
    for dist, meta, doc in zip(distances, metadatas, documents):
        sp = meta["source_path"]
        sim = _to_sim(dist)
        groups.setdefault(sp, []).append((sim, doc, meta))

    works: list[dict] = []
    for sp, chunks in groups.items():
        sims = [c[0] for c in chunks]
        max_sim = max(sims)

        # n_chunks = 해당 문서의 전체 청크 수 (메타데이터에 기록된 값)
        # 어떤 청크든 동일한 n_chunks 를 갖는다; 첫 청크에서 읽는다.
        n_chunks: int = chunks[0][2].get("n_chunks", 1)

        # length-normalized max: c·log(n_chunks), log(1)=0 이므로 단일청크는 그대로
        penalty = config.LENGTH_NORM_C * math.log(max(n_chunks, 1))
        score = max_sim - penalty

        # tiebreak: 조회된 청크 sim 평균
        mean_sim = sum(sims) / len(sims)

        # snippet: max-sim 청크의 document 텍스트
        best_chunk = max(chunks, key=lambda c: c[0])
        snippet = _collapse_snippet(best_chunk[1])

        # 대표 메타데이터 (best 청크에서 가져옴)
        meta_rep = best_chunk[2]

        works.append(
            {
                "title": meta_rep.get("title", ""),
                "year": meta_rep.get("year", 0),
                "category": meta_rep.get("category", ""),
                "author": meta_rep.get("author", ""),
                "doc_type": meta_rep.get("doc_type", ""),
                "source_path": sp,
                "similarity": round(score, 4),
                "_mean_sim": mean_sim,  # tiebreak 용, 반환 전 제거
            }
        )
        works[-1]["snippet"] = snippet

    # 6. 정렬: score desc, tiebreak mean_sim desc → top_k 절삭
    works.sort(key=lambda w: (w["similarity"], w["_mean_sim"]), reverse=True)
    works = works[:top_k]

    # tiebreak 내부 키 제거
    for w in works:
        del w["_mean_sim"]

    return works
