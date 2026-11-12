"""부하 테스트. Quick, Thinking, Agent를 섞은 시나리오로 동시 사용자를 단계적으로 늘린다. 목표는 500명이다.

  locust -f loadtest/locustfile.py --host http://localhost:8000 --headless -u 500 -r 10 -t 15m

여기서 동시 사용자 1명은 로그인해 대화 중인 사람 1명이다. 답을 읽고 다음 질문을 쓰는 시간(20~40초)이 있으므로
같은 순간에 모델이 처리하는 요청 수는 사용자 수보다 훨씬 적다. 모드 비율(80:15:5)과 생각 시간은 출발값이고,
운영 로그가 쌓이면 실제 분포로 바꾼다. 계정 load1 … load500(비밀번호 loadtest)은 미리 만들어 둔다(README).
요청 통계의 TTFT 행은 첫 토큰이 화면에 도착하기까지의 시간이다.
"""
import itertools
import json
import time

from locust import HttpUser, between, events, task

_ids = itertools.count(1)


class Staff(HttpUser):
    wait_time = between(20, 40)

    def on_start(self):
        self.client.post("/api/login", json={"id": f"load{next(_ids)}", "password": "loadtest"})
        self.sid = self.client.post("/api/sessions", json={"title": "부하 테스트", "grade": "internal"}).json()["id"]

    def follow(self, response, mode: str, started: float) -> tuple[str | None, str | None]:
        """이벤트 스트림을 끝까지 읽는다. (run id, 마지막으로 본 종료 이벤트)를 돌려주고 첫 토큰 시간을 기록한다."""
        run, end, kind, first = None, None, None, False
        for line in response.iter_lines():
            line = line.decode()
            if line.startswith("event: "):
                kind = line[7:]
                end = kind if kind in ("run_done", "approval_required") else end
                if kind == "token" and not first:
                    first = True
                    events.request.fire(request_type="TTFT", name=mode, response_time=(time.monotonic() - started) * 1000,
                                        response_length=0, exception=None, context={})
            elif kind == "run" and line.startswith("data: "):
                run = json.loads(line[6:])["id"]
        return run, end

    def chat(self, mode: str, message: str):
        started = time.monotonic()
        with self.client.post("/api/chat", json={"session_id": self.sid, "message": message, "mode": mode},
                              stream=True, name=mode, catch_response=True) as r:
            if r.status_code != 200:
                return r.failure(f"HTTP {r.status_code}")
            run, end = self.follow(r, mode, started)
            if end is None:
                return r.failure("끝나지 않은 응답")
        if end == "approval_required":      # Agent: 계획을 승인하고 끝까지 따라간다
            self.client.post(f"/api/runs/{run}/control", json={"action": "approve"}, name="agent 승인")
            with self.client.get(f"/api/runs/{run}/events", stream=True, name="agent 실행", catch_response=True) as r:
                if self.follow(r, "agent 실행", time.monotonic())[1] != "run_done":
                    r.failure("끝나지 않은 작업")

    @task(80)
    def quick(self):
        self.chat("quick", "여신과 수신의 차이를 두 문장으로 설명해줘")

    @task(15)
    def thinking(self):
        self.chat("thinking", "원금 1,000,000원을 연 5% 단리로 3년 맡기면 이자가 얼마인지 계산해줘")

    @task(5)
    def agent(self):
        self.chat("agent", "두 수를 더하는 함수 add를 calc.py에 만들어줘")
