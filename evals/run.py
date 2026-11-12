"""평가 러너. 실행 중인 플랫폼에 평가셋을 보내고 코드 채점기로 채점해 점수표를 낸다.

  python -m evals.run --url http://localhost:8000 --user <아이디> --password <비밀번호>
                      [--model <모델 ID>] [--only <유형>] [--baseline evals/results/<파일>.json]

모델, 프롬프트, 양자화, 서빙 설정을 바꿀 때마다 전체를 다시 돌려 직전 결과(--baseline)와 비교한다.
어느 유형이든 점수가 TOLERANCE보다 많이 떨어지면 종료 코드 1로 끝난다. 그 변경은 반영하지 않는다.
"""
import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import httpx

from .cases import CASES, FILES

TOLERANCE = 0.05     # 출발값. 같은 설정을 여러 번 돌려 본 점수의 흔들림을 잰 뒤 고친다


def events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        type, data = block.split("\n", 1)
        out.append((type.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


def run_case(client: httpx.Client, case: dict, model: str) -> dict:
    """문항 하나를 새 세션에서 실행하고 채점한다. Agent 문항은 계획을 그대로 승인한다."""
    started = time.monotonic()
    result = {"id": case["id"], "type": case["type"], "ok": False, "error": None}
    try:
        sid = client.post("/api/sessions", json={"title": case["id"], "grade": "internal"}).raise_for_status().json()["id"]
        for name in case["files"]:
            client.put(f"/api/sessions/{sid}/files/{name}", content=(FILES / name).read_bytes(), params={"grade": "internal"}).raise_for_status()
        evs = events(client.post("/api/chat", json={"session_id": sid, "message": case["prompt"], "mode": case["mode"],
                                                    "model": model, "images": case["images"]}).raise_for_status().text)
        while evs[-1] == ("status", {"status": "waiting"}):
            run_id = evs[0][1]["id"]
            client.post(f"/api/runs/{run_id}/control", json={"action": "approve"}).raise_for_status()
            last = max(d.get("event_id", 0) for _, d in evs)
            evs += events(client.get(f"/api/runs/{run_id}/events", params={"after": last}).text)
        answer = ""
        for type, d in evs:
            if type == "token":
                answer = "" if d.get("reset") else answer + d["text"]
            if type == "run_done":
                answer = d.get("report") or answer
                result["error"] = d.get("error")
        result["ok"] = bool(case["grade"](answer, evs, lambda path: client.get(f"/api/sessions/{sid}/files/{path}").raise_for_status().content))
        calls = [d for t, d in evs if t == "llm_call"]
        tools = [d for t, d in evs if t == "tool_result"]
        result |= {"ttft_ms": calls[0]["ttft_ms"] if calls else None,
                   "tokens": sum(c["prompt_tokens"] + c["completion_tokens"] for c in calls),
                   "tool_calls": len(tools), "tool_valid": sum(t["valid"] for t in tools), "tool_ok": sum(t["ok"] for t in tools),
                   "fixes": sum(t in ("issue", "fix") for t, _ in evs)}
    except Exception as e:      # 한 문항의 오류가 전체 평가를 멈추지 않게 한다. 오류는 실패로 센다
        result["error"] = f"{type(e).__name__}: {e}"
    result["seconds"] = round(time.monotonic() - started, 2)
    return result


def scoreboard(results: list[dict]) -> dict:
    groups = defaultdict(list)
    for r in results:
        groups[r["type"]].append(r)
    print(f"\n{'유형':<10}{'통과':>8}{'점수':>7}{'평균 시간':>10}{'첫 토큰':>9}{'평균 토큰':>10}{'도구 형식/성공/전체':>18}{'수정':>5}")
    scores = {}
    for type, rows in groups.items():
        done = [r for r in rows if "tokens" in r]
        scores[type] = round(sum(r["ok"] for r in rows) / len(rows), 3)
        mean = lambda key: sum(r[key] or 0 for r in done) / max(1, len(done))       # noqa: E731
        tools = "/".join(str(sum(r[k] for r in done)) for k in ("tool_valid", "tool_ok", "tool_calls"))
        print(f"{type:<12}{sum(r['ok'] for r in rows):>5}/{len(rows):<3}{scores[type]:>8.2f}{sum(r['seconds'] for r in rows) / len(rows):>11.1f}s"
              f"{mean('ttft_ms'):>10.0f}ms{mean('tokens'):>12.0f}{tools:>20}{sum(r['fixes'] for r in done):>6}")
    return scores


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--model", default="auto")
    ap.add_argument("--only")
    ap.add_argument("--baseline")
    args = ap.parse_args()
    client = httpx.Client(base_url=args.url, timeout=3600)
    client.post("/api/login", json={"id": args.user, "password": args.password}).raise_for_status()
    results = []
    for case in CASES:
        if args.only in (None, case["type"]):
            results.append(run_case(client, case, args.model))
            print(("통과 " if results[-1]["ok"] else "실패 ") + case["id"] + (f"  ({results[-1]['error']})" if results[-1]["error"] else ""), flush=True)
    scores = scoreboard(results)
    out = Path(__file__).parent / "results" / f"{args.model}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"model": args.model, "scores": scores, "cases": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n결과 저장: {out}")
    if args.baseline:
        base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))["scores"]
        dropped = {t: (base[t], s) for t, s in scores.items() if t in base and s < base[t] - TOLERANCE}
        for t, (before, after) in dropped.items():
            print(f"하락: {t} {before:.2f} → {after:.2f}")
        sys.exit(1 if dropped else 0)


if __name__ == "__main__":
    main()
