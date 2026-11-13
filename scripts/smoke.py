"""실제 모델을 붙인 플랫폼을 Quick → Thinking → Agent 순으로 한 번씩 돌려 보는 점검 스크립트.

  python3 scripts/smoke.py --user hong --password 'S3cret!' [--url http://localhost:8000] [--only quick,thinking,agent]

표준 라이브러리만 쓰므로 WSL의 기본 python3로 바로 돌린다. 모드마다 걸린 시간, 첫 토큰 시간, 토큰 수, 도구 호출,
답의 앞부분을 출력한다. 하나라도 실패하면 종료 코드 1. 답의 품질은 판정하지 않는다(그건 evals.run이 한다).
"""
import argparse
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request

CASES = {
    "quick": ("quick", "대한민국의 수도와 그 도시의 대표적인 강 이름을 한 문장으로 알려줘."),
    "thinking": ("thinking", "원금 1,000만 원을 연 3.5% 복리로 7년 두면 얼마가 되는지 코드를 실행해 계산하고, 계산식과 함께 답해줘."),
    "agent": ("agent", "문자열이 회문이면 True를 돌려주는 함수 is_palindrome(text)를 palindrome.py에 만들어줘. 공백과 대소문자는 무시한다."),
}


class Client:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method: str, path: str, body=None, timeout=3600) -> str:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data, {"content-type": "application/json"}, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return resp.read().decode()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path} → {e.code} {e.read().decode()[:300]}") from None


def events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        if block:
            type, data = block.split("\n", 1)
            out.append((type.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


def run(client: Client, name: str, mode: str, prompt: str) -> bool:
    print(f"\n=== {name} ===  {prompt}", flush=True)
    started = time.monotonic()
    sid = json.loads(client.call("POST", "/api/sessions", {"title": f"smoke-{name}", "grade": "internal"}))["id"]
    evs = events(client.call("POST", "/api/chat", {"session_id": sid, "message": prompt, "mode": mode}))
    while evs and evs[-1] == ("status", {"status": "waiting"}):       # Agent는 계획 승인을 기다린다
        run_id = evs[0][1]["id"]
        plan = next((d for t, d in reversed(evs) if t == "plan"), {})
        print("  계획:", json.dumps(plan, ensure_ascii=False)[:400], flush=True)
        client.call("POST", f"/api/runs/{run_id}/control", {"action": "approve"})
        last = max(d.get("event_id", 0) for _, d in evs)
        evs += events(client.call("GET", f"/api/runs/{run_id}/events?after={last}"))
    answer, error, files = "", None, []
    for type, d in evs:
        if type == "token":
            answer = "" if d.get("reset") else answer + d["text"]
        elif type == "mode":
            print("  모드:", d, flush=True)
        elif type == "tool_result":
            print(f"  도구: {d.get('tool')} ok={d.get('ok')} valid={d.get('valid')}", flush=True)
        elif type == "run_done":
            answer, error = d.get("report") or answer, d.get("error")
    if mode == "agent":
        files = json.loads(client.call("GET", f"/api/sessions/{sid}/files"))
    calls = [d for t, d in evs if t == "llm_call"]
    status = next((d["status"] for t, d in reversed(evs) if t == "status"), "done")
    print(f"  상태: {status}  걸린 시간: {time.monotonic() - started:.0f}s  LLM 호출: {len(calls)}회"
          f"  첫 토큰(첫 호출): {calls[0]['ttft_ms'] / 1000 if calls else '-'}s"
          f"  토큰(입력/출력): {sum(c['prompt_tokens'] for c in calls)}/{sum(c['completion_tokens'] for c in calls)}")
    if files:
        print("  작업 폴더:", files)
    print("  답:", (answer or "").strip().replace("\n", " ")[:500])
    if error:
        print("  오류:", error)
    return bool(answer.strip()) and not error and status not in ("failed", "cancelled")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--only", default="quick,thinking,agent")
    args = ap.parse_args()
    client = Client(args.url)
    for attempt in range(30):       # 막 띄운 API 서버는 몇 초 뒤에 연결을 받는다
        try:
            client.call("POST", "/api/login", {"id": args.user, "password": args.password})
            break
        except OSError:
            if attempt == 29:
                raise
            time.sleep(2)
    results = {}
    for name in args.only.split(","):
        try:
            results[name] = run(client, name, *CASES[name])
        except Exception as e:      # 한 모드의 실패가 다음 모드 점검을 막지 않게 한다
            print(f"  실패: {type(e).__name__}: {e}")
            results[name] = False
    print("\n결과:", "  ".join(f"{k}={'통과' if v else '실패'}" for k, v in results.items()))
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
