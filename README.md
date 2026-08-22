# 유사 자료 검색 서비스

주제별 자료를 의미 기반으로 검색해 관련 문서를 반환하는 내부 검색 API입니다.
현재 전국학생과학발명품경진대회 수상작 corpus가 기본 탑재되어 있고, 관리자가
어드민 화면에서 새 corpus(학교 규정, 복지 안내 등)를 만들어 추가할 수 있습니다.

> 이 서비스의 점수는 **신규성, 진보성, 특허 가능성 또는 침해 여부의 판정값이
> 아닙니다.** 교사와 학생이 기존 자료를 찾아 차이점을 생각하도록 돕는 검색
> 보조 자료입니다.

이 문서는 설치·배포·API 연동을 다룹니다.
**관리 화면 사용법은 [ADMIN_MANUAL.md](ADMIN_MANUAL.md)를 보세요.**

## 현재 범위

- 기본 corpus: 발명대회 수상작 1979~2017년, 약 12,630개 Markdown 문서
- 검색: Gemini gemini-embedding-2 임베딩 + ChromaDB (corpus별 컬렉션 분리)
- 처리: 길이 적응형 청킹, 문서 단위 중복 제거와 점수 집계
- API: FastAPI `POST /v1/corpora/{corpus_id}/search`
- 어드민: `/admin` — corpus 관리, 문서 업로드, 색인, 검색 테스트
- 연동: Rails 서버만 호출하는 Bearer 서비스 토큰

현재 색인에는 특허 문헌이 없습니다. 특허 검색은 KIPRIS 등 합법적인 데이터
취득 경로, 서지정보 정규화, 공개번호 기준 중복 제거, 별도 평가셋이 필요한
후속 기능입니다.

## 멀티 corpus 구조

corpus 하나 = Chroma 컬렉션 하나입니다. corpus마다 임베딩 지시문, 청킹
파라미터, 임베딩 차원이 다를 수 있고, 재색인과 삭제도 corpus 단위로 이뤄집니다.

corpus의 **동작**(파싱, 임베딩 입력 구성, 검색 필터)은 코드가 소유하고,
corpus의 **파라미터**(이름, 지시문, 청킹, 차원)는 SQLite가 소유합니다.
관리자는 종류를 고르고 파라미터를 입력할 뿐 파싱 코드를 건드리지 않습니다.

### corpus 종류

| 종류 | 용도 | 문서 형식 | 어드민에서 생성 |
|---|---|---|---|
| `invention` | 기존 수상작 corpus 전용 | `{연도}-{분야}-{저자}-{제목}-.md` 파일명 규칙 | 불가 |
| `plain` | 관리자가 만드는 모든 신규 corpus | `.md` / `.txt` — 파일명이 제목, 본문 전체가 검색 대상 | 가능 |

`plain`은 frontmatter도 파일명 규칙도 요구하지 않습니다. 어떤 텍스트를 올려도
색인되므로 파싱 실패라는 개념이 없습니다.

### corpus 상태

~~~text
초안(draft) ──색인 후 공개──> 공개(published) ──비공개──> 비공개(unpublished) ──완전삭제──> (없음)
                                    ^                            |
                                    └────────── 재공개 ──────────┘
~~~

- 초안과 비공개 corpus는 **검색 API에서 404**이며 `/v1/corpora` 목록에도 나오지
  않습니다. 어드민에서만 보이고 검색 테스트 콘솔로 품질을 확인할 수 있습니다.
- `/ready`는 공개된 corpus만 검사합니다. 갓 만든 빈 corpus가 헬스체크를
  깨뜨리지 않습니다.
- 완전삭제는 비공개 상태에서 최고관리자가 corpus 주소를 직접 입력해야 실행됩니다.

## 개인정보와 공개 저장소 주의

검색 API는 원문 파일 경로와 수상작 저자명을 반환하지 않습니다. 대신 내부
경로를 해시한 `document_id`를 제공합니다. 어떤 메타데이터를 외부에 노출할지는
corpus 종류가 `public_fields`로 선언하며, 발명 corpus는 여기서 저자를 제외합니다.

하지만 원본 파일명과 본문 자체에는 과거 참가자 이름 등 개인을 식별할 수 있는
정보가 있을 수 있습니다. 저장소나 corpus를 공개 배포하기 전 다음을 별도로
확인해야 합니다.

- 문서 재배포·AI 임베딩 처리 권한
- 이름 등 개인정보의 공개 필요성 및 보존 근거
- 필요 시 원본 저장소 비공개 전환, 파일명 가명화, 접근 권한 제한

API 응답에서 이름을 숨기는 것만으로 공개 저장소의 개인정보 문제가 해결되지는
않습니다.

## 구조

~~~text
Co-AI Rails                          관리자 브라우저
  └─ Bearer <RAG_API_TOKEN>            └─ 세션 쿠키 (별도 인증)
       └─ POST /v1/corpora/{id}/search      └─ /admin/*
            ├─ Gemini query embedding            ├─ corpus 관리
            └─ ChromaDB                          ├─ 문서 업로드·삭제
                 ├─ inventions_v1                ├─ 색인 잡 (단일 워커 스레드)
                 ├─ school-violence_v2           └─ 검색 테스트 콘솔
                 └─ welfare_v1
~~~

~~~text
config.py     corpus 중립 설정 (Gemini, 경로, 동시성, 시크릿)
storage.py    운영 SQLite — corpora / admin_users / jobs / audit_log
corpora/      corpus 정의 — kinds(코드) + registry(DB) + seed
ingest/       색인·검색 엔진 — 모든 함수가 corpus 설정을 인자로 받음
api/          검색 API
admin/        어드민 화면 — 인증, 권한, 잡 러너, 업로드, 라우트
scripts/      운영 스크립트
~~~

FastAPI는 검색과 자료 관리만 담당합니다. 학생 계정, 프로젝트, 검색 이력,
교사 대시보드, AI 피드백은 Rails가 소유합니다.

## 설치

### 요구사항

- Python 3.11 이상

~~~bash
git clone <repo-url>
cd ip-rag
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
~~~

**이 저장소에는 문서가 들어 있지 않습니다.** 코드만 담고, 검색 대상 문서는
`DATA_DIR` 볼륨에서 관리합니다. 관리자가 어드민 화면에서 올리거나
[대량 문서 가져오기](#대량-문서-가져오기)를 씁니다.

개발용 .env 예:

~~~dotenv
GEMINI_API_KEY=...
RAG_API_TOKEN=충분히-긴-무작위-값
SESSION_SECRET=충분히-긴-무작위-값
APP_ENV=development
DATA_DIR=./data
~~~

`SESSION_SECRET`은 어드민 세션 쿠키 서명에 쓰입니다. production에서 설정하지
않으면 임의 키로 기동하므로 **서버를 재시작할 때마다 관리자 로그인이 풀리고**
`/ready`가 503을 반환합니다.

.env는 Git에 커밋하지 않습니다. 운영에서는 배포 플랫폼의 secret manager로
주입합니다.

### 데이터 디렉터리

`DATA_DIR` 하나에 모든 영속 데이터가 담깁니다. 백업 대상은 이 디렉터리입니다.

~~~text
data/
├── chroma_db/        벡터 (corpus별 컬렉션)
├── docs/
│   ├── inventions/   수상작 원본
│   └── {corpus}/     관리자가 올린 문서
└── app.db            corpus 정의, 관리자 계정, 색인 잡, 감사 로그
~~~

### 대량 문서 가져오기

문서는 보통 어드민 화면에서 올립니다. 다만 수천 개를 한 번에 넣을 때는
브라우저 업로드보다 이 스크립트가 편합니다.

~~~bash
python -m scripts.migrate_docs --corpus inventions --source /경로/문서모음 --dry-run
python -m scripts.migrate_docs --corpus inventions --source /경로/문서모음
~~~

`--mode symlink`를 쓰면 복사 대신 원본을 연결합니다. 컨테이너 배포에서는
볼륨 안에 실제 파일이 있어야 하므로 기본값인 copy를 권합니다.

가져온 뒤에는 색인을 해야 검색됩니다. 어드민의 **변경분 색인**을 쓰거나
아래 CLI를 실행하세요.

## Rails Credentials 연동

Rails에는 FastAPI 자체의 Gemini 키를 넣을 필요가 없습니다. Rails가 알아야 할
값은 RAG 서버 주소와 두 서버가 공유하는 호출 토큰뿐입니다.

~~~yaml
rag:
  base_url: https://rag.example.com
  api_token: 충분히-긴-무작위-값
~~~

FastAPI 배포 환경의 `RAG_API_TOKEN`과 Rails credentials의 `rag.api_token`은
같아야 합니다. `GEMINI_API_KEY`는 FastAPI 서버의 secret manager에만 저장합니다.

## 어드민

### 초기 계정 생성

첫 계정은 자동으로 **최고관리자**가 됩니다.

~~~bash
python -m admin.cli create-user boss
python -m admin.cli list-users
python -m admin.cli reset-password boss
~~~

### 관리자 계층

| 역할 | 인원 | 권한 |
|---|---|---|
| 최고관리자 | **1명** | 일반관리자 권한 전부 + 계정 관리 + 감사 로그 + corpus 완전삭제 |
| 일반관리자 | 다수 | corpus 생성·설정·공개, 문서 업로드·삭제, 재색인, 검색 테스트 |

색인 관련 작업에서 두 역할의 권한은 동등합니다. 최고관리자 1명 제약은
애플리케이션이 아니라 DB의 partial unique index가 강제합니다.

최고관리자는 자기 계정을 삭제·비활성화·강등할 수 없습니다. 권한을 넘기려면
`관리자 계정` 화면의 **이양**을 씁니다. 이양하면 본인은 일반관리자가 되고
대상이 최고관리자가 되며, 두 변경은 한 트랜잭션으로 처리됩니다.

계정을 비활성화하거나 비밀번호를 리셋하면 해당 사용자의 **기존 로그인 세션이
즉시 끊깁니다**.

### 새 corpus 만들기

1. `/admin/corpora/new` — corpus 주소, 이름, 임베딩 지시문 입력
2. corpus 상세 화면에서 `.md`/`.txt` 파일 업로드 (여러 개 또는 zip)
3. **변경분 색인** 실행 — 진행률이 화면에 표시됩니다
4. **검색 테스트**로 결과 확인
5. **공개하기** — 이때부터 검색 API에서 조회됩니다

각 단계의 상세한 설명과 문제 해결은 [ADMIN_MANUAL.md](ADMIN_MANUAL.md)에 있습니다.

### 색인 방식

| 방식 | 동작 | 언제 |
|---|---|---|
| 변경분 색인 | 활성 컬렉션에 직접 반영. `content_hash`가 같은 문서는 재임베딩을 생략 | 문서 추가·수정 후 |
| 전체 재색인 | 새 버전 컬렉션을 만든 뒤 활성 포인터를 교체 (alias 전환) | 지시문·청킹·차원 변경 후 |

전체 재색인은 **무중단**입니다. 새 컬렉션이 완성될 때까지 검색은 기존 컬렉션을
계속 사용하고, 실패하면 새 컬렉션을 버리고 기존 색인을 그대로 유지합니다.
전환 후에도 직전 버전 컬렉션을 하나 남겨 롤백 여지를 둡니다
(`KEEP_OLD_COLLECTIONS`).

설정 중 문서 지시문·청킹·임베딩 차원을 바꾸면 기존 벡터가 무의미해지므로
어드민이 재색인을 안내합니다. 임베딩 차원 변경만은 검색이 즉시 깨지므로
저장과 동시에 재색인 잡이 자동으로 걸립니다.

## 색인 CLI

색인 빌드는 Gemini API 비용이 발생합니다. 먼저 일부 문서로 검증하세요.

~~~bash
python -m ingest.build_index --corpus inventions --limit 50
python -m ingest.build_index --corpus inventions --reset
~~~

| 옵션 | 설명 |
|---|---|
| `--corpus ID` | 대상 corpus (기본: `inventions`) |
| `--reset` | 기존 컬렉션을 삭제하고 재색인 |
| `--limit N` | 처음 N개 문서만 처리 |
| `--docs-dir PATH` | corpus 문서 디렉터리 대신 사용할 경로 |

> CLI로 색인한 뒤에는 **실행 중인 서버를 재시작**해야 결과가 반영됩니다.
> 서버 프로세스가 Chroma 연결을 캐시하기 때문입니다. 어드민 화면에서 색인하면
> 같은 프로세스 안에서 도므로 재시작이 필요 없습니다.

## 서버 실행

~~~bash
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
~~~

- API 문서: http://localhost:8000/docs
- 어드민: http://localhost:8000/admin
- 생존 확인: `GET /health`
- 준비 확인: `GET /ready`

`/health`는 프로세스가 살아 있는지만 확인합니다. `/ready`는 Gemini 키, 공개
corpus별 색인 상태, 운영 토큰 설정을 확인하고 준비되지 않았으면 503을 반환합니다.

## API

### GET /v1/corpora

검색 가능한 corpus 목록입니다. 초안·비공개 corpus는 나오지 않습니다.

~~~json
{
  "corpora": [
    {
      "corpus": "inventions",
      "label": "발명대회 수상작",
      "kind": "invention",
      "corpus_id": "national-student-invention-awards-1979-2017",
      "index_version": "inventions-gemini-embedding-2-1536-v1",
      "indexed_chunks": 13402
    }
  ],
  "count": 1
}
~~~

### POST /v1/corpora/{corpus_id}/search

운영 환경에서는 Authorization 헤더가 필수입니다.

~~~bash
curl -X POST http://localhost:8000/v1/corpora/inventions/search \
  -H "Authorization: Bearer $RAG_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "공기 압력을 이용한 소형 발전 장치",
    "top_k": 5,
    "options": {"include_advisor_docs": false}
  }'
~~~

~~~json
{
  "query": "공기 압력을 이용한 소형 발전 장치",
  "corpus": "inventions",
  "results": [
    {
      "document_id": "1b4f04e97d45d5e28e6d9217",
      "title": "공기의 압력을 이용한 미니 발전기",
      "similarity": 0.876,
      "snippet": "공기 압력의 변화를 이용하여...",
      "metadata": {
        "title": "공기의 압력을 이용한 미니 발전기",
        "year": 1979,
        "category": "과학완구",
        "doc_type": "작품설명서"
      }
    }
  ],
  "count": 1,
  "corpus_id": "national-student-invention-awards-1979-2017",
  "index_version": "inventions-gemini-embedding-2-1536-v1",
  "score_kind": "rescaled_cosine_match",
  "notice": "검색 결과는 유사 자료 탐색을 돕는 참고 정보이며, 신규성·특허 가능성을 판정하지 않습니다."
}
~~~

corpus 공통 필드는 최상위에, corpus 종류별 필드는 `metadata`에 담깁니다.
`options`는 corpus 종류가 해석합니다 — 발명 corpus는 `include_advisor_docs`를
읽고, 일반 텍스트 corpus는 옵션을 쓰지 않습니다.

`similarity`는 Chroma cosine distance를 0~1 범위로 재조정한 **매칭 점수**입니다.
법적 의미의 독창성 점수가 아닙니다. 화면에서는 "유사도 87.6%"보다
"유사 자료 매칭 점수 0.876"처럼 표시하고, 학생이 차이점을 직접 기록하게 하는
것이 안전합니다.

### 하위 호환 엔드포인트

`POST /v1/search`와 `POST /search`는 발명 corpus로 위임되며 기존 응답 형태를
그대로 유지합니다. 둘 다 deprecated이고, `/search`는 저자와 내부 경로를
포함하므로 새 연동에서는 사용하지 않습니다.

### 주요 오류

| 상태 | 의미 |
|---|---|
| 401 | 서비스 토큰 누락 또는 불일치 |
| 404 | 등록되지 않았거나 공개되지 않은 corpus |
| 422 | 빈 문자열, 길이 초과 등 잘못된 요청 |
| 503 | 토큰 미구성, Gemini/색인 장애, 동시 요청 초과 |
| 500 | 예기치 않은 내부 오류 |

공급자 예외의 상세 내용은 API 응답에 노출하지 않습니다.

## 서버 배포

`bin/deploy` 하나로 빌드부터 롤백까지 처리합니다. Rails의 Kamal과 같은
자리를 차지하는 스크립트입니다.

```
로컬에서 이미지 빌드 → Docker Hub 푸시 → 서버에서 pull
  → 컨테이너 교체 → 헬스체크 → 실패하면 이전 버전으로 자동 롤백
```

앞단에는 Caddy 리버스 프록시 컨테이너가 서서 도메인 HTTPS 인증서를
자동으로 발급받고, 관리자 화면을 IP로 제한합니다.

### 준비물

- SSH로 접속할 수 있는 리눅스 서버 1대 (Docker 설치, 80·443 포트 개방)
- Docker Hub 저장소와 로컬 `docker login`
- 서버를 가리키는 도메인 A 레코드

### 최초 설정

**1. 배포 설정을 채웁니다.**

~~~bash
cp deploy/config.env.example deploy/config.env
~~~

`SSH_HOST`, `IMAGE`, `DOMAIN`, `ACME_EMAIL`, `ADMIN_ALLOW_IPS`를 채웁니다.
`ADMIN_ALLOW_IPS`에는 관리자 화면에 접속할 IP를 넣습니다. 자기 공인 IP는
`curl -s https://api.ipify.org`로 확인합니다.

서버 접속에 특정 키를 써야 하면 `SSH_KEY`에 경로를 지정합니다. 비워두면
ssh 기본 동작(`~/.ssh/id_*`, ssh-agent, `~/.ssh/config`)에 맡깁니다.
`Permission denied (publickey)`가 나면 이 값을 확인하세요.

`deploy/config.env`와 `deploy/secrets.env`는 둘 다 `.gitignore`에 있습니다.
이 저장소가 공개되어 있어 서버 주소가 히스토리에 남지 않게 한 것입니다.
`.example` 파일만 커밋됩니다.

**2. 시크릿을 만듭니다.**

~~~bash
cp deploy/secrets.env.example deploy/secrets.env
chmod 600 deploy/secrets.env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # 토큰 생성용
~~~

`GEMINI_API_KEY`, `RAG_API_TOKEN`, `SESSION_SECRET` 세 개는 반드시 채웁니다.
값에 따옴표를 쓰지 마세요. `docker --env-file`은 따옴표를 값의 일부로 취급합니다.

**3. 서버를 준비하고 첫 배포를 합니다.**

~~~bash
bin/deploy setup
~~~

디렉터리·네트워크·볼륨 생성, 시크릿 전송, Caddy 기동, 첫 배포까지 한 번에
진행합니다. 무엇을 할지 먼저 보고 싶으면 `bin/deploy --dry-run setup`을 씁니다.

**4. 최초 관리자 계정을 만듭니다.**

~~~bash
bin/deploy admin create-user <아이디>
~~~

첫 계정이 자동으로 최고관리자가 됩니다.

**5. 문서를 올리고 색인합니다.** `https://<도메인>/admin`에 로그인해 문서를
업로드하고 색인한 뒤 corpus를 공개합니다. 공개된 corpus가 하나도 없으면
`/ready`는 503을 반환합니다 — 이것은 고장이 아니라 정상 상태입니다.

대량 문서는 어드민 업로드 대신 컨테이너 안에서 넣습니다.

~~~bash
bin/deploy exec python -m scripts.migrate_docs --corpus inventions --source /mnt/source
~~~

### 일상 운영

| 명령 | 하는 일 |
|---|---|
| `bin/deploy` | 빌드 → 푸시 → 교체 → 헬스체크 |
| `bin/deploy status` | 컨테이너 상태, 현재 태그, `/health`·`/ready`, 인증서 만료일 |
| `bin/deploy logs -f` | 앱 로그 따라가기 |
| `bin/deploy rollback` | 직전 릴리스로 되돌리기 |
| `bin/deploy releases` | 배포 이력과 남아 있는 이미지 |
| `bin/deploy admin list-users` | 관리자 계정 목록 |
| `bin/deploy console` | 컨테이너 안에서 bash 열기 |
| `bin/deploy secrets push` | 시크릿을 바꾼 뒤 서버에 반영 |
| `bin/deploy proxy reload` | Caddy 설정 변경 반영 (무중단) |
| `bin/deploy backup` | `/data` 볼륨 백업 |
| `bin/deploy help` | 전체 명령과 옵션 |

배포는 기본적으로 커밋되지 않은 변경이 있으면 멈추고, `pytest`를 먼저
돌립니다. 급할 때는 `--allow-dirty`, `--skip-tests`로 건너뜁니다.

### 배포할 때 실제로 무슨 일이 일어나는가

**5~20초 동안 앱 컨테이너가 내려갑니다.** 진짜 무중단 배포가 아닙니다.

Chroma와 `app.db`가 모두 SQLite 파일이고 `/data` 볼륨 하나를 공유하기
때문에, 신·구 컨테이너를 잠시라도 겹쳐 띄우면 두 프로세스가 같은 파일에
쓰게 되어 데이터가 깨질 수 있습니다. 그래서 구 컨테이너를 완전히 세운 뒤
새 컨테이너를 띄웁니다.

대신 Caddy가 그동안 들어온 요청을 최대 `LB_TRY_DURATION`(기본 60초) 동안
붙들고 재시도합니다. 사용자에게는 502가 아니라 "조금 느린 요청 하나"로
보입니다. 이것이 체감 무중단을 만드는 유일한 장치이므로, **Rails 쪽 호출
타임아웃을 30초 이상**으로 잡아야 의미가 있습니다. 그보다 짧으면 Caddy가
붙들고 있는 동안 Rails가 먼저 끊습니다.

**색인 잡이 도는 동안에는 배포하지 마세요.** 재시작하면 진행 중이던 잡이
"서버가 재시작되어 중단되었습니다"로 실패 처리됩니다. 스크립트가 이를
검사해 배포를 막습니다. 그래도 진행하려면 `--force`를 씁니다.

**헬스체크는 `/health`로 판정합니다.** `/ready`는 아직 공개된 corpus가
없을 때도 503을 반환하므로 배포 성공/실패의 기준이 될 수 없습니다.
`/ready`의 문제 목록에 키 누락이나 색인 접근 실패처럼 진짜 고장이 섞여
있으면 그때는 실패로 처리합니다. 색인까지 끝난 뒤 `/ready` 200을 강제하고
싶으면 `--require-ready`를 붙입니다.

헬스체크가 시간 안에 통과하지 못하면 새 컨테이너의 로그를 보여준 뒤
이전 태그로 자동 롤백합니다.

### 어드민 접근 제한

`/admin`, `/ready`, `/docs`, `/openapi.json`은 `ADMIN_ALLOW_IPS`에 있는
주소에서만 열립니다. 그 밖에서는 404를 돌려줘 존재 자체를 숨깁니다.
앱은 클라이언트 IP를 전혀 보지 않으므로 이 프록시 규칙이 유일한 방어선입니다.

`ADMIN_ALLOW_IPS`가 비어 있으면 이 경로들이 전면 차단됩니다. IP를 바꾸면
`bin/deploy proxy reload`로 반영합니다. 접속 IP가 자주 바뀐다면 IP 목록
대신 VPN이나 Tailscale을 앞에 두는 편이 낫습니다.

### 백업과 복원

백업 대상은 `/data` 볼륨 하나입니다. 벡터, 문서, 관리자 계정, 감사 로그가
모두 여기 들어 있습니다.

~~~bash
bin/deploy backup                  # 앱을 잠시 세우고 일관된 스냅샷을 뜬다
bin/deploy backup --hot            # 세우지 않고 뜬다 (일관성 보장 안 됨)
bin/deploy backup list
bin/deploy backup pull <이름>      # 로컬로 사본을 내려받는다
bin/deploy restore <이름>          # 되돌린다. 되돌리기 전 자동으로 한 번 더 백업한다
~~~

기본 백업이 앱을 잠시 세우는 이유는 `app.db`와 `chroma.sqlite3`가 WAL
모드라, 돌아가는 중에 tar로 뜨면 복원했을 때의 일관성을 보장할 수 없기
때문입니다. 정기적으로 복원 리허설을 해 두는 것을 권합니다.

Caddy 인증서는 별도 볼륨(`ip-rag-caddy-data`)에 있습니다. 이 볼륨을 지우면
Let's Encrypt 재발급 한도에 걸릴 수 있으니 건드리지 마세요.

### 인증서

Caddy가 Let's Encrypt에서 인증서를 자동으로 받고, 수명의 2/3 지점 —
90일 인증서면 만료 30일 전쯤 — 에 스스로 갱신합니다. 재시작할 필요도,
사람이 손댈 일도 없습니다. 배포는 앱 컨테이너만 교체하므로 갱신과
무관합니다.

갱신이 계속되려면 네 가지가 유지돼야 합니다.

- `ip-rag-caddy-data` 볼륨 — ACME 계정 키와 인증서가 여기 들어 있습니다.
  지우면 처음부터 재발급이라 Let's Encrypt 발급 한도에 걸릴 수 있습니다
- 프록시 컨테이너가 떠 있을 것 (`--restart unless-stopped`이라 재부팅에도 복귀)
- 80·443 포트 개방 — 갱신 때도 챌린지를 받아야 합니다
- DNS A 레코드가 계속 서버를 가리킬 것

`bin/deploy status`가 현재 인증서의 만료일을 보여줍니다. 남은 기간이 3주
밑으로 내려가면 경고합니다 — Caddy가 정상이라면 그 전에 갱신되므로,
경고가 뜬다면 갱신이 실패하고 있다는 뜻입니다. `bin/deploy proxy logs`로
원인을 확인합니다.

### 장애 대응

~~~bash
bin/deploy status                  # 어디가 문제인지 먼저 본다
bin/deploy logs -n 200             # 앱 로그
bin/deploy proxy logs              # 인증서 발급 실패 등 프록시 문제
bin/deploy rollback                # 직전 버전으로
bin/deploy rollback <태그>         # 특정 버전으로 (releases 로 확인)
bin/deploy unlock                  # 죽은 배포가 락을 남겼을 때
~~~

## 로컬 Docker 실행 (참고)

배포 스크립트 없이 손으로 띄울 때의 절차입니다.

~~~bash
docker build -t ip-rag .
docker volume create ip-rag-data

docker run --rm -it -v ip-rag-data:/data ip-rag \
  python -m admin.cli create-user boss

docker run --rm -p 8000:8000 \
  -e APP_ENV=production \
  -e GEMINI_API_KEY=... \
  -e RAG_API_TOKEN=... \
  -e SESSION_SECRET=... \
  -v ip-rag-data:/data \
  ip-rag
~~~

**단일 컨테이너·단일 Uvicorn worker**로 운영합니다. Chroma는 SQLite 기반이라
다중 프로세스 쓰기가 위험하고, 색인 잡 러너도 이 프로세스 안의 단일 워커
스레드로 돕니다. 처리량은 워커 수가 아니라 `SEARCH_CONCURRENCY`로 조정합니다.
그 이상의 규모가 필요해지면 관리형 벡터 DB나 별도 Chroma 서버 전환을
검토합니다.

리버스 프록시 뒤에 둘 때는 `FORWARDED_ALLOW_IPS`를 설정해야 합니다.
uvicorn의 `--proxy-headers`는 기본적으로 `127.0.0.1`에서 온 요청의
`X-Forwarded-*`만 신뢰하므로, 프록시가 다른 컨테이너에 있으면 헤더가
무시됩니다. 세션 쿠키는 production에서 `Secure` 플래그가 붙으므로
HTTPS가 필요합니다.

## 테스트

~~~bash
python -m pytest -q
~~~

단위·통합·보안·운영 회귀 테스트가 실제 Gemini API를 호출하지 않고 돕니다.
GitHub Actions도 같은 명령을 실행합니다.

## 아직 어려운 부분

- 실제 전체 색인 비용·시간 측정
- 교사가 검토한 정답셋으로 검색 정확도와 임계값 평가
- 작품명·부품명처럼 정확한 단어를 보완하는 키워드/하이브리드 검색
  (규정·복지처럼 정확한 용어 매칭이 중요한 corpus에서 더 필요합니다)
- 특허 문헌 수집·갱신·출처 링크·법적 고지
- 공개 corpus의 저작권·개인정보 검토

세부 후속 작업은 [TODO.md](TODO.md)를 참고하세요.
