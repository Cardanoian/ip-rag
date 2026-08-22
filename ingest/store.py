"""ChromaDB 접근 공유 계약 — build_index 와 search 가 동일 컬렉션을 사용한다.

corpus 하나당 컬렉션 하나이며, 컬렉션 이름은 `{base}_v{n}` 형태다. 재색인은 새 버전
컬렉션을 만든 뒤 corpus의 활성 포인터를 옮기는 방식이라 검색이 중단되지 않는다.

레코드 1건 = 청크 1개.
  id        : f"{source_path}::{chunk_index}"
  embedding : Gemini gemini-embedding-2 벡터 (corpus의 embed_dim 차원)
  document  : 청크 원문(스니펫용)
  metadata  : kind.metadata_fields + {source_path, chunk_index, n_chunks, content_hash}
유사도 공간: cosine.
"""
from __future__ import annotations

import logging
import threading

import chromadb

import config

logger = logging.getLogger(__name__)

_client = None
_collections: dict[str, object] = {}
_lock = threading.Lock()


def chunk_id(source_path: str, chunk_index: int) -> str:
    return f"{source_path}::{chunk_index}"


def get_client():
    global _client
    with _lock:
        if _client is None:
            config.CHROMA_PATH.mkdir(parents=True, exist_ok=True)
            _client = chromadb.PersistentClient(path=str(config.CHROMA_PATH))
        return _client


def get_collection(name: str, reset: bool = False):
    """이름으로 영속 컬렉션을 가져온다. reset=True 면 삭제 후 재생성."""
    client = get_client()
    if reset:
        drop_collection(name)
    with _lock:
        collection = _collections.get(name)
        if collection is None:
            collection = client.get_or_create_collection(
                name=name,
                metadata={"hnsw:space": "cosine"},
            )
            _collections[name] = collection
        return collection


def drop_collection(name: str) -> None:
    """컬렉션을 삭제한다. 없으면 조용히 넘어간다."""
    try:
        get_client().delete_collection(name)
    except Exception:
        logger.debug("drop_collection: %s 없음 또는 삭제 불가", name)
    with _lock:
        _collections.pop(name, None)


def list_collections() -> list[str]:
    try:
        return [c.name for c in get_client().list_collections()]
    except Exception:
        logger.exception("list_collections 실패")
        return []


def collection_exists(name: str) -> bool:
    return name in list_collections()


def count_documents(name: str) -> int:
    """컬렉션의 청크 수. 존재하지 않으면 0 — 컬렉션을 새로 만들지 않는다."""
    if not collection_exists(name):
        return 0
    try:
        return get_collection(name).count()
    except Exception:
        logger.exception("count_documents 실패: %s", name)
        return 0


def reset_cache() -> None:
    """테스트용: 모듈 캐시 초기화."""
    global _client
    with _lock:
        _client = None
        _collections.clear()
