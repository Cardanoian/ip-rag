"""ChromaDB 접근 공유 계약 — build_index 와 search 가 동일 컬렉션을 사용한다.

레코드 1건 = 청크 1개.
  id        : f"{source_path}::{chunk_index}"
  embedding : Gemini gemini-embedding-2 벡터 (config.EMBED_DIM 차원)
  document  : 청크 원문(스니펫용)
  metadata  : {year:int, category:str, author:str, title:str, doc_type:str,
               source_path:str, chunk_index:int, n_chunks:int, content_hash:str}
유사도 공간: cosine.
"""
from __future__ import annotations

import chromadb

import config

_client = None
_collection = None


def chunk_id(source_path: str, chunk_index: int) -> str:
    return f"{source_path}::{chunk_index}"


def get_client():
    global _client
    if _client is None:
        config.CHROMA_PATH.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=str(config.CHROMA_PATH))
    return _client


def get_collection(reset: bool = False):
    """영속 컬렉션을 가져온다. reset=True 면 기존 컬렉션을 삭제 후 재생성."""
    global _collection
    client = get_client()
    if reset:
        try:
            client.delete_collection(config.COLLECTION_NAME)
        except Exception:
            pass
        _collection = None
    if _collection is None:
        _collection = client.get_or_create_collection(
            name=config.COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def reset_cache() -> None:
    """테스트용: 모듈 캐시 초기화."""
    global _client, _collection
    _client = None
    _collection = None
