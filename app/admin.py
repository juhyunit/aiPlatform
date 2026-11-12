"""운영자용 명령. 서버에서 직접 실행한다.

  python -m app.admin adduser <아이디> <비밀번호> <등급> [그룹 ...]
  python -m app.admin index <소유자 아이디> <파일> <등급> [접근 그룹 ...]     사내 지식으로 색인
  python -m app.admin verify-audit
"""
import asyncio
import os
import sys
from pathlib import Path

import psycopg

from . import auth, db, files, knowledge, trace
from .policy import rank


def adduser(user_id: str, password: str, grade: str, *groups: str):
    with psycopg.connect(os.environ["ADMIN_DATABASE_URL"]) as conn:    # users 표는 소유자만 쓸 수 있다
        conn.execute("insert into users values (%s, %s, %s, %s) on conflict (id) do update"
                     " set password_hash = excluded.password_hash, clearance = excluded.clearance, groups = excluded.groups",
                     (user_id, auth.hash_password(password), rank(grade), list(groups)))


async def index(owner: str, path: str, grade: str, *groups: str):
    """문서 한 건을 사내 지식으로 색인한다. 등급 태그는 필수다."""
    async with db.pool:
        user = (await db.q(None, "select id, clearance, groups from users where id = %s", (owner,)))[0]
        name, chunks = await files.parse("org", Path(path).name, Path(path).read_bytes())
        await knowledge.index(user, "org", name, rank(grade), chunks, groups=groups)
        await trace.audit(owner, "index", {"name": name, "grade": grade, "groups": list(groups), "chunks": len(chunks)})
    print(f"{name}: {len(chunks)}개 청크 색인")


async def verify_audit():
    async with db.pool:
        bad = await trace.verify_audit()
    print("감사 로그 해시 연쇄 정상" if bad is None else f"감사 로그가 id {bad}에서 어긋납니다")
    return bad is not None


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "adduser":
        adduser(*args)
    elif cmd == "index":
        asyncio.run(index(*args))
    elif cmd == "verify-audit":
        sys.exit(asyncio.run(verify_audit()))
