"""인증. 개발 단계는 로컬 계정이다. 사내 이식 때 OIDC로 바꾸는 자리는 login과 current_user 두 곳이다."""
import hashlib
import hmac
import os
import time

from fastapi import HTTPException, Request

from . import db

SECRET = os.environ["SECRET_KEY"].encode()
TTL = 12 * 3600


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    return salt.hex() + ":" + hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_password(password: str, stored: str) -> bool:
    return hmac.compare_digest(hash_password(password, bytes.fromhex(stored.split(":")[0])), stored)


def issue(user_id: str) -> str:
    body = f"{user_id}|{int(time.time()) + TTL}"
    return body + "|" + hmac.new(SECRET, body.encode(), "sha256").hexdigest()


async def current_user(request: Request) -> dict:
    """쿠키의 토큰을 확인하고 사용자 행을 돌려준다. 권한은 매 요청 DB에서 다시 읽는다."""
    body, _, sig = request.cookies.get("token", "").rpartition("|")
    user_id, _, expires = body.partition("|")
    if not hmac.compare_digest(sig, hmac.new(SECRET, body.encode(), "sha256").hexdigest()) \
            or int(expires or 0) < time.time():
        raise HTTPException(401, "로그인이 필요합니다")
    rows = await db.q(None, "select id, clearance, groups from users where id = %s", (user_id,))
    if not rows:
        raise HTTPException(401, "로그인이 필요합니다")
    return rows[0]
