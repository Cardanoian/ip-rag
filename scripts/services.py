"""공용 RAG 초기 설정. 토큰은 명령 인자가 아닌 숨김 입력이나 표준입력으로 받는다."""
import argparse
import getpass
import json
import sys

import corpora
import storage
from api.services import register


def main():
    parser = argparse.ArgumentParser(description="서비스별 corpus 권한과 EasyFlow 자료집 설정")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap-easyflow", help="easyflow corpus를 초안으로 생성한다 (기존 자료집은 보존)")
    grant = sub.add_parser("grant")
    grant.add_argument("--service", required=True)
    grant.add_argument("--token-id", required=True)
    grant.add_argument("--corpus", required=True)
    grant.add_argument("--write", action="store_true")
    grant.add_argument("--read", action="store_true")
    grant.add_argument("--rpm", type=int, default=120)
    grant.add_argument("--token-stdin", action="store_true")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("token_id")
    sub.add_parser("list", help="비밀값 없이 서비스·토큰 ID·권한을 표시")
    args = parser.parse_args()
    corpora.ensure_seed()
    if args.command == "bootstrap-easyflow":
        try:
            cfg = corpora.get("easyflow")
            if cfg.kind != "easyflow":
                parser.error("기존 easyflow 자료집의 종류가 다릅니다. 자동 변경하지 않습니다.")
        except corpora.CorpusNotFound:
            cfg = corpora.create(corpus_slug="easyflow", label="EasyFlow 승인 업무 자료", kind="easyflow",
                corpus_id="easyflow-approved-documents", doc_prefix="[승인된 학교 업무 자료]", query_prefix="[학교 업무 검색]",
                embed_dim=1536, chunk_size=1000, chunk_overlap=150, single_chunk_char_hint=5500, created_by="service-bootstrap")
        print(f"corpus={cfg.id} kind={cfg.kind} status={cfg.status}")
        print("첫 승인 자료를 색인한 뒤 관리 화면에서 공개하세요. EasyFlow rag.corpus_id는 easyflow입니다.")
    elif args.command == "grant":
        operations = (["search"] if args.read else []) + (["write"] if args.write else [])
        if not operations:
            parser.error("--read 또는 --write가 필요합니다")
        corpora.get(args.corpus)
        token = sys.stdin.read().strip() if args.token_stdin else getpass.getpass("credentials에 넣을 토큰 (화면에 표시하지 않음): ")
        register(args.service, args.token_id, token, {args.corpus: operations}, args.rpm)
        print(f"서비스 {args.service}의 토큰 ID {args.token_id} 권한 등록 완료 (비밀값 미출력)")
    elif args.command == "revoke":
        with storage.transaction() as cur:
            cur.execute("UPDATE service_tokens SET active=0 WHERE token_id=?", (args.token_id,))
        print("토큰 비활성화 완료")
    else:
        with storage.cursor() as cur:
            cur.execute("SELECT token_id, service_id, grants_json, active FROM service_tokens ORDER BY service_id, token_id")
            for row in cur.fetchall():
                print(json.dumps(dict(row), ensure_ascii=False))


if __name__ == "__main__":
    main()
