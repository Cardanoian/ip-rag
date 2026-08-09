# 발명 아이디어 유사 자료 검색 서비스

학생이 작성한 발명 아이디어와 전국학생과학발명품경진대회 수상작 문서를
의미 기반으로 비교해, 검토할 만한 유사 자료를 반환하는 내부 검색 API입니다.

> 이 서비스의 점수는 **신규성, 진보성, 특허 가능성 또는 침해 여부의 판정값이
> 아닙니다.** 교사와 학생이 기존 자료를 찾아 차이점을 생각하도록 돕는 검색
> 보조 자료입니다.

## 현재 범위

- 수상작 문서: 1979~2017년, 약 12,630개 Markdown 문서
- 검색: Gemini gemini-embedding-2 1536차원 임베딩 + ChromaDB
- 처리: 길이 적응형 청킹, 작품 단위 중복 제거와 점수 집계
- API: FastAPI POST /v1/search
- 연동: Rails 서버만 호출하는 Bearer 서비스 토큰

현재 색인에는 특허 문헌이 없습니다. 특허 검색은 KIPRIS 등 합법적인 데이터
취득 경로, 서지정보 정규화, 공개번호 기준 중복 제거, 별도 평가셋이 필요한
후속 기능입니다. 대회 수상작과 특허는 서로 다른 corpus로 유지하고 결과에서
출처를 구분하는 방식을 권장합니다.

## 개인정보와 공개 저장소 주의

/v1/search는 원문 파일 경로와 수상작 저자명을 반환하지 않습니다. 대신
내부 경로를 해시한 document_id를 제공합니다.

하지만 docs/ 원본 파일명과 본문 자체에는 과거 참가자 이름 등 개인을 식별할
수 있는 정보가 있을 수 있습니다. 저장소나 corpus를 공개 배포하기 전 다음을
별도로 확인해야 합니다.

- 문서 재배포·AI 임베딩 처리 권한
- 이름 등 개인정보의 공개 필요성 및 보존 근거
- 필요 시 원본 저장소 비공개 전환, 파일명 가명화, 접근 권한 제한

API 응답에서 이름을 숨기는 것만으로 공개 저장소의 개인정보 문제가 해결되지는
않습니다.

## 구조

~~~text
Co-AI Rails
  └─ Authorization: Bearer <RAG_API_TOKEN>
       └─ FastAPI /v1/search
            ├─ Gemini query embedding
            └─ local ChromaDB index
                 └─ competition award documents
~~~

FastAPI는 검색만 담당합니다. 학생 계정, 프로젝트, 검색 이력, 교사 대시보드,
AI 피드백은 Rails가 소유합니다. FastAPI에 학생 계정이나 프로젝트 데이터를
복제하지 않습니다.

## 설치

### 요구사항

- Python 3.11 이상
- Git LFS

~~~bash
git clone <repo-url>
cd ip-rag
git lfs install
git lfs pull
python -m venv .venv
~~~

가상환경을 활성화한 뒤:

~~~bash
python -m pip install -r requirements.txt
cp .env.example .env
~~~

개발용 .env 예:

~~~dotenv
GEMINI_API_KEY=...
RAG_API_TOKEN=충분히-긴-무작위-값
APP_ENV=development
CHROMA_PATH=chroma_db
INDEX_VERSION=inventions-gemini-embedding-2-1536-v1
CORPUS_ID=national-student-invention-awards-1979-2017
~~~

.env는 Git에 커밋하지 않습니다. 운영에서는 배포 플랫폼의 secret manager로
주입합니다.

## Rails Credentials 연동

Rails에는 FastAPI 자체의 Gemini 키를 넣을 필요가 없습니다. Rails가 알아야 할
값은 RAG 서버 주소와 두 서버가 공유하는 호출 토큰뿐입니다.

~~~bash
bin/rails credentials:edit
~~~

~~~yaml
rag:
  base_url: https://rag.example.com
  api_token: 충분히-긴-무작위-값
~~~

FastAPI 배포 환경의 RAG_API_TOKEN과 Rails credentials의 rag.api_token은
같아야 합니다. GEMINI_API_KEY는 FastAPI 서버의 secret manager에만 저장합니다.

## 색인 빌드

색인 빌드는 Gemini API 비용이 발생합니다. 먼저 일부 문서로 검증하세요.

~~~bash
python -m ingest.build_index --reset --limit 50
python -m ingest.build_index --reset
~~~

| 옵션 | 설명 |
|---|---|
| --reset | 기존 컬렉션을 삭제하고 재색인 |
| --limit N | 처음 N개 문서만 처리 |
| --docs-dir PATH | 기본 docs/ 대신 사용할 디렉터리 |

임베딩 모델, 차원, task prefix를 바꾸면 기존 벡터와 섞지 말고 전체 재색인해야
합니다. 배포할 때는 INDEX_VERSION도 함께 변경합니다.

## 서버 실행

~~~bash
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
~~~

- API 문서: http://localhost:8000/docs
- 생존 확인: GET /health
- 준비 확인: GET /ready

/health는 프로세스가 살아 있는지만 확인합니다. /ready는 Gemini 키, Chroma
색인, 운영 토큰 설정을 확인하고 준비되지 않았으면 HTTP 503을 반환합니다.

## API

### POST /v1/search

운영 환경에서는 Authorization 헤더가 필수입니다.

~~~bash
curl -X POST http://localhost:8000/v1/search \
  -H "Authorization: Bearer $RAG_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "공기 압력을 이용한 소형 발전 장치",
    "top_k": 5,
    "include_advisor_docs": false
  }'
~~~

응답 예:

~~~json
{
  "query": "공기 압력을 이용한 소형 발전 장치",
  "results": [
    {
      "document_id": "1b4f04e97d45d5e28e6d9217",
      "title": "공기의 압력을 이용한 미니 발전기",
      "year": 1979,
      "category": "과학완구",
      "doc_type": "작품설명서",
      "similarity": 0.876,
      "snippet": "공기 압력의 변화를 이용하여..."
    }
  ],
  "count": 1,
  "corpus_id": "national-student-invention-awards-1979-2017",
  "index_version": "inventions-gemini-embedding-2-1536-v1",
  "score_kind": "rescaled_cosine_match",
  "notice": "검색 결과는 유사 자료 탐색을 돕는 참고 정보이며, 신규성·특허 가능성을 판정하지 않습니다."
}
~~~

similarity는 Chroma cosine distance를 0~1 범위로 재조정한 **매칭 점수**입니다.
법적 의미의 독창성 점수가 아닙니다. 화면에서는 “유사도 87.6%”보다
“유사 자료 매칭 점수 0.876”처럼 표시하고, 학생이 차이점을 직접 기록하게 하는
것이 안전합니다.

기존 POST /search는 하위 호환을 위해 유지하지만 deprecated 상태이며, 저자와
내부 경로를 포함하므로 새 Co-AI 연동에서는 사용하지 않습니다.

### 주요 오류

| 상태 | 의미 |
|---|---|
| 401 | 서비스 토큰 누락 또는 불일치 |
| 422 | 빈 문자열, 길이 초과 등 잘못된 요청 |
| 503 | 토큰 미구성, Gemini/색인 장애, 동시 요청 초과 |
| 500 | 예기치 않은 내부 오류 |

공급자 예외의 상세 내용은 API 응답에 노출하지 않습니다.

## Docker 배포

~~~bash
docker build -t ip-rag .
~~~

색인 생성 시 corpus와 데이터 볼륨을 마운트합니다.

~~~bash
docker volume create ip-rag-data
docker run --rm \
  -e GEMINI_API_KEY=... \
  -e CHROMA_PATH=/data/chroma_db \
  -v "$PWD/docs:/app/docs:ro" \
  -v ip-rag-data:/data \
  ip-rag python -m ingest.build_index --reset
~~~

API 실행:

~~~bash
docker run --rm -p 8000:8000 \
  -e APP_ENV=production \
  -e GEMINI_API_KEY=... \
  -e RAG_API_TOKEN=... \
  -e CHROMA_PATH=/data/chroma_db \
  -e INDEX_VERSION=inventions-gemini-embedding-2-1536-v1 \
  -v ip-rag-data:/data \
  ip-rag
~~~

로컬 ChromaDB를 사용하므로 우선 **단일 컨테이너·단일 Uvicorn worker**로
운영합니다. 요청 처리는 내부 thread pool과 semaphore로 병렬화됩니다. 여러
컨테이너가 같은 Chroma 디렉터리를 동시에 공유하는 수평 확장은 권장하지
않습니다. 그 규모가 필요해지면 관리형 벡터 DB나 별도 Chroma 서버 전환을
검토합니다.

## 테스트

~~~bash
python -m pytest -q
~~~

현재 단위·통합·운영 회귀 테스트는 90개이며 실제 Gemini API를 호출하지 않습니다.
GitHub Actions도 같은 명령을 실행합니다.

## 아직 어려운 부분

- 실제 전체 색인 비용·시간 측정
- 교사가 검토한 정답셋으로 검색 정확도와 임계값 평가
- 작품명·부품명처럼 정확한 단어를 보완하는 키워드/하이브리드 검색
- 특허 문헌 수집·갱신·출처 링크·법적 고지
- 무중단 색인 교체와 이전 색인 롤백
- 공개 corpus의 저작권·개인정보 검토

세부 후속 작업은 [TODO.md](TODO.md)를 참고하세요.
