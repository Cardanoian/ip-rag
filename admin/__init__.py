"""어드민 관리 화면.

- `auth`        : 계정 저장소와 scrypt 해시
- `permissions` : 세션 인증, 역할 게이트, CSRF
- `jobs`        : 단일 워커 스레드 색인 잡 러너
- `documents`   : 업로드·삭제 파일 처리
- `routes`      : FastAPI 라우터
- `cli`         : 초기 계정 생성 (`python -m admin.cli`)
"""
