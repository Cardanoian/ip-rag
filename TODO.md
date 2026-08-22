# TODO

## 완료

### Co-AI 연동
- [x] /v1/search 버전 API와 하위 호환 /search 분리
- [x] Rails ↔ RAG Bearer 서비스 토큰 인증
- [x] 운영 환경 토큰 미설정 차단
- [x] 동기 Gemini/Chroma 호출을 FastAPI 이벤트 루프 밖에서 실행
- [x] /health와 /ready 분리
- [x] 공급자 오류 상세를 API 응답에서 제거
- [x] v1 응답에서 저자명·내부 파일 경로 제거
- [x] year=-1을 null로 변환
- [x] 비표준 --docs-dir의 절대 경로 노출 방지
- [x] 임베딩 재시도를 rate-limit·5xx·연결·시간초과로 제한
- [x] .env 자동 로딩과 운영 환경변수 문서화
- [x] Dockerfile과 GitHub Actions 테스트 추가

### 멀티 corpus
- [x] corpus별 Chroma 컬렉션 분리
- [x] corpus 정의를 SQLite로 이관 (동작은 코드의 kind가 소유)
- [x] `POST /v1/corpora/{corpus_id}/search` 경로 분리, 기존 경로 하위 호환 유지
- [x] 임베딩 지시문·청킹·차원을 corpus별 설정으로 전환
- [x] 공개 메타데이터를 kind가 선언 (발명 corpus는 저자 제외)
- [x] 새 컬렉션을 완전히 만든 뒤 alias를 전환하는 무중단 색인 배포
- [x] 색인 빌드 실패 시 기존 컬렉션을 보존하는 원자적 교체
- [x] corpus에서 삭제된 문서의 고아 청크 정리
- [x] draft/published/unpublished 상태로 미완성 corpus 격리

### 어드민
- [x] 세션 로그인, CSRF, scrypt 비밀번호 해시
- [x] 최고관리자 1명 + 일반관리자 계층 (DB partial unique index로 강제)
- [x] 최고관리자 이양과 락아웃 방지
- [x] corpus 생성·설정·공개·완전삭제
- [x] 문서 업로드(zip 포함)·삭제, path traversal·zip slip 방어
- [x] 단일 워커 스레드 색인 잡 러너와 진행률 표시
- [x] 재시작으로 끊긴 잡 정리
- [x] 검색 테스트 콘솔 (공개 전 corpus 포함)
- [x] 감사 로그

## 배포 전 필수

- [ ] **원본 corpus의 재배포 권리와 개인정보 검토**
  - 파일명·본문에 과거 참가자 이름이 존재할 수 있음
  - 필요하면 가명화본만 운영 색인에 사용
  - 저장소에서 `docs/` 를 제거했지만 **git 히스토리에는 남아 있다.** 과거
    커밋에서 여전히 접근 가능하므로, 완전 제거가 필요하면 히스토리 재작성
    (`git filter-repo`) 과 LFS 객체 정리가 별도로 필요하다
- [ ] Gemini API 키, 서비스 토큰, `SESSION_SECRET`을 실제 secret manager에 등록
- [ ] 어드민에서 문서를 올리고 색인 — 먼저 소량으로 비용·속도·검색 결과 확인
- [ ] 전체 색인을 빌드하고 릴리스별 index_version 확인
- [ ] 20~30개 이상의 교사 라벨 평가 질의로 top-k 정확도 측정
- [x] 배포 자동화 (`bin/deploy`) — 빌드·푸시·교체·헬스체크·자동 롤백,
      Caddy 리버스 프록시로 자동 HTTPS와 `/admin` IP 제한. 로컬에서 이미지
      build/run, 교체 중 무중단, 백업·복원까지 검증했다
- [x] 실제 서버(rag.gbeai.net) 첫 배포 완료 — Let's Encrypt 인증서 발급,
      허용 IP 밖에서 `/admin`·`/ready`·`/docs` 404, 교체 중 80개 요청이
      전부 200(최대 2.3초 지연, 502 없음)까지 확인했다
- [x] `ADMIN_ALLOW_IPS` 설정. 접속 IP 가 바뀌면 값을 고치고
      `bin/deploy proxy reload`
- [ ] `bin/deploy rollback` 실서버 확인 — 첫 배포라 돌아갈 릴리스가 없다.
      두 번째 배포 뒤에 한 번 시험한다
- [ ] 최초 관리자 계정 생성: `bin/deploy admin create-user <아이디>`
- [ ] Rails timeout, 재시도, circuit breaker와 검색 이력 저장 구현
  - 배포 중 Caddy 가 최대 `LB_TRY_DURATION`(기본 60초) 동안 요청을 붙드므로
    Rails 쪽 호출 타임아웃은 30초 이상이어야 한다

## 검색 품질

- [ ] 교사 라벨 평가셋으로 max, length-normalized max, 평균 집계 비교
- [ ] OVERFETCH_MULTIPLIER 부족 발생률 측정
- [ ] 제목·부품명·정확한 용어를 위한 BM25/FTS 하이브리드 검색 검토
  - 규정·복지처럼 "제17조", "위(Wee)클래스" 같은 정확한 용어 매칭이 중요한
    corpus에서 발명 corpus보다 수요가 크다. corpus별 검색 전략을 둘 수 있게
    설계해 두었으므로 필요한 corpus부터 적용 가능
- [ ] 유사도 임계값을 임의 지정하지 말고 precision/recall로 결정
- [ ] 지도논문과 작품설명서 중복률 측정
- [ ] 1536차원과 3072차원의 품질·저장공간·지연 비교
- [ ] 100개 표본으로 한국어 문자/토큰 비율 실측

## 어드민 후속

- [ ] 문서 목록 페이지네이션 (문서가 수천 개인 corpus에서 필요)
- [ ] 색인 실패 문서 목록을 잡 결과에 표시 (현재는 건수만)
- [ ] 색인 잡 취소 기능
- [ ] 이전 컬렉션 버전으로 되돌리는 롤백 버튼 (현재는 수동)
- [ ] corpus별 검색 로그와 결과 0건 질의 확인

## 특허 자료 — 별도 corpus

- [ ] KIPRIS 또는 허용된 원천의 이용조건·호출 제한 확인
- [ ] 공개번호, 출원번호, 등록번호, 제목, 초록, 청구항, IPC, 날짜 정규화
- [ ] 공개번호 기준 중복 제거와 갱신 작업 설계
- [ ] 특허용 corpus kind 추가 (서지정보 파서 필요 — plain으로는 부족)
- [ ] 수상작과 특허 결과를 Rails에서 출처별로 묶어 표시
- [ ] 원문 링크와 조회일을 저장해 학생이 근거를 확인할 수 있게 함
- [ ] "유사 특허 검색"과 "특허 가능성 판정"을 화면·문구에서 분리

## 운영 견고성

- [ ] 요청 수, p95 지연, 401/429/5xx, Gemini 오류, 검색 결과 0건 모니터링
- [ ] Gemini 장애 시 Rails에서 검색 지연 안내와 수동 검색 체크리스트 제공
- [ ] `/data` 볼륨 정기 백업·복원 시험 (`bin/deploy backup` / `restore`).
      명령은 만들었으나 실서버 복원 리허설은 아직이다
- [ ] 실제 부하 시험 후 SEARCH_CONCURRENCY와 서버 사양 조정
- [ ] 색인 잡이 도는 동안의 검색 지연 측정 (같은 프로세스에서 실행되므로)
