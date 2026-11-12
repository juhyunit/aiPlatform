"""PostgreSQL 접근. 연결은 질의 하나만큼만 잡는다. LLM 호출 동안 잡고 있지 않는다."""
import os
from contextlib import asynccontextmanager

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

pool = AsyncConnectionPool(os.environ["DATABASE_URL"], open=False, max_size=30,
                           kwargs={"row_factory": dict_row})


@asynccontextmanager
async def tx(user: dict | None = None):
    """트랜잭션. user를 주면 행 수준 보안이 그 사용자 기준으로 걸린다."""
    async with pool.connection() as conn:
        if user:
            await conn.execute(
                "select set_config('app.user', %s, true), set_config('app.clearance', %s, true),"
                " set_config('app.groups', %s, true)",
                (user["id"], str(user["clearance"]), ",".join(user["groups"])))
        yield conn


async def q(user: dict | None, sql: str, params=()) -> list[dict]:
    async with tx(user) as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall() if cur.description else []
