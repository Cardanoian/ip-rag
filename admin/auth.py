"""관리자 계정 저장소와 비밀번호 해시.

해시는 표준 라이브러리 `hashlib.scrypt`를 쓴다. passlib/bcrypt 의존성 없이
메모리 하드 KDF를 얻을 수 있다.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import storage

logger = logging.getLogger(__name__)

ROLE_SUPERADMIN = "superadmin"
ROLE_ADMIN = "admin"

# scrypt 파라미터 — 로그인은 드문 작업이라 넉넉하게 잡는다.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 64

MIN_PASSWORD_LENGTH = 10

# 로그인 실패 제한 (프로세스 메모리. 단일 워커 전제)
_MAX_FAILURES = 8
_LOCKOUT_SECONDS = 300
_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


class AuthError(ValueError):
    """계정 관리 규칙 위반. 라우터가 400으로 바꾼다."""


@dataclass(frozen=True)
class AdminUser:
    id: int
    username: str
    role: str
    is_active: bool
    session_version: int
    created_at: str
    created_by: str | None
    last_login_at: str | None

    @property
    def is_superadmin(self) -> bool:
        return self.role == ROLE_SUPERADMIN


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_user(row) -> AdminUser:
    return AdminUser(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        is_active=bool(row["is_active"]),
        session_version=row["session_version"],
        created_at=row["created_at"],
        created_by=row["created_by"],
        last_login_at=row["last_login_at"],
    )


def hash_password(password: str) -> tuple[str, str]:
    """(hash_hex, salt_hex) 반환."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return digest.hex(), salt.hex()


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    try:
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt),
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=_SCRYPT_DKLEN,
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), password_hash)


def validate_password(password: str) -> str:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다."
        )
    return password


def validate_username(username: str) -> str:
    name = (username or "").strip().lower()
    if not (3 <= len(name) <= 32) or not name.replace("-", "").replace("_", "").isalnum():
        raise AuthError(
            "아이디는 영문·숫자·하이픈·밑줄 3~32자여야 합니다."
        )
    return name


# --- 조회 ---------------------------------------------------------------


def get_by_id(user_id: int) -> AdminUser | None:
    with storage.cursor() as cur:
        cur.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,))
        row = cur.fetchone()
    return _row_to_user(row) if row else None


def get_by_username(username: str) -> AdminUser | None:
    with storage.cursor() as cur:
        cur.execute("SELECT * FROM admin_users WHERE username = ?", (username,))
        row = cur.fetchone()
    return _row_to_user(row) if row else None


def list_users() -> list[AdminUser]:
    with storage.cursor() as cur:
        cur.execute(
            "SELECT * FROM admin_users "
            "ORDER BY CASE role WHEN 'superadmin' THEN 0 ELSE 1 END, username"
        )
        return [_row_to_user(row) for row in cur.fetchall()]


def count_users() -> int:
    with storage.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM admin_users")
        return cur.fetchone()["n"]


def has_superadmin() -> bool:
    with storage.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM admin_users WHERE role = ?",
            (ROLE_SUPERADMIN,),
        )
        return cur.fetchone()["n"] > 0


# --- 생성·변경 -----------------------------------------------------------


def create_user(
    username: str,
    password: str,
    *,
    role: str | None = None,
    created_by: str | None = None,
) -> AdminUser:
    """관리자를 만든다. role을 생략하면 첫 계정만 최고관리자가 된다."""
    name = validate_username(username)
    validate_password(password)

    if role is None:
        role = ROLE_ADMIN if has_superadmin() else ROLE_SUPERADMIN
    if role not in (ROLE_SUPERADMIN, ROLE_ADMIN):
        raise AuthError(f"알 수 없는 역할입니다: {role}")
    if role == ROLE_SUPERADMIN and has_superadmin():
        raise AuthError(
            "최고관리자는 1명만 지정할 수 있습니다. 이양 기능을 사용하세요."
        )

    password_hash, salt = hash_password(password)
    try:
        with storage.transaction() as cur:
            cur.execute(
                "INSERT INTO admin_users "
                "(username, password_hash, salt, role, is_active, session_version, "
                " created_at, created_by) "
                "VALUES (?, ?, ?, ?, 1, 1, ?, ?)",
                (name, password_hash, salt, role, _now(), created_by),
            )
    except sqlite3.IntegrityError as exc:
        if "ux_single_superadmin" in str(exc):
            raise AuthError("최고관리자는 1명만 지정할 수 있습니다.") from None
        raise AuthError(f"이미 존재하는 아이디입니다: {name}") from None

    user = get_by_username(name)
    assert user is not None
    return user


def set_password(user: AdminUser, password: str) -> None:
    """비밀번호를 바꾸고 기존 세션을 모두 무효화한다."""
    validate_password(password)
    password_hash, salt = hash_password(password)
    with storage.transaction() as cur:
        cur.execute(
            "UPDATE admin_users "
            "SET password_hash = ?, salt = ?, session_version = session_version + 1 "
            "WHERE id = ?",
            (password_hash, salt, user.id),
        )


def set_active(user: AdminUser, is_active: bool) -> None:
    """비활성화하면 session_version을 올려 로그인 중인 세션도 끊는다."""
    if user.is_superadmin and not is_active:
        raise AuthError("최고관리자 계정은 비활성화할 수 없습니다.")
    with storage.transaction() as cur:
        cur.execute(
            "UPDATE admin_users "
            "SET is_active = ?, session_version = session_version + 1 "
            "WHERE id = ?",
            (int(is_active), user.id),
        )


def delete_user(user: AdminUser) -> None:
    if user.is_superadmin:
        raise AuthError("최고관리자 계정은 삭제할 수 없습니다. 먼저 이양하세요.")
    with storage.transaction() as cur:
        cur.execute("DELETE FROM admin_users WHERE id = ?", (user.id,))


def transfer_superadmin(current: AdminUser, target: AdminUser) -> None:
    """최고관리자를 넘긴다. 두 UPDATE가 한 트랜잭션으로 묶인다.

    partial unique index가 superadmin 2명을 막으므로 현재 최고관리자를 먼저
    강등한 뒤 대상을 승격해야 한다.
    """
    if not current.is_superadmin:
        raise AuthError("최고관리자만 이양할 수 있습니다.")
    if target.id == current.id:
        raise AuthError("자기 자신에게는 이양할 수 없습니다.")
    if not target.is_active:
        raise AuthError("비활성 계정에는 이양할 수 없습니다.")
    if target.is_superadmin:
        raise AuthError("이미 최고관리자입니다.")

    with storage.transaction() as cur:
        cur.execute(
            "UPDATE admin_users SET role = ? WHERE id = ?",
            (ROLE_ADMIN, current.id),
        )
        cur.execute(
            "UPDATE admin_users SET role = ? WHERE id = ?",
            (ROLE_SUPERADMIN, target.id),
        )


def touch_login(user: AdminUser) -> None:
    with storage.transaction() as cur:
        cur.execute(
            "UPDATE admin_users SET last_login_at = ? WHERE id = ?",
            (_now(), user.id),
        )


# --- 로그인 --------------------------------------------------------------


def _is_locked_out(username: str) -> bool:
    with _failures_lock:
        recent = [
            stamp
            for stamp in _failures.get(username, [])
            if time.monotonic() - stamp < _LOCKOUT_SECONDS
        ]
        _failures[username] = recent
        return len(recent) >= _MAX_FAILURES


def _record_failure(username: str) -> None:
    with _failures_lock:
        _failures.setdefault(username, []).append(time.monotonic())


def clear_failures(username: str) -> None:
    with _failures_lock:
        _failures.pop(username, None)


def authenticate(username: str, password: str) -> AdminUser:
    """성공하면 사용자를, 실패하면 AuthError를 던진다."""
    name = (username or "").strip().lower()
    if _is_locked_out(name):
        raise AuthError(
            "로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요."
        )

    with storage.cursor() as cur:
        cur.execute("SELECT * FROM admin_users WHERE username = ?", (name,))
        row = cur.fetchone()

    # 계정이 없어도 해시를 한 번 계산해 응답 시간으로 존재 여부가 새지 않게 한다.
    stored_hash = row["password_hash"] if row else "00" * _SCRYPT_DKLEN
    stored_salt = row["salt"] if row else "00" * 16
    valid = verify_password(password or "", stored_hash, stored_salt)

    if not row or not valid or not row["is_active"]:
        _record_failure(name)
        raise AuthError("아이디 또는 비밀번호가 올바르지 않습니다.")

    clear_failures(name)
    user = _row_to_user(row)
    touch_login(user)
    return user


def generate_password(length: int = 16) -> str:
    """비밀번호 리셋용 임시 비밀번호."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))
