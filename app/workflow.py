"""Workflow Engine. Agent 모드의 상태 기계다: 명세 → 계획 → (사용자 승인) → 실행 → 검증 → 요구사항 대조.

다음 단계와 종료는 모델이 아니라 이 코드가 정한다. 관문을 여는 것은 테스트와 규칙 검사의 결과다.
advance()는 한 칸만 움직이고, 워커가 그때마다 상태를 DB에 저장한다. 중단된 작업은 마지막 저장 지점부터 재개된다.
"""
import json
import re
import time

from . import db, gateway, modes, prompts, runtime, sandbox, tools
from .policy import BUDGET, HARNESS, P
from .trace import audit

WORKER_TOOLS = ["search", "list_files", "read_file", "write_file", "run_python"]

# 업무 템플릿 3종. verify는 검증 단계에서 샌드박스로 돌리는 (이름, 명령)이다.
TEMPLATES = {
    "doc": {"name": "문서 분석", "verify": []},     # 인용 확인은 아래 _citations가 한다
    "data": {"name": "데이터 분석", "verify": [
        ("재실행 결과 일치", "mkdir -p .agent && cp result.json .agent/before.json && python analysis.py && python -c \""
         "import json; assert json.load(open('result.json')) == json.load(open('.agent/before.json')), '재실행 결과가 다르다'\"")]},
    "dev": {"name": "프로그램 개발", "verify": [("전체 테스트", "python -m pytest -q -p no:cacheprovider"),
                                          ("린트", "ruff check --no-cache --exclude .agent .")]},
}

_TEXT = {"type": "string", "minLength": 1}
SPEC = {"type": "object", "required": ["template", "criteria"], "additionalProperties": False, "properties": {
    "template": {"enum": list(TEMPLATES)}, "criteria": {"type": "array", "minItems": 1, "items": _TEXT}}}
PLAN = {"type": "object", "required": ["steps"], "additionalProperties": False, "properties": {
    "steps": {"type": "array", "minItems": 1, "items": {
        "type": "object", "required": ["task", "criteria", "check"], "additionalProperties": False,
        "properties": {"task": _TEXT, "criteria": {"type": "array", "items": _TEXT}, "check": _TEXT}}}}}
REVIEW = {"type": "object", "required": ["issues"], "additionalProperties": False, "properties": {
    "issues": {"type": "array", "items": {
        "type": "object", "required": ["problem", "check"], "additionalProperties": False,
        "properties": {"problem": _TEXT, "check": _TEXT}}}}}


def new_state(request: str, model: str) -> dict:
    return {"request": request, "model": model, "phase": "spec", "approved": False, "template": None,
            "criteria": [], "steps": [], "notes": [], "rounds": 0, "replans": 0, "elapsed": 0}


async def _ask(run, cands, prompt: str, text: str, schema: dict) -> dict:
    out = await gateway.chat(run, cands, [{"role": "system", "content": prompts.load(prompt, cands[0]["id"])},
                                          {"role": "user", "content": text}], schema=schema, max_tokens=P["max_output"]["agent"])
    return json.loads(out["content"])


def _criteria(s: dict) -> str:
    return "\n".join(f"{c['id']}. {c['text']}" for c in s["criteria"])


async def _check(run, name: str, command: str) -> tuple[bool, str]:
    code, out = await sandbox.run(run.session_id, ["sh", "-c", command], timeout=180)
    await run.emit("check_result", {"check": name, "ok": code == 0, "output": out[-2000:]})
    return code == 0, out[-runtime.ERROR_TAIL:]


async def spec(run, cands, s):
    """명세. 요구사항을 확인 가능한 완료 조건 목록으로 정리한다."""
    out = await _ask(run, cands, "planner_spec", f"요청:\n{s['request']}\n\n작업 폴더의 파일:\n{await tools.list_files(run)}", SPEC)
    s |= {"template": out["template"], "criteria": [{"id": f"C{i + 1}", "text": t} for i, t in enumerate(out["criteria"])], "phase": "plan"}
    return s, "running"


async def plan(run, cands, s):
    """계획. 아직 단계에 연결되지 않은 완료 조건을 단계로 나눈다. 모든 조건이 단계에 연결됐는지는 코드가 확인한다."""
    covered = {c for st in s["steps"] for c in st["criteria"]}
    todo = [c["id"] for c in s["criteria"] if c["id"] not in covered]
    text = (f"요청:\n{s['request']}\n\n업무 유형: {TEMPLATES[s['template']]['name']}\n\n완료 조건:\n{_criteria(s)}\n\n"
            f"단계에 연결해야 하는 조건: {', '.join(todo)}\n\n작업 폴더의 파일:\n{await tools.list_files(run)}")
    for _ in range(2):
        steps = [{"id": f"S{len(s['steps']) + i + 1}", **st, "status": "pending", "fixes": 0, "summary": "", "feedback": ""}
                 for i, st in enumerate((await _ask(run, cands, "planner_plan", text, PLAN))["steps"])]
        missing = set(todo) - {c for st in steps for c in st["criteria"]}
        if not missing:
            break
        text += f"\n\n앞선 계획은 조건 {', '.join(sorted(missing))}을 어느 단계에도 연결하지 않았다. 모든 조건을 연결해 다시 계획한다."
    else:
        return await finish(run, s, f"계획이 완료 조건 {', '.join(sorted(missing))}을 다루지 못했습니다")
    s["steps"] += steps
    s["phase"] = "execute"
    await run.emit("plan", {"template": TEMPLATES[s["template"]]["name"], "criteria": s["criteria"],
                            "steps": [{k: st[k] for k in ("id", "task", "criteria", "check")} for st in s["steps"]]})
    if not s["approved"]:      # 첫 번째 관문: 사용자가 완료 조건과 계획을 승인한다
        await run.emit("approval_required", {"what": "완료 조건과 계획"})
        return s, "waiting"
    return s, "running"


async def execute(run, cands, s):
    """실행. 대기 중인 단계 하나를 새 컨텍스트의 Worker가 수행하고, 그 단계의 검사를 돌린다."""
    step = next((st for st in s["steps"] if st["status"] == "pending"), None)
    if not step:
        s["phase"] = "verify"
        return s, "running"
    await run.emit("step_started", {"step": step["id"], "task": step["task"], "profile": "Worker"})
    done = "\n".join(f"- {st['id']} {st['task']}: {st['summary']}" for st in s["steps"] if st["status"] == "done")
    task = (f"전체 요청:\n{s['request']}\n\n끝난 단계:\n{done or '없음'}\n\n작업 폴더의 파일:\n{await tools.list_files(run)}\n\n"
            f"이번 단계:\n{step['task']}")
    if step["feedback"]:    # 실패한 검사의 출력은 줄이지 않고 그대로 준다
        task += f"\n\n앞선 시도에서 아래 검사가 실패했다. 원인을 고친다.\n{step['feedback']}"
    state = f"이번 단계: {step['task']}\n통과해야 하는 검사: {step['check']}\n관련 완료 조건: {', '.join(step['criteria']) or '검증 실패 수정'}"
    system = prompts.load("worker", cands[0]["id"]) + "\n" + prompts.load("worker_" + s["template"], cands[0]["id"])
    summary = await runtime.run_step(run, cands, [{"role": "system", "content": system}, {"role": "user", "content": task}],
                                     WORKER_TOOLS, mode="agent", state=state)
    ok, out = await _check(run, step["check"], step["check"])
    if ok:
        step |= {"status": "done", "summary": summary[:500], "feedback": ""}
        await run.emit("step_done", {"step": step["id"], "summary": step["summary"]})
        return s, "running"
    step["fixes"] += 1
    step["feedback"] = out
    await run.emit("issue", {"step": step["id"], "problems": [f"검사 실패: {step['check']}"]})
    if step["fixes"] > BUDGET["fixes_per_step"]:
        return await finish(run, s, f"단계 {step['id']}의 검사가 수정 {BUDGET['fixes_per_step']}회 뒤에도 실패했습니다:\n{out[-1500:]}")
    return s, "running"


async def _citations(run) -> tuple[bool, str]:
    """문서 분석의 규칙 검사. answer.md가 인용한 번호가 이 사용자가 볼 수 있는 자료에 실제로 있어야 한다."""
    path = sandbox.workspace(run.session_id) / "answer.md"
    if not path.exists():
        return False, "answer.md가 없다"
    cited = {int(n) for n in re.findall(r"\[#(\d+)\]", path.read_text(encoding="utf-8"))}
    found = {r["id"] for r in await db.q(run.user, "select id from chunks where id = any(%s)", (list(cited),))}
    if not cited:
        return False, "answer.md에 [#번호] 인용이 없다"
    return cited == found, "없는 인용: " + ", ".join(f"[#{n}]" for n in sorted(cited - found))


def _artifacts(run, cands) -> str:
    """Reviewer에게 보여줄 결과물. 글로 읽히는 파일만, 컨텍스트의 절반 안에서 고르게 나눠 싣는다."""
    root = sandbox.workspace(run.session_id)
    texts = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not any(x.startswith(".") for x in p.relative_to(root).parts):
            try:
                texts[str(p.relative_to(root))] = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                pass
    share = cands[0]["capabilities"]["context"] // 2 // max(1, len(texts))     # 파일 하나의 글자 수 상한
    return "\n\n".join(f"--- {name} ---\n{text[:share]}" for name, text in texts.items())


async def verify(run, cands, s):
    """검증(두 번째 관문). 실행·규칙 검사가 통과와 실패를 정한다. Reviewer의 지적은 재현되는 검사로 바뀔 때만 실패다."""
    failures = []
    for name, command in TEMPLATES[s["template"]]["verify"]:
        ok, out = await _check(run, name, command)
        if not ok:
            failures.append((command, out))
    if s["template"] == "doc":
        ok, out = await _citations(run)
        await run.emit("check_result", {"check": "인용 실재", "ok": ok, "output": "" if ok else out})
        if not ok:
            failures.append(("test -f answer.md", "answer.md의 인용을 고친다. " + out))
    if not failures and HARNESS["reviewer"]:    # 작업 과정을 보지 않은 새 컨텍스트에서 결과물만 본다
        for n, issue in enumerate((await _ask(run, cands, "reviewer", f"완료 조건:\n{_criteria(s)}\n\n결과물:\n{_artifacts(run, cands)}", REVIEW))["issues"]):
            script = f".agent/review_{s['rounds']}_{n}.py"
            await tools.write_file(run, script, issue["check"])
            code, out = await sandbox.run(run.session_id, ["python", script])
            await run.emit("issue", {"profile": "Reviewer", "problems": [issue["problem"]], "confirmed": code != 0})
            if code:
                failures.append((f"python {script}", f"{issue['problem']}\n{out}"))
            else:   # 재현되지 않은 지적은 참고 의견으로만 넘긴다
                s["notes"].append(issue["problem"])
    if not failures:
        s["phase"] = "reconcile"
        return s, "running"
    s["rounds"] += 1
    if s["rounds"] > BUDGET["fixes_per_step"]:
        return await finish(run, s, f"검증이 수정 {BUDGET['fixes_per_step']}회 뒤에도 실패했습니다:\n{failures[0][1][-1500:]}")
    for command, out in failures:      # 오류 출력을 들고 실행으로 돌아간다
        s["steps"].append({"id": f"S{len(s['steps']) + 1}", "task": "검증에서 실패한 검사가 통과하도록 결과물을 고친다", "criteria": [],
                           "check": command, "status": "pending", "fixes": 0, "summary": "", "feedback": out})
    s["phase"] = "execute"
    return s, "running"


def _evidence(s: dict) -> dict:
    """완료 조건마다 통과한 검사를 연결한다."""
    return {c["id"]: [st["check"] for st in s["steps"] if st["status"] == "done" and c["id"] in st["criteria"]] for c in s["criteria"]}


async def reconcile(run, cands, s):
    """요구사항 대조. 증거 없는 완료 조건이 있으면 계획으로 돌아간다."""
    if all(_evidence(s).values()):
        return await finish(run, s, None)
    s["replans"] += 1
    if s["replans"] > 1:
        return await finish(run, s, "일부 완료 조건의 증거를 만들지 못했습니다")
    s["phase"] = "plan"
    return s, "running"


async def finish(run, s, failure: str | None):
    """결과를 정리해 사용자에게 넘긴다. 부분 결과는 완료로 표시하지 않는다."""
    evidence = _evidence(s)
    unmet = [c["id"] for c in s["criteria"] if not evidence[c["id"]]]
    lines = [f"[Agent · {TEMPLATES[s['template']]['name'] if s['template'] else '작업'}] " + ("완료" if not failure else "미완료 — " + failure), "", "완료 조건"]
    lines += [f"- {'✓' if evidence[c['id']] else '✗'} {c['id']}. {c['text']}" + (f" (근거: {', '.join(evidence[c['id']])})" if evidence[c["id"]] else " (근거 없음)")
              for c in s["criteria"]]
    answer = sandbox.workspace(run.session_id) / "answer.md"
    if s["template"] == "doc" and answer.exists():
        lines += ["", answer.read_text(encoding="utf-8")]
    files = await tools.list_files(run)
    lines += ["", "작업 폴더의 파일", files]
    if s["notes"]:
        lines += ["", "참고(LLM 검토 의견. 검사로 재현되지 않아 완료 판정에는 쓰지 않았다)", *("- " + n for n in s["notes"])]
    lines += ["", "이 결과는 AI가 생성했다. 검증한 범위는 위 완료 조건에 연결된 검사까지이고, 최종 판단은 사용자에게 있다."]
    await db.q(run.user, "insert into messages (session_id, role, content) values (%s, 'user', %s), (%s, 'assistant', %s)",
               (run.session_id, s["request"], run.session_id, "\n".join(lines)))
    await run.emit("run_done", {"ok": not failure, "error": failure, "unmet": unmet, "files": files.splitlines(), "report": "\n".join(lines)})
    await audit(run.user["id"], "run", {"run": str(run.id), "mode": "agent", "ok": not failure, **await run.used()})
    s["phase"] = "done"
    return s, "failed" if failure else "done"


PHASES = {"spec": spec, "plan": plan, "execute": execute, "verify": verify, "reconcile": reconcile}


async def advance(run, s: dict) -> tuple[dict, str]:
    """상태 기계를 한 칸 움직이고 (새 상태, 실행 상태)를 돌려준다. 예산을 넘으면 거기서 멈춘다."""
    tokens = (await db.q(run.user, "select tokens from runs where id = %s", (run.id,)))[0]["tokens"]
    if s["elapsed"] > BUDGET["minutes"] * 60 or tokens > BUDGET["tokens"]:
        return await finish(run, s, "예산(실행 시간 또는 토큰)을 넘었습니다")
    started = time.monotonic()
    s, status = await PHASES[s["phase"]](run, await modes.candidates(run, s["model"]), s)
    s["elapsed"] += time.monotonic() - started
    return s, status
