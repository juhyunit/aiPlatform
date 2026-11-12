"""API 서버. 인증, 권한, 세션, 요청 접수, 이벤트 스트림. 상태는 모두 DB에 있다."""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from . import auth, db, files, gateway, knowledge, modes, sandbox, workflow
from .policy import GRADES, LIMITS, TOP, rank
from .trace import Run, audit

log = logging.getLogger("api")
_tasks = set()      # 실행 중인 대화형 요청. 화면 연결이 끊겨도 끝까지 돌아 결과를 저장한다


@asynccontextmanager
async def lifespan(app):
    async with db.pool:
        yield

app = FastAPI(lifespan=lifespan)
User = Depends(auth.current_user)


def sse(type: str, data: dict) -> str:
    return f"event: {type}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


class Login(BaseModel):
    id: str
    password: str


@app.post("/api/login")
async def login(body: Login, response: Response):
    rows = await db.q(None, "select password_hash from users where id = %s", (body.id,))
    # 해시 계산은 CPU를 오래 쓴다. 이벤트 루프를 막지 않게 스레드에서 돌린다
    if not rows or not await asyncio.to_thread(auth.check_password, body.password, rows[0]["password_hash"]):
        await audit(body.id, "login_failed", {})
        raise HTTPException(401, "아이디 또는 비밀번호가 맞지 않습니다")
    response.set_cookie("token", auth.issue(body.id), httponly=True, samesite="strict", max_age=auth.TTL)
    await audit(body.id, "login", {})


@app.get("/api/me")
async def me(user=User):
    """화면이 선택지를 그리는 데 필요한 것: 사용자, 고를 수 있는 등급, 모델 목록. 모델을 실제로 쓸 수 있는지는 요청마다 정책 필터가 정한다."""
    models = [m["id"] for m in gateway.MODELS if m["status"] == "active" and "embedding" not in m["capabilities"]]
    return {**user, "grades": GRADES[:user["clearance"] + 1], "models": models}


def _grade_rank(grade: str | None, default: int = TOP) -> int:
    if grade is None:
        return default
    try:
        return rank(grade)
    except ValueError as e:
        raise HTTPException(422, str(e))


class NewSession(BaseModel):
    title: str
    grade: str


@app.post("/api/sessions")
async def create_session(body: NewSession, user=User):
    g = _grade_rank(body.grade)
    if g > user["clearance"]:
        raise HTTPException(403, "자신의 등급보다 높은 세션은 만들 수 없습니다")
    return (await db.q(user, "insert into sessions (user_id, title, grade) values (%s, %s, %s) returning id, title, grade",
                       (user["id"], body.title, g)))[0]


@app.get("/api/sessions")
async def list_sessions(user=User):
    return await db.q(user, "select id, title, grade, created_at from sessions order by created_at desc")


@app.get("/api/sessions/{session_id}/messages")
async def list_messages(session_id: UUID, user=User):
    return await db.q(user, "select role, content from messages where session_id = %s order by id", (session_id,))


@app.put("/api/sessions/{session_id}/files/{name}")
async def upload(session_id: UUID, name: str, request: Request, grade: str | None = None, user=User):
    """첨부 파일. 등급 태그가 없으면 최고 등급으로 취급한다."""
    if not await db.q(user, "select 1 from sessions where id = %s", (session_id,)):
        raise HTTPException(404, "세션이 없습니다")
    g = _grade_rank(grade, TOP)
    if g > user["clearance"]:
        raise HTTPException(403, "자신의 등급보다 높은 자료는 올릴 수 없습니다. 등급을 지정해 주세요")
    try:
        doc = await files.ingest(user, session_id, name, await request.body(), g)
    except ValueError as e:
        raise HTTPException(422, str(e))
    await audit(user["id"], "upload", {"session": str(session_id), "document": str(doc["id"]), "name": doc["name"], "grade": GRADES[g]})
    return doc


class Memory(BaseModel):
    text: str
    grade: str | None = None


@app.post("/api/memories")
async def save_memory(body: Memory, user=User):
    """장기 기억은 사용자가 직접 저장한 것만 둔다."""
    g = _grade_rank(body.grade, user["clearance"])
    if g > user["clearance"]:
        raise HTTPException(403, "자신의 등급보다 높은 기억은 저장할 수 없습니다")
    return {"id": await knowledge.index(user, "memory", "기억", g, [{"page": None, "heading": "", "text": body.text}])}


@app.get("/api/memories")
async def list_memories(user=User):
    return await db.q(user, """select d.id, d.grade, c.text from documents d join chunks c on c.document_id = d.id
                               where d.scope = 'memory' and d.owner = %s order by c.id""", (user["id"],))


@app.delete("/api/memories/{doc_id}")
async def delete_memory(doc_id: UUID, user=User):
    await db.q(user, "delete from documents where id = %s and scope = 'memory' and owner = %s", (doc_id, user["id"]))


class Chat(BaseModel):
    session_id: UUID
    message: str
    mode: Literal["auto", "quick", "thinking", "agent"] = "auto"
    model: str = "auto"      # auto | 모델 ID
    search: bool = False     # 사내 지식 검색
    images: list[str] = []   # 이번 질문에 붙일 이미지 파일 이름(세션에 올린 것)


@app.post("/api/chat")
async def chat(req: Chat, user=User):
    async with db.tx(user) as conn:
        if not await (await conn.execute("select 1 from sessions where id = %s", (req.session_id,))).fetchone():
            raise HTTPException(404, "세션이 없습니다")
        await conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (user["id"],))
        usage = await (await conn.execute("""
            select count(*) filter (where status in ('queued', 'running') and updated_at > now() - interval '15 minutes') as active,
                   coalesce(sum(tokens) filter (where created_at >= current_date), 0) as tokens from runs""")).fetchone()
        if usage["active"] >= LIMITS["concurrent_requests"]:
            raise HTTPException(429, "동시에 처리할 수 있는 요청 수를 넘었습니다. 진행 중인 요청이 끝난 뒤 다시 시도해 주세요")
        if usage["tokens"] >= LIMITS["daily_tokens"]:
            raise HTTPException(429, "오늘 사용할 수 있는 토큰을 모두 썼습니다")
        run_id = (await (await conn.execute(
            "insert into runs (session_id, user_id, mode, status) values (%s, %s, %s, 'running') returning id",
            (req.session_id, user["id"], req.mode))).fetchone())["id"]
    run = Run(run_id, user, req.session_id, live=True)
    try:
        mode, cands = await modes.route(run, req)
    except gateway.Denied as e:     # 정책 거부는 감사 로그에 남긴다
        await db.q(user, "update runs set status = 'failed' where id = %s", (run_id,))
        await audit(user["id"], "denied", {"run": str(run_id), "reason": str(e)})
        raise HTTPException(403, str(e))

    if mode == "agent":     # 작업 큐에 넣고, 워커가 남기는 이벤트를 따라간다
        await db.q(user, "update runs set mode = 'agent', status = 'queued', state = %s where id = %s",
                   (Jsonb(workflow.new_state(req.message, req.model)), run_id))
        return _stream(follow(user, run_id, 0))

    async def work():
        try:
            await modes.chat(run, cands, mode, req)
        except Exception as e:
            log.exception("run %s", run_id)
            await db.q(user, "update runs set status = 'failed', updated_at = now() where id = %s", (run_id,))
            await run.emit("run_done", {"ok": False, "error": str(e)})
        finally:
            await audit(user["id"], "run", {"run": str(run_id), "mode": mode, **await run.used()})
            run.queue.put_nowait(None)

    task = asyncio.create_task(work())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)

    async def stream():
        yield sse("run", {"id": run_id})
        while (item := await run.queue.get()) is not None:
            yield sse(*item)

    return _stream(stream())


def _stream(events):
    return StreamingResponse(events, media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


async def follow(user, run_id, after: int):
    """Agent 작업의 이벤트를 추적 표에서 읽어 보낸다. 작업이 끝나거나 사용자를 기다리는 상태가 되면 닫는다."""
    yield sse("run", {"id": run_id})
    while True:
        async with db.tx(user) as conn:
            status = (await (await conn.execute("select status from runs where id = %s", (run_id,))).fetchone())["status"]
            rows = await (await conn.execute(
                "select id, type, data from events where run_id = %s and id > %s order by id", (run_id, after))).fetchall()
        for row in rows:
            after = row["id"]
            yield sse(row["type"], {**row["data"], "event_id": row["id"]})
        if status not in ("queued", "running"):     # 상태를 먼저 읽었으므로 마지막 이벤트까지 보낸 뒤다
            yield sse("status", {"status": status})
            return
        await asyncio.sleep(0.5)


@app.get("/api/runs")
async def list_runs(user=User):
    """작업 이력."""
    return await db.q(user, """select id, session_id, mode, status, tokens, created_at, state->>'request' as request
                               from runs order by created_at desc limit 100""")


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: UUID, after: int = 0, user=User):
    if not await db.q(user, "select 1 from runs where id = %s", (run_id,)):
        raise HTTPException(404, "작업이 없습니다")
    return _stream(follow(user, run_id, after))


class Control(BaseModel):
    action: str                          # approve | pause | resume | cancel
    criteria: list[str] | None = None    # 승인하거나 재개할 때 완료 조건을 고쳐 보낼 수 있다


@app.post("/api/runs/{run_id}/control")
async def control(run_id: UUID, body: Control, user=User):
    """사람의 개입: 승인, 일시 정지, 재개, 취소. 정지한 동안 완료 조건을 고칠 수 있다."""
    async with db.tx(user) as conn:
        row = await (await conn.execute("select status, state from runs where id = %s and mode = 'agent' for update", (run_id,))).fetchone()
        if not row:
            raise HTTPException(404, "작업이 없습니다")
        status, state = row["status"], row["state"]
        allowed = {"approve": ("waiting",), "pause": ("queued", "running"), "resume": ("paused",),
                   "cancel": ("queued", "running", "waiting", "paused")}
        if status not in allowed.get(body.action, ()):
            raise HTTPException(409, f"지금 상태({status})에서는 할 수 없습니다")
        if body.action in ("approve", "resume") and body.criteria and body.criteria != [c["text"] for c in state["criteria"]]:
            # 완료 조건이 바뀌면 끝난 단계는 두고 남은 계획을 다시 세운다
            state["criteria"] = [{"id": f"C{i + 1}", "text": t} for i, t in enumerate(body.criteria)]
            state["steps"] = [st for st in state["steps"] if st["status"] == "done"]
            state["phase"] = "plan"
        if body.action == "approve":
            state["approved"] = True
        status = {"approve": "queued", "pause": "paused", "resume": "queued", "cancel": "cancelled"}[body.action]
        await conn.execute("update runs set status = %s, state = %s, updated_at = now() where id = %s", (status, Jsonb(state), run_id))
    await audit(user["id"], body.action, {"run": str(run_id)})
    return {"status": status}


@app.get("/api/sessions/{session_id}/files")
async def list_files(session_id: UUID, user=User):
    """작업 폴더의 파일: 첨부와 결과물."""
    if not await db.q(user, "select 1 from sessions where id = %s", (session_id,)):
        raise HTTPException(404, "세션이 없습니다")
    root = sandbox.workspace(session_id)
    return [str(p.relative_to(root)) for p in sorted(root.rglob("*"))
            if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)]


@app.get("/api/sessions/{session_id}/files/{path:path}")
async def download(session_id: UUID, path: str, user=User):
    if not await db.q(user, "select 1 from sessions where id = %s", (session_id,)):
        raise HTTPException(404, "세션이 없습니다")
    root = sandbox.workspace(session_id).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "파일이 없습니다")
    await audit(user["id"], "download", {"session": str(session_id), "path": path})
    return FileResponse(target, filename=target.name)


app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "web", html=True))
