"""관리자 계정 CLI — 최초 계정을 만들 때 쓴다.

    python -m admin.cli create-user <아이디>
    python -m admin.cli list-users

첫 계정은 자동으로 최고관리자가 된다. 이후 계정 추가와 역할 이양은 어드민 화면에서 한다.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from admin.auth import (
    AuthError,
    create_user,
    get_by_username,
    has_superadmin,
    list_users,
    set_password,
)


def _prompt_password() -> str:
    first = getpass.getpass("비밀번호: ")
    second = getpass.getpass("비밀번호 확인: ")
    if first != second:
        print("비밀번호가 서로 다릅니다.", file=sys.stderr)
        raise SystemExit(1)
    return first


def cmd_create_user(args: argparse.Namespace) -> int:
    will_be_super = not has_superadmin()
    password = args.password or _prompt_password()

    try:
        user = create_user(args.username, password, created_by="cli")
    except AuthError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    role = "최고관리자" if user.is_superadmin else "일반관리자"
    print(f"'{user.username}' 계정을 {role}로 만들었습니다.")
    if will_be_super:
        print("첫 계정이므로 최고관리자 권한이 부여되었습니다.")
    return 0


def cmd_reset_password(args: argparse.Namespace) -> int:
    user = get_by_username(args.username.strip().lower())
    if user is None:
        print(f"오류: 계정을 찾을 수 없습니다 — {args.username}", file=sys.stderr)
        return 1

    password = args.password or _prompt_password()
    try:
        set_password(user, password)
    except AuthError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    print(f"'{user.username}'의 비밀번호를 변경했습니다. 기존 세션은 모두 끊겼습니다.")
    return 0


def cmd_list_users(args: argparse.Namespace) -> int:
    users = list_users()
    if not users:
        print("등록된 관리자가 없습니다. create-user 로 첫 계정을 만드세요.")
        return 0

    print(f"{'아이디':<20} {'역할':<12} {'상태':<8} 마지막 로그인")
    for user in users:
        role = "최고관리자" if user.is_superadmin else "일반관리자"
        state = "활성" if user.is_active else "비활성"
        print(f"{user.username:<20} {role:<12} {state:<8} {user.last_login_at or '-'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m admin.cli",
        description="어드민 계정을 관리한다.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-user", help="관리자 계정을 만든다.")
    create.add_argument("username", help="아이디 (영문·숫자·하이픈·밑줄 3~32자)")
    create.add_argument(
        "--password",
        default=None,
        help="비밀번호. 생략하면 입력을 받는다 (셸 히스토리에 남지 않으므로 권장).",
    )
    create.set_defaults(func=cmd_create_user)

    reset = sub.add_parser("reset-password", help="비밀번호를 바꾼다.")
    reset.add_argument("username")
    reset.add_argument("--password", default=None)
    reset.set_defaults(func=cmd_reset_password)

    listing = sub.add_parser("list-users", help="관리자 목록을 보여준다.")
    listing.set_defaults(func=cmd_list_users)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
