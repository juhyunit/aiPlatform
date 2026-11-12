"""모드 라우터와 대화형 실행 경로(Quick, Thinking). Agent는 작업 큐로 넘긴다."""
import base64
import json
import re

from . import context, db, gateway, knowledge, prompts, runtime, sandbox, validator

# Auto 판정 규칙. 산출물 생성과 다단계 지시는 Agent, 계산·비교·원인 분석은 Thinking.
AGENT = re.compile(r"만들어|작성해|생성해|개발해|구현해|짜\s?줘|보고서|프로그램|파일로|엑셀로|워드로")
THINKING = re.compile(r"계산|비교|분석|원인|왜 |이유|차이|검토|평가|합계|평균")
MODE_SCHEMA = {"type": "object", "required": ["mode"], "additionalProperties": False,
               "properties": {"mode": {"enum": ["quick", "thinking", "agent"]}}}
THINKING_TOOLS = ["search", "list_files", "read_file", "run_python"]     # 읽기 전용 도구와 계산용 코드 실행


async def candidates(run, model: str, need: tuple = ()) -> list[dict]:
    """정책 필터. 요청의 데이터 등급은 세션과 첨부의 태그에서 읽는다. 모델이 민감도를 추정하게 하지 않는다."""
    grade = (await db.q(run.user, """
        select greatest(s.grade, coalesce((select max(d.grade) from documents d where d.session_id = s.id), 0)) as grade
        from sessions s where s.id = %s""", (run.session_id,)))[0]["grade"]
    cands = gateway.select(grade, model, need=need)
    run.clear = gateway.clearance(cands[0])
    return cands


async def route(run, req) -> tuple[str, list[dict]]:
    """모델 후보와 실행 모드를 정한다."""
    cands = await candidates(run, req.model, ("vision",) if req.images else ())
    mode = req.mode
    if mode == "auto":
        attached = await db.q(run.user, "select 1 from documents where session_id = %s limit 1", (run.session_id,))
        mode = ("agent" if AGENT.search(req.message) else "thinking" if THINKING.search(req.message)
                else "quick" if len(req.message) < 40 and not attached else None)
    if mode is None:    # 규칙으로 가르지 못한 요청만 모델이 분류한다
        out = await gateway.chat(run, cands, [{"role": "system", "content": prompts.load("router", cands[0]["id"])},
                                              {"role": "user", "content": req.message}], schema=MODE_SCHEMA, max_tokens=16)
        mode = json.loads(out["content"])["mode"]
    await run.emit("mode", {"mode": mode, "model": cands[0]["id"], "prompt_version": prompts.VERSION})
    return mode, cands


async def _messages(run, cands, mode: str, req) -> tuple[list[dict], int]:
    """시스템 프롬프트, 기억, 이력, 자료, 질문을 예산 안에서 조립한다. 메시지와 출력 토큰 상한을 돌려준다."""
    model = cands[0]
    system = prompts.load(mode, model["id"])
    if mode == "thinking" and "high" not in model["capabilities"].get("reasoning", {}):
        system += "\n" + prompts.load("reason", model["id"])     # 추론 제어가 없는 모델은 2단 프롬프트로 대신한다
    memories = await knowledge.memories(run.user, run.clear)
    if memories:
        system += "\n\n[사용자가 저장한 기억]\n" + "\n".join("- " + m for m in context.take(memories, model["capabilities"]["context"] // 20))
    budget = int(model["capabilities"]["context"] * context.SLOTS["retrieved"])
    rows = await knowledge.attached(run.user, run.clear, run.session_id, budget)     # 작은 첨부는 전문을 넣는다
    if rows is None or req.search:
        found = await knowledge.search(run.user, run.clear, req.message, run.session_id, req.search)
        rows = (rows or []) + [r for r in found if r["id"] not in {x["id"] for x in rows or []}]
    run.sources.update({r["id"]: r for r in rows})
    history, summary = await context.history(run, cands)
    messages, max_tokens = context.assemble(model, mode, system, history, summary, [knowledge.cite(r) for r in rows], req.message)
    if req.images:      # 이미지는 이번 질문에만 붙인다. 이 세션에 올린 파일만 쓸 수 있다
        uploaded = await db.q(run.user, "select name from documents where session_id = %s and name = any(%s)", (run.session_id, req.images))
        if len(uploaded) != len(set(req.images)):
            raise ValueError("이 대화에 첨부하지 않은 이미지입니다")
        root = sandbox.workspace(run.session_id)
        parts = [{"type": "image_url", "image_url": {"url": "data:image/" + ("png" if name.lower().endswith("png") else "jpeg") +
                  ";base64," + base64.b64encode((root / name).read_bytes()).decode()}} for name in req.images]
        messages[-1]["content"] = [{"type": "text", "text": messages[-1]["content"]}, *parts]
    return messages, max_tokens


def _check(run, answer: str, question: str) -> list[str]:
    evidence = question + "\n".join(r["text"] for r in run.sources.values()) + "\n".join(run.evidence)
    return validator.citations(answer, set(run.sources)) + validator.numbers(answer, evidence)


async def quick(run, cands, req, on_token) -> tuple[str, bool]:
    """LLM 1회 호출. 검증은 형식 검사(빈 응답, 잘린 응답)뿐이다."""
    messages, max_tokens = await _messages(run, cands, "quick", req)
    out = await gateway.chat(run, cands, messages, max_tokens=max_tokens, on_token=on_token)
    ok = bool(out["content"].strip()) and out["finish"] != "length"
    await run.emit("check_result", {"check": "형식", "ok": ok})
    return out["content"], ok


async def thinking(run, cands, req, on_token) -> tuple[str, bool]:
    """추론을 켠 단일 에이전트. 인용과 수치를 확인하고, 실패하면 한 번만 고친다."""
    messages, _ = await _messages(run, cands, "thinking", req)
    answer = await runtime.run_step(run, cands, messages, THINKING_TOOLS, mode="thinking", reasoning="high", on_token=on_token)
    problems = _check(run, answer, req.message)
    await run.emit("check_result", {"check": "인용·수치", "ok": not problems, "problems": problems})
    if problems:
        await run.emit("issue", {"problems": problems})
        messages += [{"role": "assistant", "content": answer},
                     {"role": "user", "content": "검증에서 다음 문제가 나왔다. 고쳐서 답 전체를 다시 써라.\n- " + "\n- ".join(problems)}]
        await run.emit("token", {"reset": True}, store=False)
        answer = await runtime.run_step(run, cands, messages, THINKING_TOOLS, mode="thinking", reasoning="high", on_token=on_token)
        problems = _check(run, answer, req.message)
        await run.emit("fix", {"ok": not problems, "problems": problems})
    return answer, not problems


async def chat(run, cands, mode: str, req):
    """대화형 요청 하나를 끝까지 처리하고 대화를 저장한다. Quick이 형식 검사에 걸리면 Thinking으로 승격한다."""
    async def on_token(text):
        await run.emit("token", {"text": text}, store=False)

    if mode == "quick":
        answer, ok = await quick(run, cands, req, on_token)
        if not ok:
            mode = "thinking"
            await run.emit("mode", {"mode": mode, "model": cands[0]["id"], "promoted": True})
            await run.emit("token", {"reset": True}, store=False)
    if mode == "thinking":
        answer, ok = await thinking(run, cands, req, on_token)
    await db.q(run.user, "insert into messages (session_id, role, content) values (%s, 'user', %s), (%s, 'assistant', %s)",
               (run.session_id, req.message, run.session_id, answer))
    await db.q(run.user, "update runs set status = 'done', mode = %s, updated_at = now() where id = %s", (mode, run.id))
    sources = [{"id": r["id"], "name": r["name"], "page": r["page"]} for r in run.sources.values()]
    await run.emit("run_done", {"ok": ok, "verified": "형식" if mode == "quick" else "인용·수치", "sources": sources})
