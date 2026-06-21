"""중앙 설정 — 모든 모듈이 이 상수/계약을 공유한다.

환경변수(.env):
  GEMINI_API_KEY   : Gemini API 키 (필수, 임베딩 호출 시)
"""
from __future__ import annotations

import os
from pathlib import Path

# --- 경로 ---
PROJECT_ROOT = Path(__file__).resolve().parent
DOCS_DIR = PROJECT_ROOT / "docs"
CHROMA_PATH = PROJECT_ROOT / "chroma_db"
COLLECTION_NAME = "inventions"

# --- 임베딩 (Gemini gemini-embedding-2) ---
EMBED_MODEL = "gemini-embedding-2"
# output_dimensionality: 128~3072 (기본 3072). 품질/저장 균형으로 1536 채택.
EMBED_DIM = 1536
# gemini-embedding-2 입력 한도(토큰)
MAX_INPUT_TOKENS = 8192
# 임베딩 배치 크기 (Batch/embed_content 호출당 contents 수)
EMBED_BATCH_SIZE = 100

# gemini-embedding-2 는 task_type 파라미터가 없으므로 프롬프트에 task 지시문을 직접 포함한다.
# 비대칭 검색: 문서/질의에 서로 다른 지시문 프리픽스를 일관 적용한다.
DOC_TASK_PREFIX = "[검색 대상 문서] 다음은 학생 발명품 작품 설명서입니다. 핵심 발명 아이디어를 검색용으로 표현합니다.\n"
QUERY_TASK_PREFIX = "[검색 질의] 다음 발명 아이디어와 유사한 기존 발명 작품을 찾습니다.\n"

# --- 청킹 (길이 적응형 하이브리드) ---
# 토큰 수가 MAX_INPUT_TOKENS 이하이면 통째 1청크, 초과하면 아래 크기로 분할.
CHUNK_SIZE = 1000        # 문자 기준
CHUNK_OVERLAP = 150      # 문자 기준
# 문자수 1차 필터(토큰 측정 전 빠른 분기). 실제 분기는 토큰 수로 확정한다.
SINGLE_CHUNK_CHAR_HINT = 5500

# --- 데이터 품질 ---
MIN_DOC_CHARS = 50       # 이 미만(0바이트 포함)인 문서는 색인 skip

# --- 검색 ---
DEFAULT_TOP_K = 5
OVERFETCH_MULTIPLIER = 5   # 작품 단위 dedup 후 top_k 보장을 위해 N*배수 만큼 청크 조회
# length-normalized max 집계 보정 계수: score - LENGTH_NORM_C * log(n_chunks)
LENGTH_NORM_C = 0.0        # 기본 0(순수 max). 평가셋(AC2b) 튜닝으로 확정.

# 기본 검색에서 제외할 doc_type (include_advisor_docs=True 시 포함)
ADVISOR_DOC_TYPE = "지도논문"
MAIN_DOC_TYPE = "작품설명서"

# --- API ---
MAX_QUERY_CHARS = 10000

# --- 비밀값 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


def require_api_key() -> str:
    """API 키가 없으면 명확한 에러를 던진다."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY 환경변수가 설정되지 않았습니다. .env 또는 환경에 키를 설정하세요."
        )
    return key
