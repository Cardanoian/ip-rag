"""중앙 설정 — 모든 모듈이 이 상수/계약을 공유한다.

환경변수(.env):
  GEMINI_API_KEY : Gemini API 키 (임베딩 호출 시 필수)
  RAG_API_TOKEN  : Rails → RAG 서비스 인증용 공유 토큰 (production 필수)
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# --- 경로 ---
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

DOCS_DIR = Path(os.getenv("DOCS_DIR", str(PROJECT_ROOT / "docs")))
CHROMA_PATH = Path(os.getenv("CHROMA_PATH", str(PROJECT_ROOT / "chroma_db")))
COLLECTION_NAME = "inventions"

# --- 임베딩 (Gemini gemini-embedding-2) ---
EMBED_MODEL = "gemini-embedding-2"
# output_dimensionality: 128~3072 (기본 3072). 품질/저장 균형으로 1536 채택.
EMBED_DIM = 1536
# gemini-embedding-2 입력 한도(토큰)
MAX_INPUT_TOKENS = 8192
# 임베딩 배치 크기 (Batch/embed_content 호출당 contents 수)
EMBED_BATCH_SIZE = 100
GEMINI_TIMEOUT_MS = int(os.getenv("GEMINI_TIMEOUT_MS", "30000"))

# gemini-embedding-2는 task_type 대신 텍스트 지시문을 사용한다.
# 비대칭 검색: 문서/질의에 서로 다른 지시문 프리픽스를 일관 적용한다.
DOC_TASK_PREFIX = (
    "[검색 대상 문서] 다음은 학생 발명품 작품 설명서입니다. "
    "핵심 발명 아이디어를 검색용으로 표현합니다.\n"
)
QUERY_TASK_PREFIX = (
    "[검색 질의] 다음 발명 아이디어와 유사한 기존 발명 작품을 찾습니다.\n"
)

# --- 청킹 (길이 적응형 하이브리드) ---
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150
SINGLE_CHUNK_CHAR_HINT = 5500

# --- 데이터 품질 ---
MIN_DOC_CHARS = 50

# --- 검색 ---
DEFAULT_TOP_K = 5
OVERFETCH_MULTIPLIER = 5
LENGTH_NORM_C = 0.0

ADVISOR_DOC_TYPE = "지도논문"
MAIN_DOC_TYPE = "작품설명서"

# --- API / 운영 ---
MAX_QUERY_CHARS = 10000
SEARCH_CONCURRENCY = int(os.getenv("SEARCH_CONCURRENCY", "10"))
SEARCH_QUEUE_TIMEOUT_SECONDS = float(
    os.getenv("SEARCH_QUEUE_TIMEOUT_SECONDS", "2.0")
)
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
INDEX_VERSION = os.getenv(
    "INDEX_VERSION",
    f"{COLLECTION_NAME}:{EMBED_MODEL}:{EMBED_DIM}:v1",
)
CORPUS_ID = os.getenv(
    "CORPUS_ID",
    "national-student-invention-awards-1979-2017",
)

# --- 비밀값 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
RAG_API_TOKEN = os.getenv("RAG_API_TOKEN")


def require_api_key() -> str:
    """API 키가 없으면 명확한 에러를 던진다."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY 환경변수가 설정되지 않았습니다. .env 또는 환경에 키를 설정하세요."
        )
    return key
