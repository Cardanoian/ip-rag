"""중앙 설정 — corpus에 종속되지 않는 상수/계약만 둔다.

corpus별 설정(컬렉션명, 임베딩 지시문, 청킹 파라미터, 차원)은 SQLite `corpora`
테이블이 소유한다. corpora/seed.py 참고.

환경변수(.env):
  GEMINI_API_KEY : Gemini API 키 (임베딩 호출 시 필수)
  RAG_API_TOKEN  : Rails → RAG 서비스 인증용 공유 토큰 (production 필수)
  SESSION_SECRET : 어드민 세션 쿠키 서명 키 (production 필수)
  DATA_DIR       : 문서·벡터·메타 DB를 담는 영속 볼륨 (기본 ./data)

문서는 저장소가 아니라 DATA_DIR 아래에만 존재한다. 관리자가 어드민 화면에서
올리거나 scripts.migrate_docs 로 외부 디렉터리에서 가져온다.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# --- 경로 ---
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

# 영속 볼륨 루트. Docker에서는 /data 를 마운트한다.
DATA_DIR = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data")))
# corpus별 원본 문서: DOCS_ROOT/{corpus_id}/*.md
DOCS_ROOT = Path(os.getenv("DOCS_ROOT", str(DATA_DIR / "docs")))
# corpora 정의, 관리자 계정, 색인 잡, 감사 로그를 담는 운영 DB
APP_DB_PATH = Path(os.getenv("APP_DB_PATH", str(DATA_DIR / "app.db")))
CHROMA_PATH = Path(os.getenv("CHROMA_PATH", str(DATA_DIR / "chroma_db")))

# --- 임베딩 (Gemini gemini-embedding-2) ---
EMBED_MODEL = "gemini-embedding-2"
# gemini-embedding-2 입력 한도(토큰)
MAX_INPUT_TOKENS = 8192
# 임베딩 배치 크기 (Batch/embed_content 호출당 contents 수)
EMBED_BATCH_SIZE = 100
GEMINI_TIMEOUT_MS = int(os.getenv("GEMINI_TIMEOUT_MS", "30000"))
# output_dimensionality 허용 범위. corpus별 embed_dim 검증에 쓴다.
EMBED_DIM_MIN = 128
EMBED_DIM_MAX = 3072

# --- 데이터 품질 ---
MIN_DOC_CHARS = 50

# --- 검색 ---
DEFAULT_TOP_K = 5
OVERFETCH_MULTIPLIER = 5
LENGTH_NORM_C = 0.0

# --- API / 운영 ---
MAX_QUERY_CHARS = 10000
SEARCH_CONCURRENCY = int(os.getenv("SEARCH_CONCURRENCY", "10"))
SEARCH_QUEUE_TIMEOUT_SECONDS = float(
    os.getenv("SEARCH_QUEUE_TIMEOUT_SECONDS", "2.0")
)
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()

# --- 어드민 ---
# 세션 쿠키 유효기간(초). 기본 8시간.
ADMIN_SESSION_MAX_AGE = int(os.getenv("ADMIN_SESSION_MAX_AGE", str(8 * 3600)))
# 업로드 요청 1건의 총 바이트 상한.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
# zip 해제 후 총 바이트 상한 (zip bomb 방어).
MAX_UNZIPPED_BYTES = int(os.getenv("MAX_UNZIPPED_BYTES", str(200 * 1024 * 1024)))
# alias 전환 후 남겨둘 이전 컬렉션 수 (롤백 여지).
KEEP_OLD_COLLECTIONS = int(os.getenv("KEEP_OLD_COLLECTIONS", "1"))

# --- 비밀값 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
RAG_API_TOKEN = os.getenv("RAG_API_TOKEN")
SESSION_SECRET = os.getenv("SESSION_SECRET")


def is_production() -> bool:
    return APP_ENV in {"production", "prod"}


# SESSION_SECRET 미설정으로 임시 키를 쓰고 있는지. /ready 가 이 값을 보고 경고한다.
session_secret_is_ephemeral = False


def resolve_session_secret() -> str:
    """어드민 세션 서명 키.

    production에서 키가 없으면 예측 가능한 고정 키로 떨어지지 않고 임의 키를 만든다.
    서명은 안전하지만 재시작마다 로그인이 풀리므로 /ready 가 경고를 띄운다.
    검색 API까지 기동 실패로 끌어내리지는 않는다.
    """
    global session_secret_is_ephemeral

    secret = os.getenv("SESSION_SECRET")
    if secret:
        return secret
    if is_production():
        import secrets as _secrets

        session_secret_is_ephemeral = True
        return _secrets.token_urlsafe(48)
    # 개발 환경 전용 고정 키. 프로세스를 재시작해도 로그인이 유지된다.
    return "dev-only-insecure-session-secret"


def require_api_key() -> str:
    """API 키가 없으면 명확한 에러를 던진다."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY 환경변수가 설정되지 않았습니다. .env 또는 환경에 키를 설정하세요."
        )
    return key
