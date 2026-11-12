"""작업 워커. PostgreSQL을 작업 큐로 쓴다. 대기 중인 Agent 작업을 집어 상태 기계를 한 칸씩 돌리고 그때마다 저장한다."""
import asyncio
import logging

from psycopg.types.json import Jsonb

from . import db, workflow
from .policy import LIMITS
from .trace import Run

log = logging.getLogger("worker")


async def process(run_id, user_id):
    user = (await db.q(None, "select id, clearance, groups from users where id = %s", (user_id,)))[0]
    row = (await db.q(user, "select session_id from runs where id = %s", (run_id,)))[0]
    run = Run(run_id, user, row["session_id"])
    try:
        while True:
            row = (await db.q(user, "select status, state from runs where id = %s", (run_id,)))[0]
            if row["status"] != "running":      # 사용자가 일시 정지하거나 취소했다
                return
            state, status = await workflow.advance(run, row["state"])
            # 그사이 사용자가 정지·취소했으면 그 상태를 덮어쓰지 않는다
            await db.q(user, "update runs set state = %s, updated_at = now(),"
                             " status = case when status = 'running' then %s else status end where id = %s",
                       (Jsonb(state), status, run_id))
    except Exception as e:
        log.exception("run %s", run_id)
        await db.q(user, "update runs set status = 'failed', updated_at = now() where id = %s", (run_id,))
        await run.emit("run_done", {"ok": False, "error": str(e)})


async def main():
    async with db.pool:
        await db.q(None, "select requeue_runs()")       # 워커가 죽기 전에 돌던 작업을 마지막 저장 지점부터 재개한다
        active = set()
        while True:
            claimed = await db.q(None, "select * from claim_run()") if len(active) < LIMITS["agent_concurrency"] else []
            if claimed:
                task = asyncio.create_task(process(claimed[0]["id"], claimed[0]["user_id"]))
                active.add(task)
                task.add_done_callback(active.discard)
            else:
                await asyncio.sleep(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
