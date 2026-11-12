"""추적과 감사. 모든 요청은 실행(run) 하나이고, LLM 호출·도구 호출·검증 결과를 같은 events 표에 남긴다."""
import asyncio
import hashlib
import json
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from . import db
from .policy import mask


class Run:
    """실행 하나의 문맥. live면 저장한 이벤트를 화면 스트림에도 보낸다."""

    def __init__(self, id, user: dict, session_id, live: bool = False):
        self.id, self.user, self.session_id = id, user, session_id
        self.queue = asyncio.Queue() if live else None
        self.clear = 0              # 답할 모델이 받을 수 있는 최고 데이터 등급. 검색 필터에 쓴다
        self.sources = {}           # 이번 실행에서 모델에 준 자료(청크 ID → 행). 인용 확인에 쓴다
        self.evidence = []          # 도구 실행 결과. 수치 확인에 쓴다

    async def emit(self, type: str, data: dict, store: bool = True):
        if store:
            data = mask(data)
            await db.q(self.user, "insert into events (run_id, type, data) values (%s, %s, %s)",
                       (self.id, type, Jsonb(data)))
        if self.queue:
            self.queue.put_nowait((type, data))

    async def used(self) -> dict:
        """감사 로그용 요약: 이 실행이 쓴 모델, 도구, 문서."""
        rows = await db.q(self.user, "select type, data from events where run_id = %s and type in ('llm_call', 'tool_call')", (self.id,))
        return {"models": sorted({r["data"]["model"] for r in rows if r["type"] == "llm_call"}),
                "tools": sorted({r["data"]["tool"] for r in rows if r["type"] == "tool_call"}),
                "documents": sorted({r["name"] for r in self.sources.values()})}

    async def add_tokens(self, n: int):
        await db.q(self.user, "update runs set tokens = tokens + %s, updated_at = now() where id = %s", (n, self.id))


def _hash(prev: str, at: datetime, user_id: str, action: str, detail: dict) -> str:
    body = json.dumps([prev, at.astimezone(timezone.utc).isoformat(), user_id, action, detail],
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


async def audit(user_id: str, action: str, detail: dict):
    """감사 로그에 한 행을 잇는다. 잠금으로 순서를 고정해 해시 연쇄가 갈라지지 않게 한다."""
    detail, at = mask(detail), datetime.now(timezone.utc)
    async with db.tx() as conn:
        await conn.execute("select pg_advisory_xact_lock(1)")
        last = await (await conn.execute("select hash from audit_log order by id desc limit 1")).fetchone()
        prev = last["hash"] if last else ""
        await conn.execute(
            "insert into audit_log (at, user_id, action, detail, prev_hash, hash) values (%s, %s, %s, %s, %s, %s)",
            (at, user_id, action, Jsonb(detail), prev, _hash(prev, at, user_id, action, detail)))


async def verify_audit() -> int | None:
    """해시 연쇄를 처음부터 다시 계산한다. 어긋난 첫 행의 id를 돌려주고, 온전하면 None."""
    prev = ""
    for row in await db.q(None, "select * from audit_log order by id"):
        if row["prev_hash"] != prev or row["hash"] != _hash(prev, row["at"], row["user_id"], row["action"], row["detail"]):
            return row["id"]
        prev = row["hash"]
    return None
