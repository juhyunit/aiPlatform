"""Agent Runtime. 한 역할이 한 단계를 도구를 써서 수행하는 루프다.

무엇을 어떤 인자로 부를지는 모델이 정한다. 다음 단계와 종료는 여기서 정하지 않는다(Workflow Engine의 일).
단계가 끝나면 이 메시지 목록은 버리고 결과 글만 돌려준다.
"""
import json

from . import context, gateway
from .policy import HARNESS, P
from .tools import TOOLS

MAX_TURNS = 12
PREVIEW = 1500          # 도구 결과가 이보다 길면 파일로 저장하고 앞부분만 컨텍스트에 둔다
ERROR_TAIL = 6000       # 오류 출력은 줄이지 않는다. 수정의 근거다. 이 길이를 넘을 때만 끝부분을 남긴다


async def _execute(run, allowed: list[str], call: dict, n: int) -> tuple[str, bool]:
    """도구 하나를 실행한다. 형식이 맞는 호출인지(valid)와 실행이 성공했는지(ok)를 따로 기록한다."""
    name, valid, ok = call["name"], False, False
    try:
        args = json.loads(call["arguments"])
        tool = TOOLS[name] if name in allowed else None
        valid = bool(tool) and isinstance(args, dict) and set(tool["inputSchema"]["required"]) <= set(args) <= set(tool["inputSchema"]["properties"])
    except json.JSONDecodeError:
        args = call["arguments"]
    await run.emit("tool_call", {"tool": name, "arguments": args})
    if not valid:
        result = f"잘못된 도구 호출이다. 쓸 수 있는 도구: {', '.join(allowed)}. 인자는 도구 정의의 JSON 스키마를 따른다."
    else:
        try:
            result, ok = await tool["fn"](run, **args), True
        except Exception as e:
            result = f"오류: {e}"[-ERROR_TAIL:]
    if ok and len(result) > PREVIEW:
        path = f".agent/tool-{run.id}-{n}.txt"
        await TOOLS["write_file"]["fn"](run, path, result)
        result = result[:PREVIEW] + f"\n… (전체 {len(result)}자. read_file로 {path} 를 읽을 수 있다)"
    run.evidence.append(result)
    await run.emit("tool_result", {"tool": name, "valid": valid, "ok": ok, "preview": result[:300]})
    return result, ok


def _shrink(messages: list[dict], budget: int):
    """컨텍스트가 넘치면 오래된 성공 결과부터 비운다. 오류 출력은 남긴다."""
    for m in messages:
        if sum(context.tokens(x["content"]) for x in messages) <= budget:
            return
        if m["role"] == "tool" and not m["content"].startswith("오류"):
            m["content"] = "(오래된 도구 결과는 비웠다)"


async def run_step(run, cands: list[dict], messages: list[dict], tools: list[str], *, mode: str,
                   reasoning: str = "off", state: str = "", on_token=None) -> str:
    """도구 호출이 없는 응답이 나올 때까지 돈다. 횟수 상한에 닿으면 도구 없이 한 번 더 불러 답을 받는다."""
    defs = [TOOLS[name] for name in tools]
    max_tokens = P["max_output"][mode]
    budget = cands[0]["capabilities"]["context"] - max_tokens
    for turn in range(MAX_TURNS + 1):
        sent = messages
        if state and HARNESS["state_reinjection"]:      # 작업 상태를 매 호출 끝에 다시 넣는다
            sent = [*messages[:-1], {**messages[-1], "content": messages[-1]["content"] + "\n\n[작업 상태]\n" + state}]
        out = await gateway.chat(run, cands, sent, tools=defs if turn < MAX_TURNS else None, reasoning=reasoning,
                                 max_tokens=max_tokens, on_token=on_token)
        if not out["tool_calls"]:
            return out["content"]
        messages.append({"role": "assistant", "content": out["content"], "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
            for c in out["tool_calls"]]})
        for n, call in enumerate(out["tool_calls"]):
            result, _ = await _execute(run, tools, call, turn * 10 + n)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        _shrink(messages, budget)
    return out["content"]
