# TODO — post-MVP 작업 목록

MVP는 완성·검증 완료(테스트 82개 통과, code-review APPROVE). 아래는 **실제 색인 이후 품질·견고성·운영을 높이기 위한 후속 작업**이다.
출처: code-reviewer 검증 findings + `README.md` 향후 작업 + 계획서(`.omc/plans/rag-invention-similarity-plan.md`) AC2b.

범례 — 우선순위: 🔴 높음 / 🟡 중간 / 🟢 낮음 · **선행조건**: 실색인 = 실제 `build_index` 실행 후라야 의미 있음.

---

## 0. 운영 선행 작업 (가장 먼저)

- [ ] **`.env`에 `GEMINI_API_KEY` 설정** (`cp .env.example .env` 후 키 입력)
- [ ] **실제 색인 실행**: `python -m ingest.build_index` — ⚠️ Gemini API 호출 = **비용 발생**. 먼저 `--limit 50`으로 소규모 검증 후 전체 실행 권장.
- [ ] **색인 비용 산정**: 12,630문서 × 평균 청크 수 = 예상 API 호출 수/비용 추산 (Batch API 50% 가격 반영). 빌더가 호출 수를 로깅함.

---

## 1. 검색 품질 🔴 (실색인 + 평가셋 필요)

- [ ] **(a) 집계 산식 튜닝 — AC2b 게이트** 🔴 *(선행조건: 실색인)*
  - 현재: `작품점수 = max(청크 유사도) - LENGTH_NORM_C·log(청크수)`, `LENGTH_NORM_C=0.0`(순수 max). → 파일: [ingest/search.py](ingest/search.py), [config.py](config.py)
  - 할 일: **사람 라벨 유사 작품 쌍 20~30개**(라벨러·유사성 정의·쌍 선정 프로토콜 문서화) 구축 → `max` vs `length-normalized max` vs `평균` A/B 비교 → 최고 방식과 `LENGTH_NORM_C` 값 확정.
  - 이유: 순수 max는 청크 많은 긴 문서에 편향 가능 / 평균은 긴 문서 희석. 데이터로만 판정 가능.
- [ ] **(b) over-fetch 충분성 점검** 🟡 *(선행조건: 실색인)*
  - 현재: 청크를 `top_k×5` 조회 후 작품 단위 dedup, 부족 시 확보분만 반환(의도된 동작). → 파일: [ingest/search.py](ingest/search.py), `OVERFETCH_MULTIPLIER` in [config.py](config.py)
  - 할 일: 실제 질의로 부족분 발생률 측정 → 잦으면 `OVERFETCH_MULTIPLIER` 상향 또는 N개 채울 때까지 반복 조회.
- [ ] **(f) 토큰비 실측** 🟡 *(선행조건: 실색인 전 표본 측정)*
  - 현재: 청킹 분기를 문자수(5,500자) 휴리스틱 + 1.2자/토큰 추정으로 처리. 트렁케이션 가드는 이미 코드화됨. → 파일: [ingest/build_index.py](ingest/build_index.py), [ingest/chunker.py](ingest/chunker.py)
  - 할 일: Gemini 토크나이저로 100개 표본 tokens/char 실측 → 임계값 보정.
- [ ] **지도논문 중복도 측정** 🟢 *(선행조건: 실색인)*
  - 색인 후 작품설명서 ↔ 지도논문 실제 중복률 분석(필터의 근거였던 "준중복" 가정 검증). → 파일: [ingest/parse.py](ingest/parse.py)(doc_type), [ingest/search.py](ingest/search.py)(필터)

---

## 2. 코드 견고성 🟢 (지금 바로 가능, 색인 불필요)

- [ ] **(d) `year=-1` 센티넬 → `null` 매핑** 🟢 *(~10분)*
  - 파싱 실패 시 연도가 `-1`로 응답에 노출(Chroma가 `None` 미저장). 응답 단계에서 `-1`→`null`로 매핑. → 파일: [ingest/search.py](ingest/search.py), [api/schemas.py](api/schemas.py)
- [ ] **(c) 비표준 `--docs-dir` 시 `source_path` 절대경로화** 🟢 *(~15분)*
  - 프로젝트 밖 docs 경로 사용 시 `source_path`가 절대경로가 돼 ID 이식성/경로 노출 문제. 스캔 디렉터리 기준 상대경로로 도출하거나 제약 문서화. → 파일: [ingest/parse.py](ingest/parse.py)
- [ ] **(e) 임베딩 백오프를 transient 오류로 한정** 🟢 *(~15분)*
  - 현재 모든 예외에 5회 재시도 → 인증(401) 등 영구 오류도 ~31초 낭비. rate-limit/5xx/연결오류로만 재시도, 4xx는 즉시 실패. → 파일: [ingest/embedder.py](ingest/embedder.py)

---

## 3. 운영 / 배포 🟡

- [ ] **임베딩 차원 최적화**: 1536 vs 3072 비교(정확도 ↔ 저장·검색 성능). → `EMBED_DIM` in [config.py](config.py) *(차원 변경 시 재색인 필요)*
- [ ] **Docker 패키징**: 프로덕션 배포용 컨테이너(`Dockerfile`, 환경변수 주입, chroma_db 볼륨).
- [ ] **검색 운영 보강**(선택): 요청 타임아웃·백프레셔 임계값 튜닝(현재 semaphore 10), 로깅/모니터링.

---

## 진행 순서 추천

1. **0번 운영 선행** — 키 설정 + 실색인(소규모→전체) + 비용 산정.
2. **2번 코드 견고성** (d)(c)(e) — 색인과 무관하게 즉시 적용 가능, 각 10~20분.
3. **1번 검색 품질** (a)→(b)→(f) — 실색인 + 평가셋 구축 후 측정으로 확정.
4. **3번 운영/배포** — 차원 최적화·Docker·모니터링.
