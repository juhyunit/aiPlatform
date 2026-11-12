"""테스트용 가짜 모델 서버. OpenAI 호환 API의 모양만 흉내 낸다.

/script 로 넣어 둔 응답을 순서대로 돌려주고, 없으면 마지막 사용자 발화를 되돌려준다.
모델 이름은 주소의 첫 마디다(/a/v1, /b/v1). /down 으로 특정 모델을 장애 상태로 만든다.
"""
import asyncio
import json
import math
import os
import zlib

from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

app = FastAPI()
script: list[dict] = []     # {"content": ...} | {"tool": 이름, "arguments": {...}} | {"json": {...}}
calls: list[dict] = []      # 받은 요청 본문
down: set[str] = set()
DELAY = float(os.environ.get("FAKE_DELAY", 0))    # 조각 사이 지연(부하 테스트용)


@app.post("/script")
async def set_script(request: Request):
    script[:] = await request.json()
    calls.clear()


@app.get("/calls")
async def get_calls():
    return calls


@app.post("/down")
async def set_down(request: Request):
    down.clear()
    down.update(await request.json())


@app.post("/{name}/v1/chat/completions")
async def chat(name: str, request: Request):
    body = await request.json()
    if name in down:
        return Response(status_code=503)
    calls.append({"model": name, **body})
    item = script.pop(0) if script else {"content": "echo: " + str(body["messages"][-1]["content"])[:200]}
    constrained = "anyOf" in body.get("response_format", {}).get("json_schema", {}).get("schema", {})
    if "json" in item:
        item = {"content": json.dumps(item["json"], ensure_ascii=False)}
    elif constrained and "tool" in item:
        item = {"content": json.dumps({"tool": item["tool"], "arguments": item["arguments"]}, ensure_ascii=False)}
    elif constrained:
        item = {"content": json.dumps({"answer": item["content"]}, ensure_ascii=False)}

    if "tool" in item:      # 인자를 두 조각으로 나눠 보낸다
        args = json.dumps(item["arguments"], ensure_ascii=False)
        deltas = [{"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": item["tool"], "arguments": args[:5]}}]},
                  {"tool_calls": [{"index": 0, "function": {"arguments": args[5:]}}]}]
        finish = "tool_calls"
    else:
        text = item["content"]
        deltas = [{"content": text[i:i + 8]} for i in range(0, len(text), 8)]
        finish = item.get("finish", "stop")

    async def stream():
        for delta in deltas:
            await asyncio.sleep(DELAY)
            yield "data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}) + "\n\n"
        yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}) + "\n\n"
        yield "data: " + json.dumps({"choices": [], "usage": {
            "prompt_tokens": len(json.dumps(body["messages"])) // 3, "completion_tokens": len(deltas)}}) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/{name}/v1/embeddings")
async def embeddings(name: str, request: Request):
    """글자 2-gram을 1024칸에 세어 넣은 벡터. 같은 글자가 많이 겹치는 글끼리 가깝다."""
    data = []
    for text in (await request.json())["input"]:
        v = [0.0] * 1024
        for i in range(len(text) - 1):
            v[zlib.crc32(text[i:i + 2].encode()) % 1024] += 1
        norm = math.sqrt(sum(x * x for x in v)) or 1
        data.append({"embedding": [x / norm for x in v]})
    return {"data": data}
