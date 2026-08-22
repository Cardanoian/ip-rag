"""테스트 공통 격리 — 모든 테스트가 자기만의 데이터 디렉터리를 쓴다.

corpus 정의가 SQLite에 있으므로 Chroma 경로뿐 아니라 운영 DB와 문서 루트도
테스트마다 새로 잡아야 한다. 하나라도 새면 테스트 간 corpus가 섞인다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import corpora
import storage
from corpora.models import CorpusConfig
from ingest import store

# 세션 미들웨어가 기동 시점에 키를 요구한다.
os.environ.setdefault("SESSION_SECRET", "test-session-secret")


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """config 경로를 tmp로 돌리고 모든 모듈 캐시를 비운다.

    개발자 로컬 .env(APP_ENV=production 등)가 테스트 결과를 바꾸면 안 되므로
    환경도 함께 중립화한다.
    """
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DOCS_ROOT", data_dir / "docs")
    monkeypatch.setattr(config, "APP_DB_PATH", data_dir / "app.db")
    monkeypatch.setattr(config, "CHROMA_PATH", data_dir / "chroma_db")

    # api/auth.py 는 os.environ을, config.is_production()은 모듈 상수를 본다.
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setattr(config, "APP_ENV", "development")
    monkeypatch.setattr(config, "session_secret_is_ephemeral", False)
    monkeypatch.delenv("RAG_API_TOKEN", raising=False)
    monkeypatch.delenv("INDEX_VERSION", raising=False)
    monkeypatch.delenv("CORPUS_ID", raising=False)

    storage.reset_cache()
    corpora.invalidate_cache()
    store.reset_cache()
    yield data_dir
    storage.reset_cache()
    corpora.invalidate_cache()
    store.reset_cache()


@pytest.fixture()
def seed_corpus() -> CorpusConfig:
    """시드 발명 corpus (kind=invention)."""
    corpora.ensure_seed()
    return corpora.get(corpora.SEED_CORPUS_ID)


@pytest.fixture()
def plain_corpus() -> CorpusConfig:
    """관리자가 만들 법한 일반 텍스트 corpus."""
    corpora.ensure_seed()
    return corpora.create(
        corpus_slug="rules",
        label="학교 규정",
        kind="plain",
        corpus_id="school-rules",
        doc_prefix="[검색 대상 문서] 다음은 학교 규정 문서입니다.",
        query_prefix="[검색 질의] 관련 규정을 찾습니다.",
        embed_dim=1536,
        chunk_size=1000,
        chunk_overlap=150,
        single_chunk_char_hint=5500,
        created_by="tester",
    )


@pytest.fixture()
def docs_dir(seed_corpus) -> Path:
    """시드 corpus의 문서 디렉터리."""
    path = seed_corpus.docs_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def fake_embed_documents(texts: list[str], cfg) -> list[list[float]]:
    """결정적 가짜 문서 임베딩 — API 호출 없이 색인 경로를 검증한다."""
    return [[float(i % 10) * 0.1] * cfg.embed_dim for i, _ in enumerate(texts)]


def fake_embed_query(text: str, cfg) -> list[float]:
    return [0.0] * cfg.embed_dim


@pytest.fixture()
def patch_embed(monkeypatch):
    """색인·검색 양쪽의 임베딩을 가짜로 바꾼다."""
    import ingest.build_index as build_index_module
    import ingest.search as search_module

    monkeypatch.setattr(build_index_module, "embed_documents", fake_embed_documents)
    monkeypatch.setattr(search_module, "embed_query", fake_embed_query)
