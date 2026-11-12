"""Model Gateway. 모델로 가는 유일한 길이다.

정책 필터(거부권) → 라우터(가장 가벼운 합격 모델) → 어댑터(OpenAI 호환) → 장애 전환·계측 순서로 처리한다.
"""
import json
import os
import re
import time
from pathlib import Path

import httpx
import yaml

from .policy import CONFIG, P, rank

MODELS: list[dict] = yaml.safe_load(
    Path(os.environ.get("MODELS_FILE", CONFIG / "models.yaml")).read_text(encoding="utf-8"))["models"]
BLOCK_SECONDS = 30
_blocked: dict[str, float] = {}     # 모델 ID → 차단이 풀리는 시각
_http = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=5))
_THINK = re.compile(r"<think>.*?</think>\s*", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.S)


class Denied(Exception):
    """정책이 허용한 모델이 없다."""


def parse_json(text: str):
    """모델이 JSON 앞뒤에 붙인 <think> 블록이나 코드 블록 표시를 걷어내고 파싱한다."""
    cleaned = _FENCE.sub("", _THINK.sub("", text.strip()).strip())
    return json.loads(cleaned)


def clearance(model: dict) -> int:
    """모델이 받을 수 있는 최고 등급. 실행 환경의 상한을 넘지 못한다."""
    return min(rank(model["clearance"]), rank(P["env_max"][model["env"]]))


def select(grade: int, model: str = "auto", task: str = "chat", need: tuple = ()) -> list[dict]:
    """데이터 등급으로 허용 집합을 구하고, 그 안에서 가벼운 순으로 후보를 돌려준다. 뒤의 후보는 장애 전환용이다."""
    kind = "embedding" in need
    cands = [m for m in MODELS
             if m["status"] == "active" and grade <= clearance(m)
             and ("embedding" in m["capabilities"]) == kind
             and all(m["capabilities"].get(c) for c in need)]
    if model != "auto":      # 사용자가 지정한 모델은 허용 집합에 있는지만 확인한다
        cands = [m for m in cands if m["id"] == model]
    else:
        line = P["pass_line"].get(task, 0)
        cands = sorted((m for m in cands if m["eval"].get(task, 0) >= line),
                       key=lambda m: (m["resources"]["vram_gb"], -m["eval"].get(task, 0)))
    if not cands:
        raise Denied(f"데이터 등급 '{P['grades'][grade]}'에 허용된 모델이 없습니다")
    return cands


def _tool_schema(tools: list[dict]) -> dict:
    """도구 호출을 믿을 수 없는 모델용. 도구 하나를 고르거나 최종 답을 내는 JSON만 나오게 제약한다."""
    calls = [{"type": "object", "required": ["tool", "arguments"], "additionalProperties": False,
              "properties": {"tool": {"const": t["name"]}, "arguments": t["inputSchema"]}} for t in tools]
    final = {"type": "object", "required": ["answer"], "additionalProperties": False,
             "properties": {"answer": {"type": "string"}}}
    return {"anyOf": calls + [final]}


def _as_text(messages: list[dict], tools: list[dict]) -> list[dict]:
    """표준 메시지(도구 호출·도구 결과)를 도구 형식이 없는 모델이 읽을 수 있는 글로 바꾼다."""
    guide = "\n\n도구 목록:\n" + "\n".join(f"- {t['name']}: {t['description']}" for t in tools) + \
        '\n도구를 쓰려면 {"tool": 이름, "arguments": {...}}, 답이 준비되면 {"answer": "..."} 형식의 JSON만 출력한다.'
    out = []
    for m in messages:
        if m["role"] == "system":
            out.append({"role": "system", "content": m["content"] + guide})
        elif m.get("tool_calls"):
            for tc in m["tool_calls"]:
                call = tc["function"]
                try:
                    args = json.loads(call["arguments"])
                except json.JSONDecodeError:
                    args = call["arguments"]
                out.append({"role": "assistant", "content": json.dumps(
                    {"tool": call["name"], "arguments": args}, ensure_ascii=False)})
        elif m["role"] == "tool":
            out.append({"role": "user", "content": "[도구 결과]\n" + m["content"]})
        else:
            out.append(m)
    return out


async def _call(model: dict, messages, tools, schema, reasoning, max_tokens, on_token) -> dict:
    caps = model["capabilities"]
    body = {"model": model["id"], "messages": messages, "max_tokens": max_tokens, "stream": True,
            "stream_options": {"include_usage": True}, **caps.get("reasoning", {}).get(reasoning, {})}
    constrained = tools and caps["tools"] == "schema"
    if constrained:
        body["messages"], schema = _as_text(messages, tools), _tool_schema(tools)
    elif tools:
        body["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["inputSchema"]}} for t in tools]
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}

    started, first, content, calls, usage, finish = time.monotonic(), None, "", {}, {}, None
    async with _http.stream("POST", model["endpoint"] + "/chat/completions", json=body) as resp:
        if resp.status_code >= 400:
            await resp.aread()
            resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            usage = chunk.get("usage") or usage
            for choice in chunk.get("choices") or []:
                delta, finish = choice.get("delta") or {}, choice.get("finish_reason") or finish
                if delta.get("reasoning_content"):
                    first = first or time.monotonic()
                if delta.get("content"):
                    first = first or time.monotonic()
                    content += delta["content"]
                    if on_token and not schema:
                        await on_token(delta["content"])
                for tc in delta.get("tool_calls") or []:      # 도구 호출은 조각으로 나뉘어 온다
                    first = first or time.monotonic()
                    fn = tc.get("function") or {}
                    call = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    call["id"] += tc.get("id") or ""
                    call["name"] += fn.get("name") or ""
                    call["arguments"] += fn.get("arguments") or ""

    tool_calls = [{**c, "id": c["id"] or f"call_{i}"} for i, c in sorted(calls.items())]
    if constrained:
        out = parse_json(content)
        content = out.get("answer", "")
        if "tool" in out:
            tool_calls = [{"id": "call_0", "name": out["tool"],
                           "arguments": json.dumps(out["arguments"], ensure_ascii=False)}]
        if on_token and content:
            await on_token(content)
    now = time.monotonic()
    return {"content": content, "tool_calls": tool_calls, "finish": finish, "model": model["id"],
            "prompt_tokens": usage.get("prompt_tokens", 0), "completion_tokens": usage.get("completion_tokens", 0),
            "ttft_ms": int(((first or now) - started) * 1000), "total_ms": int((now - started) * 1000)}


async def chat(run, cands: list[dict], messages: list[dict], *, max_tokens: int, tools=None, schema=None,
               reasoning: str = "off", on_token=None) -> dict:
    """후보 순서대로 호출한다. 연결 실패나 서버 오류가 난 모델은 잠시 차단하고 다음 후보로 넘긴다."""
    failed = []
    for model in cands:
        if _blocked.get(model["id"], 0) > time.time():
            failed.append(model["id"])
            continue
        sent = []

        async def forward(text):
            sent.append(text)
            await on_token(text)

        try:
            out = await _call(model, messages, tools, schema, reasoning, max_tokens, forward if on_token else None)
        except httpx.HTTPError as e:
            client_error = isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500
            if client_error or sent:     # 요청이 잘못됐거나 이미 일부를 내보냈으면 전환하지 않는다
                raise
            _blocked[model["id"]] = time.time() + BLOCK_SECONDS
            failed.append(model["id"])
            continue
        out["failover_from"] = failed
        await run.emit("llm_call", {k: v for k, v in out.items() if k not in ("content", "tool_calls")})
        await run.add_tokens(out["prompt_tokens"] + out["completion_tokens"])
        return out
    raise RuntimeError("응답할 수 있는 모델 서버가 없습니다: " + ", ".join(failed))


async def embed(texts: list[str], grade: int) -> tuple[str, list[list[float]]]:
    """임베딩. 생성 모델과 다른 프로세스의 임베딩 서버를 같은 정책 필터를 거쳐 부른다."""
    cands = select(grade, need=("embedding",))
    if not texts:
        return cands[0]["id"], []
    failed = []
    for model in cands:
        if _blocked.get(model["id"], 0) > time.time():
            failed.append(model["id"])
            continue
        try:
            vectors = []
            for i in range(0, len(texts), 32):
                resp = await _http.post(model["endpoint"] + "/embeddings",
                                        json={"model": model["id"], "input": texts[i:i + 32]})
                resp.raise_for_status()
                vectors += [d["embedding"] for d in resp.json()["data"]]
            return model["id"], vectors
        except httpx.HTTPError as e:
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
                raise
            _blocked[model["id"]] = time.time() + BLOCK_SECONDS
            failed.append(model["id"])
    raise RuntimeError("응답할 수 있는 임베딩 서버가 없습니다: " + ", ".join(failed))
