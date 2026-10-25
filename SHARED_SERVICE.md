# Co-AI · EasyFlow 공동 RAG 운영

기존 `inventions`와 신규 `easyflow`를 같은 엔진에서 운영합니다. Co-AI의 검색 URL·문서 ID·응답 형식은 유지하며, 서비스마다 허용 corpus와 작업을 제한합니다. **두 앱의 문서는 혼용하지 않습니다.** EasyFlow 자료는 관리자가 EasyFlow에서 업로드하고 승인 후 API로만 전달합니다. EasyFlow 종류와 다른 서비스 사이의 잘못된 grant는 등록과 실행 양쪽에서 거절합니다.

## 첫 설정

아래 명령은 **배포한 RAG 서버의 프로젝트 폴더**에서 실행합니다. 실제 비밀값을 인자나 문서에 적지 않습니다.

```bash
python -m scripts.services bootstrap-easyflow
python -m scripts.services grant --service coai --token-id coai-read-v1 --corpus inventions --read
python -m scripts.services grant --service easyflow --token-id easyflow-read-v1 --corpus easyflow --read
python -m scripts.services grant --service easyflow --token-id easyflow-write-v1 --corpus easyflow --write
python -m scripts.services list
```

`grant`는 토큰을 숨김 입력으로 받습니다. 32자 이상의 충분히 긴 무작위 값을 사용하고, 같은 값을 각 Rails 앱의 암호화 credentials에 입력합니다. 이 서버는 검증용 SHA-256 해시만 저장합니다. 자동 전달에는 `--token-stdin`을 사용할 수 있습니다. 토큰 원문이 출력되는 명령이나 로그를 남기지 마세요.

| 앱 | credentials | 입력 값 |
| --- | --- | --- |
| Co-AI | `rag.base_url` | 이 서버의 기본 URL |
| Co-AI | `rag.api_token` | `coai-read-v1`에 등록한 토큰 |
| EasyFlow | `rag.base_url` | 이 서버의 기본 URL |
| EasyFlow | `rag.corpus_id` | **`easyflow`** (응답의 긴 `corpus_id`가 아닌 URL slug) |
| EasyFlow | `rag.api_token` | `easyflow-read-v1` 토큰 |
| EasyFlow | `rag.indexing_api_token` | `easyflow-write-v1` 토큰 |

`bootstrap-easyflow`는 `kind=easyflow` 자료집을 **초안**으로 생성합니다. 첫 승인 자료의 색인이 성공한 뒤 RAG 관리자 화면에서 공개하세요. 기존 자료집을 덮어쓰거나 다른 종류를 자동으로 바꾸지 않습니다. 공개 상태와 서비스 접근 권한은 별도로 검사합니다.

기존 `RAG_API_TOKEN`은 이전 기간에만 유지합니다. 이 토큰은 `RAG_LEGACY_CORPUS`(기본 `inventions`) 읽기에만 접근합니다. 기존 운영 corpus 재정의와 다른 호출자를 먼저 확인하세요. Co-AI 전환을 확인한 뒤 환경에서 구 토큰을 제거합니다. `scripts.services revoke TOKEN_ID`는 DB에 등록한 토큰을 비활성화하며 구 환경변수를 제거하지는 않습니다.

## 관리자 파일 업로드

상단 **문서 업로드**(`/admin/upload`)에서 대상 corpus를 필수 선택합니다. `inventions`를 선택하면 발명 자료를 그 자료집에 저장하며, **업로드 후 변경분 색인**을 체크하면 같은 대상의 색인 작업도 생성합니다. 자료집 상세 화면에서도 대상을 선택할 수 있습니다. 없는 corpus는 다른 대상으로 대체하지 않습니다. EasyFlow 종류는 선택지에서 제외하고 직접 요청도 차단합니다. EasyFlow 자료를 수정하려면 EasyFlow의 RAG 데이터 관리 화면을 사용하세요.

## API 계약

모든 `/v1/corpora` 목록·검색·corpus 준비 상태와 서비스 작업 API는 인증이 필요합니다. 검색 토큰은 문서를 변경할 수 없고, 다른 서비스의 corpus·작업은 404로 숨깁니다. 관리자 세션은 서비스 토큰을 대신하지 않습니다.

- `POST /v1/corpora/{corpus}/search`: 기존 성공 응답 유지. EasyFlow 결과의 `metadata`에 `source_id`, `source_version_id`, 실제 `page`, `anchor` 포함.
- `GET /v1/corpora/{corpus}/ready`: 해당 서비스가 이용 가능한 corpus만 점검.
- `PUT /v1/corpora/{corpus}/sources/{uuid}`: 승인 문서 버전의 페이지별 텍스트 접수. `202 + job_id`는 접수이며 완료가 아닙니다.
- `PATCH /v1/corpora/{corpus}/sources/{uuid}`: 검색 중지 또는 유효한 기존 버전 재활성화.
- `DELETE /v1/corpora/{corpus}/sources/{uuid}`: 검색 즉시 제외 후 파일·청크 정리.
- `GET /v1/jobs/{job_id}`: 같은 서비스의 쓰기 권한으로 상태 확인.

쓰기 요청은 증가하는 정수 `revision`과 재시도해도 변하지 않는 `idempotency_key`를 포함합니다. 같은 키의 다른 내용과 오래된 revision은 409입니다. 같은 문서 버전 ID에 다른 원문을 덮어쓸 수 없습니다.

등록 본문:

```json
{
  "revision": 1,
  "idempotency_key": "local-job-uuid",
  "source_version_id": "019a0000-0000-7000-8000-000000000002",
  "title": "학교회계 지침",
  "pages": [{"page": 3, "text": "승인된 페이지의 실제 텍스트"}]
}
```

중지·삭제 본문은 `revision`, `idempotency_key`, `enabled: false`입니다. 상태는 `queued`, `running`, `succeeded`, `failed`, `superseded`이며, `superseded`는 더 최신 요청이 있어 이전 작업을 활성화하지 않았다는 뜻입니다. 실패 응답은 원문 대신 오류 코드만 제공합니다.

본문은 전체 요청 6MiB, 문서 텍스트 합계 5MiB, 최대 2,000쪽으로 제한합니다. 작업 대기는 서비스당 최대 100건입니다. 완료 전 문서 상태는 SQLite에 영속화하고 작업 실행은 5분 임대와 배치별 갱신으로 선점합니다. 중지·삭제 후 옛 색인이 완료돼도 활성 버전으로 전환하지 않습니다.

## 호환성과 운영

- `/health`는 생존, `/ready`는 공통 의존성만 점검합니다. 익명 응답에 corpus 목록·문서 수를 싣지 않습니다. 자료집별 준비 상태는 인증된 경로를 사용합니다.
- 기본 서비스당 요청 한도는 `grant --rpm`으로 설정하며 기본 120회/분입니다. `SEARCH_SERVICE_CONCURRENCY` 기본값은 프로세스당 5, 전역 `SEARCH_CONCURRENCY`는 기존 설정을 유지합니다. 이 값은 성능 보장이 아니며 두 서비스 동시 부하 시험으로 조정하세요.
- 429는 `Retry-After`가 있는 할당량/대기 제한입니다. 503의 `error.code=search_busy`와 `dependency_unavailable`을 구분합니다. 새 코드를 처리하는 Co-AI/EasyFlow 버전을 배포한 뒤 트래픽을 전환하세요.
- Co-AI는 `inventions`, `include_advisor_docs: false`, 기존 `(corpus_id,index_version,document_id)` 근거 키와 과거 snapshot을 유지합니다.
- EasyFlow kind의 페이지·버전 정보는 서비스 원장과 함께 관리됩니다. 관리자 화면의 일반 파일 업로드·삭제·이름 복구로 승인 절차를 우회하지 않도록 해당 변경을 막았습니다. 승인 자료의 변경은 EasyFlow에서 수행하세요. 운영 관리자의 재색인·공개 기능은 유지합니다.
- EasyFlow 전체 재색인은 활성 버전의 영속 payload에서 원래 출처·페이지·버전을 재현합니다. 재색인과 서비스 버전 전환은 같은 corpus 잠금으로 조정합니다. SQLite 원장과 벡터·원본을 함께 백업하세요.
- Docker의 `--workers 1` 전제를 유지합니다. 다중 API 프로세스나 별도 색인 워커 확장은 별도의 실행·Chroma 쓰기 조정이 필요합니다.
- 일반 관리자들이 서로 다른 서비스를 맡는 경우의 관리자 corpus별 권한 분리는 추가 작업입니다. 현재 관리자 계정은 기존의 공통 운영 팀 권한을 유지합니다.
- 원본과 출처 payload는 서버의 `DATA_DIR`에 보관됩니다. 백업·보존·완전 삭제 정책을 함께 운영해야 합니다. 작업 및 버전 원장 보존 기간은 이 구현에서 자동 삭제하지 않습니다.
- 내부 자료를 넣은 뒤 공용 토큰에 모든 corpus가 열리던 과거 서버로 단순 복귀하지 마세요. 권한 패치를 유지하거나 EasyFlow corpus를 비공개로 내린 뒤 복구합니다.
