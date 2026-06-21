# 발명 아이디어 유사도 검색 RAG 시스템

학생이 발명 아이디어를 텍스트로 제출하면, 기존 수상작(1979~2017년, 약 1.26만 건) 중 유사한 아이디어가 있었는지를 판별하는 **검색 API**입니다. FastAPI + ChromaDB + Gemini 임베딩으로 구성되어 있으며, 유사도 점수와 함께 JSON으로 결과를 반환합니다.

## 개요

- **목적**: 학생 발명 아이디어의 신규성 판별을 위한 기존 수상작 유사도 검색
- **데이터**: `docs/` 디렉터리의 ~12,630개 마크다운 문서 (총 ~234MB)
- **아키텍처**: ChromaDB(로컬 벡터 저장소) + Gemini `gemini-embedding-2`(1536차원 임베딩) + FastAPI + 길이 적응형 청킹 + 작품 단위 집계

## 사전 준비

### 요구사항

- Python 3.11 이상
- Git LFS (`git lfs install`)

### 설치 단계

1. **저장소 클론 및 LFS 설정**
   ```bash
   git clone <repo-url>
   cd ip-rag
   git lfs install
   git lfs pull  # docs/ 파일 다운로드 (~234MB)
   ```

2. **Python 환경 설정**
   ```bash
   pip install -r requirements.txt
   ```

3. **환경 변수 설정**
   ```bash
   cp .env.example .env
   # .env 파일을 열어 GEMINI_API_KEY 설정
   ```

   `.env` 파일:
   ```
   GEMINI_API_KEY=your-gemini-api-key-here
   ```

   **주의**: `.env`는 Git에 추적되지 않습니다(`.gitignore` 설정). API 키를 코드나 VCS에 노출하지 마세요.

## 색인 빌드

**주의: 색인 빌드는 Gemini API 호출을 수반하며 비용이 발생합니다.**

색인을 처음 빌드하거나 문서를 추가한 후에는 다음 명령어를 실행하세요.

```bash
python -m ingest.build_index
```

### 옵션

| 옵션              | 설명                                      |
| ----------------- | ----------------------------------------- |
| `--reset`         | 기존 색인을 삭제하고 처음부터 재색인      |
| `--limit N`       | 처음 N개 문서만 처리 (테스트/비용 절감용) |
| `--docs-dir PATH` | 스캔할 문서 디렉터리 (기본: `docs/`)      |

### 예시

```bash
# 전체 문서 색인 (첫 실행)
python -m ingest.build_index --reset

# 처음 10개 문서만으로 테스트 색인
python -m ingest.build_index --reset --limit 10

# 진행 상황 확인 (로그 레벨 DEBUG)
DEBUG=1 python -m ingest.build_index
```

색인 완료 후 `chroma_db/` 디렉터리에 로컬 벡터 데이터베이스가 생성됩니다.

## 서버 실행

색인 빌드 후 검색 API 서버를 실행하세요.

```bash
# 방법 1: uvicorn 직접 실행 (권장 — 자동 리로드)
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000

# 방법 2: Python 스크립트로 실행
python main.py
```

서버가 시작되면:
- **API 문서**: http://localhost:8000/docs (Swagger UI)
- **상태 확인**: http://localhost:8000/health

## API 사용

### 엔드포인트: `POST /search`

발명 아이디어를 제출하고 유사한 수상작을 검색합니다.

**요청**
```json
{
  "text": "공기 압력을 이용한 소형 발전 장치",
  "top_k": 5,
  "include_advisor_docs": false
}
```

| 파라미터               | 타입          | 기본값 | 설명                                     |
| ---------------------- | ------------- | ------ | ---------------------------------------- |
| `text`                 | string (필수) | -      | 검색할 발명 아이디어 텍스트 (1~10,000자) |
| `top_k`                | integer       | 5      | 반환할 결과 개수 (1~50)                  |
| `include_advisor_docs` | boolean       | false  | 지도논문 포함 여부                       |

**응답** (HTTP 200)
```json
{
  "query": "공기 압력을 이용한 소형 발전 장치",
  "results": [
    {
      "title": "공기의 압력을 이용한 미니 발전기",
      "year": 1979,
      "category": "과학완구",
      "author": "강용환",
      "doc_type": "작품설명서",
      "source_path": "docs/1979-과학완구-강용환-공기의 압력을 이용한 미니 발전기-.md",
      "similarity": 0.876,
      "snippet": "공기 압력의 변화를 이용하여 작은 크기의 발전기를 개발했다..."
    },
    {
      "title": "풍력을 이용한 전기 발전기",
      "year": 1995,
      "category": "과학완구",
      "author": "이순신",
      "doc_type": "작품설명서",
      "source_path": "docs/1995-과학완구-이순신-풍력을 이용한 전기 발전기-.md",
      "similarity": 0.812,
      "snippet": "바람을 이용한 자연 에너지를 전기로 변환하는 장치..."
    }
  ],
  "count": 2
}
```

| 응답 필드 | 설명                                           |
| --------- | ---------------------------------------------- |
| `query`   | 입력된 검색 텍스트                             |
| `results` | 유사도 높은 순서로 정렬된 작품 배열            |
| `count`   | 실제 반환된 결과 개수 (`top_k` 이하일 수 있음) |

**결과 필드 설명**
- `similarity`: 유사도 점수 (0~1, 높을수록 유사함)
- `snippet`: 매칭된 내용 발췌
- `doc_type`: `"작품설명서"` 또는 `"지도논문"`

**에러 응답**

| HTTP 코드 | 상황                                       |
| --------- | ------------------------------------------ |
| 400       | 잘못된 입력 (빈 문자열, 초과 길이)         |
| 503       | Gemini API 사용 불가 (키 미설정, API 장애) |
| 500       | 서버 내부 오류                             |

### curl 예시

```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{
    "text": "태양열을 이용한 온수기",
    "top_k": 3,
    "include_advisor_docs": false
  }'
```

### 엔드포인트: `GET /health`

서버 상태 확인 (헬스 체크).

```bash
curl http://localhost:8000/health
```

응답:
```json
{"status": "ok"}
```

## 프로젝트 구조

```
ip-rag/
├── config.py                    # 전역 설정 (모델명, 청크 크기, DB 경로, 기본값)
├── main.py                      # uvicorn 진입점
├── requirements.txt             # Python 패키지 의존성
├── .env.example                 # 환경 변수 템플릿
├── .gitattributes               # Git LFS 설정
├── README.md                    # 이 파일
│
├── ingest/                      # 색인 빌드 모듈
│   ├── parse.py                 # 파일명 파싱, 메타데이터 추출
│   ├── chunker.py               # 길이 적응형 청킹
│   ├── embedder.py              # Gemini 임베딩 추상화 (배치 + 재시도)
│   ├── store.py                 # ChromaDB 저장소 인터페이스
│   ├── search.py                # 검색 로직 (집계, dedup, 정렬)
│   └── build_index.py           # CLI: 전체 문서 색인화
│
├── api/                         # FastAPI 서버
│   ├── main.py                  # API 앱 정의 및 엔드포인트
│   └── schemas.py               # Pydantic 요청/응답 스키마
│
├── tests/                       # 단위 및 통합 테스트
│   └── test_*.py                # 82개 테스트 (실제 Gemini 호출 미포함)
│
├── docs/                        # 발명 아이디어 문서 (Git LFS)
│   └── *.md                     # 12,630개 마크다운 파일
│
└── chroma_db/                   # 로컬 벡터 저장소 (자동 생성, .gitignore)
```

### 핵심 모듈 설명

| 모듈                    | 역할                                                              |
| ----------------------- | ----------------------------------------------------------------- |
| `config.py`             | 임베딩 모델, 청킹 크기, ChromaDB 경로, API 제한값 등 중앙 설정    |
| `ingest/parse.py`       | 파일명 정규식으로 {연도, 분야, 저자, 제목} 추출; NFC 정규화       |
| `ingest/chunker.py`     | 짧은 문서는 통째로, 긴 문서는 1,000자 단위로 분할 (overlap 150자) |
| `ingest/embedder.py`    | Gemini API 호출 추상화; 배치 처리, exponential backoff 재시도     |
| `ingest/store.py`       | ChromaDB 컬렉션 초기화, 청크 ID 생성, 메타데이터 관리             |
| `ingest/search.py`      | 질의 임베딩 → Chroma 검색 → 작품 단위 집계 (max 유사도) → 정렬    |
| `ingest/build_index.py` | docs/ 전체 순회 → 멱등성 유지 (content_hash 비교) → 배치 저장     |
| `api/main.py`           | FastAPI 앱; 엔드포인트 정의, 에러 처리, 동시성 제어               |
| `api/schemas.py`        | 요청/응답 Pydantic 모델 (검증 포함)                               |

## 테스트 실행

단위 테스트 및 통합 테스트를 실행합니다. (실제 Gemini API 호출 미포함)

```bash
# 전체 테스트 실행
python -m pytest -v

# 간단한 출력
python -m pytest -q

# 특정 테스트 파일만 실행
python -m pytest tests/test_search.py -v

# 커버리지 확인 (선택)
python -m pytest --cov=ingest --cov=api tests/
```

**현재 테스트 상태**: 82개 테스트 통과
- 파일 파싱: 정규식, 메타데이터, NFC 정규화 검증
- 청킹: 단문/장문 경계 테스트
- 검색: 작품 단위 집계, 필터링, 정렬 로직
- API: 요청 검증, 응답 스키마

## 설계 노트

### 아키텍처 결정

1. **임베딩 모델**: Gemini `gemini-embedding-2`
   - 한국어 품질 우수
   - GPU/로컬 모델 불필요 (API 호출)
   - 1536차원(품질/저장 균형)

2. **벡터 저장소**: ChromaDB (로컬 지속성)
   - 규모(~12,630 문서) 로컬 실행 적합
   - 메타데이터 필터 기본 지원

3. **청킹**: 길이 적응형
   - ≤5,500자 문서(45.5%): 통째로 1청크 → 임베딩 1회
   - >5,500자 문서(54.5%): 1,000자 단위 분할 (overlap 150자) → 배치 임베딩

4. **결과 집계**: 작품 단위 (파일 기준)
   - 다중 청크 작품: max 유사도 사용
   - 단일 청크와 동일 스케일 유지 (비교 가능)
   - 스니펫: 매칭 청크 또는 본문 앞부분 발췌

5. **멱등성**: content_hash 기반 재사용
   - 동일 내용 → 기존 임베딩 재사용 (API 호출 절약)
   - 수정된 문서 → 고아 청크 삭제 후 재임베딩

### 메타데이터 파싱

파일명 패턴: `{YYYY}-{분야}-{저자}-{제목}-.md`
- 정규식: `^(\d{4})-([^-]+)-([^-]+)-(.+)-\.md$`
- NFC 정규화 적용 (로마숫자, 스마트쿠오트, 중점 통일)
- 실패 시 경고 로그 + fallback 처리

### 지도논문 필터링

- **doc_type 태깅**: 제목 끝 `(지도논문)` 접미사로 구분
- **기본 검색**: 작품설명서만 포함
- **`include_advisor_docs=true`**: 지도논문 포함 가능

## 향후 작업 (미해결)

1. **평가셋 기반 집계 산식 튜닝**: 20~30개 인간 라벨 유사 쌍으로 max vs length-normalized max vs 평균 A/B 비교
2. **임베딩 차원 최적화**: 1536 vs 3072 (정확도 vs 저장·검색 성능)
3. **Docker 패키징**: 프로덕션 배포용 컨테이너
4. **색인 비용 산정**: 12,630문서 × 평균 청크 수 = API 호출 예상 비용
5. **지도논문 중복도 측정**: 색인 후 작품설명서와의 실제 중복률 분석

## 문제 해결

### "GEMINI_API_KEY 환경변수가 설정되지 않았습니다"

`.env` 파일이 없거나 `GEMINI_API_KEY`가 비어 있습니다.

```bash
cp .env.example .env
# .env를 열어 실제 API 키 입력
```

### 색인 빌드가 느린 경우

- Gemini API rate limit이 활성화되었을 수 있습니다. 배치 크기(기본 100)는 `config.py`에서 조정 가능합니다.
- 테스트용으로 `--limit 10` 옵션으로 먼저 시도해보세요.

### "Git LFS에서 docs/ 파일을 가져올 수 없습니다"

```bash
git lfs install
git lfs pull
```

### 검색 결과가 부족한 경우

- `count < top_k`라면 색인된 전체 문서 중 매칭 문서가 `top_k`개 미만입니다. `over-fetch(top_k×5)`를 시도했으나 부족합니다.
- 입력 텍스트를 더 구체적으로 작성하거나 `top_k`를 줄여보세요.

## 문의 및 기여

이 프로젝트는 학생 발명 아이디어 신규성 판별을 위한 연구 목적 시스템입니다.

---

**최종 업데이트**: 2026-06-22  
**Python 버전**: 3.11+  
**라이선스**: [프로젝트 라이선스 명시]
