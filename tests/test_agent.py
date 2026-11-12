"""Agent 모드: 명세 → 계획 → 승인 → 실행 → 검증 → 요구사항 대조. 관문은 테스트와 규칙 검사가 연다."""
import time

from conftest import WORKER, events, model_calls, of, script, start_worker

GOOD = "def add(a, b):\n    return a + b\n"
BAD = "def add(a, b):\n    return a - b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
PYTEST = "python -m pytest -q -p no:cacheprovider tests/test_calc.py"
SPEC = {"json": {"template": "dev", "criteria": ["add(a, b)는 두 수의 합을 돌려준다"]}}
PLAN = {"json": {"steps": [{"task": "calc.py에 add를 만들고 tests/test_calc.py에 테스트를 쓴다", "criteria": ["C1"], "check": PYTEST}]}}
NO_ISSUES = {"json": {"issues": []}}


def write(path, content):
    return {"tool": "write_file", "arguments": {"path": path, "content": content}}


def start(client, sid, *items, request="두 수를 더하는 함수를 만들어줘"):
    """요청을 보내 승인 대기까지 간 뒤 run id를 돌려준다."""
    script(*items)
    evs = client.chat(sid, request, mode="agent")
    assert evs[-1] == ("status", {"status": "waiting"}) and of(evs, "approval_required")
    return evs[0][1]["id"], evs


def test_dev_task_passes_both_gates(alice):
    sid = alice.session()
    run, evs = start(alice, sid, SPEC, PLAN, write("calc.py", GOOD), write("tests/test_calc.py", TEST), {"content": "함수와 테스트를 만들었다."}, NO_ISSUES)
    plan = of(evs, "plan")[0]
    assert plan["criteria"] == [{"id": "C1", "text": "add(a, b)는 두 수의 합을 돌려준다"}] and plan["steps"][0]["check"] == PYTEST
    evs = alice.control(run, "approve")
    assert [d["check"] for d in of(evs, "check_result")] == [PYTEST, "전체 테스트", "린트"] and all(d["ok"] for d in of(evs, "check_result"))
    assert of(evs, "step_started")[0]["profile"] == "Worker" and of(evs, "step_done")[0]["summary"] == "함수와 테스트를 만들었다."
    done = of(evs, "run_done")[0]
    assert done["ok"] and done["unmet"] == [] and [f.split(" ")[0] for f in done["files"]] == ["calc.py", "tests/test_calc.py"]
    assert f"✓ C1. add(a, b)는 두 수의 합을 돌려준다 (근거: {PYTEST})" in done["report"] and "AI가 생성" in done["report"]
    assert alice.get(f"/api/sessions/{sid}/files/calc.py").text == GOOD
    assert alice.get(f"/api/sessions/{sid}/messages").json()[-1]["content"] == done["report"]
    assert alice.status(run) == "done"


def test_failed_check_goes_back_to_execution_with_the_error_output(alice):
    run, _ = start(alice, alice.session(), SPEC, PLAN, write("calc.py", BAD), write("tests/test_calc.py", TEST), {"content": "1차"},
                   write("calc.py", GOOD), {"content": "고쳤다."}, NO_ISSUES)
    evs = alice.control(run, "approve")
    assert [d["ok"] for d in of(evs, "check_result")] == [False, True, True, True]
    assert of(evs, "issue")[0]["problems"] == [f"검사 실패: {PYTEST}"] and of(evs, "run_done")[0]["ok"]
    retry = next(c for c in model_calls() if "앞선 시도에서 아래 검사가 실패했다" in str(c["messages"][1]["content"]))
    assert "assert add(2, 3) == 5" in retry["messages"][1]["content"]         # 실패 출력은 줄이지 않고 준다
    assert retry["messages"][-1]["content"].endswith(f"통과해야 하는 검사: {PYTEST}\n관련 완료 조건: C1")   # 작업 상태 재주입


def test_budget_exhaustion_is_reported_as_incomplete_not_done(alice):
    run, _ = start(alice, alice.session(), SPEC, PLAN, write("calc.py", BAD), write("tests/test_calc.py", TEST),
                   {"content": "1"}, {"content": "2"}, {"content": "3"})
    done = of(alice.control(run, "approve"), "run_done")[0]
    assert not done["ok"] and done["unmet"] == ["C1"] and "수정 2회 뒤에도 실패" in done["error"]
    assert "미완료" in done["report"] and "✗ C1" in done["report"] and alice.status(run) == "failed"


def test_reviewer_issue_counts_only_when_its_check_reproduces(alice):
    abs_add = "def add(a, b):\n    return abs(a) + abs(b)\n"
    review = {"json": {"issues": [
        {"problem": "음수를 더하면 틀린다", "check": "from calc import add\nassert add(-1, 1) == 0"},
        {"problem": "함수에 설명이 없다", "check": "pass"}]}}
    run, _ = start(alice, alice.session(), SPEC, PLAN, write("calc.py", abs_add), write("tests/test_calc.py", TEST), {"content": "1차"},
                   review, write("calc.py", GOOD), {"content": "고쳤다."}, NO_ISSUES)
    evs = alice.control(run, "approve")
    issues = [d for d in of(evs, "issue") if d.get("profile") == "Reviewer"]
    assert [(d["problems"][0], d["confirmed"]) for d in issues] == [("음수를 더하면 틀린다", True), ("함수에 설명이 없다", False)]
    done = of(evs, "run_done")[0]
    assert done["ok"] and "참고(LLM 검토 의견" in done["report"] and "- 함수에 설명이 없다" in done["report"]
    reviewer_call = next(c for c in model_calls() if "Reviewer" in c["messages"][0]["content"])
    assert len(reviewer_call["messages"]) == 2 and "--- calc.py ---" in reviewer_call["messages"][1]["content"]   # 새 컨텍스트, 결과물만


def test_plan_must_cover_every_criterion(alice):
    spec = {"json": {"template": "dev", "criteria": ["조건 하나", "조건 둘"]}}
    script(spec, PLAN, PLAN)        # 두 번 모두 C2를 빠뜨린 계획
    evs = alice.chat(alice.session(), "만들어줘", mode="agent")
    assert not of(evs, "run_done")[0]["ok"] and "C2" in of(evs, "run_done")[0]["error"] and not of(evs, "approval_required")
    assert "조건 C2을 어느 단계에도 연결하지 않았다" in model_calls()[-1]["messages"][1]["content"]


def test_editing_criteria_at_approval_replans(alice):
    plan2 = {"json": {"steps": [{"task": "calc.py와 테스트", "criteria": ["C1", "C2"], "check": PYTEST}]}}
    run, _ = start(alice, alice.session(), SPEC, PLAN, plan2, write("calc.py", GOOD), write("tests/test_calc.py", TEST), {"content": "끝"}, NO_ISSUES)
    evs = alice.control(run, "approve", criteria=["add는 합을 돌려준다", "테스트가 있다"])
    assert len(of(evs, "plan")) == 2 and len(of(evs, "approval_required")) == 1        # 고친 것이 곧 승인이다
    assert "✓ C2. 테스트가 있다" in of(evs, "run_done")[0]["report"]


def test_cancel_and_ownership(alice, bob):
    run, _ = start(alice, alice.session(), SPEC, PLAN)
    assert bob.post(f"/api/runs/{run}/control", json={"action": "cancel"}).status_code == 404
    assert bob.get(f"/api/runs/{run}/events").status_code == 404
    assert alice.post(f"/api/runs/{run}/control", json={"action": "pause"}).status_code == 409      # 승인 대기 중에는 정지할 것이 없다
    alice.control(run, "cancel")
    assert alice.status(run) == "cancelled"


def slow(seconds):
    return {"tool": "run_python", "arguments": {"code": f"import time\ntime.sleep({seconds})\nprint('ok')"}}


def wait_for(fn, seconds=40):
    for _ in range(seconds * 5):
        if fn():
            return
        time.sleep(0.2)
    raise AssertionError("기다리던 상태가 되지 않았습니다")


def test_pause_stops_at_step_boundary_and_resume_continues_from_checkpoint(alice):
    plan = {"json": {"steps": [{"task": "calc.py", "criteria": ["C1"], "check": "test -f calc.py"},
                               {"task": "테스트", "criteria": ["C1"], "check": PYTEST}]}}
    run, _ = start(alice, alice.session(), SPEC, plan, slow(3), write("calc.py", GOOD), {"content": "1단계 끝"},
                   write("tests/test_calc.py", TEST), {"content": "2단계 끝"}, NO_ISSUES)
    alice.post(f"/api/runs/{run}/control", json={"action": "approve"})
    wait_for(lambda: len(model_calls()) >= 3)                   # 1단계가 도는 중
    alice.post(f"/api/runs/{run}/control", json={"action": "pause"}).raise_for_status()
    wait_for(lambda: len(model_calls()) >= 5)                   # 돌던 단계는 끝까지 간다
    time.sleep(1.5)
    assert alice.status(run) == "paused" and len(model_calls()) == 5      # 2단계는 시작하지 않았다
    evs = alice.control(run, "resume")
    assert [d["step"] for d in of(evs, "step_done")] == ["S1", "S2"] and of(evs, "run_done")[0]["ok"]


def test_killed_worker_resumes_run_from_last_checkpoint(alice):
    plan = {"json": {"steps": [{"task": "calc.py", "criteria": ["C1"], "check": "test -f calc.py"},
                               {"task": "테스트", "criteria": ["C1"], "check": PYTEST}]}}
    run, _ = start(alice, alice.session(), SPEC, plan, write("calc.py", GOOD), {"content": "1단계 끝"}, slow(4),
                   write("tests/test_calc.py", TEST), {"content": "2단계 끝"}, NO_ISSUES)
    alice.post(f"/api/runs/{run}/control", json={"action": "approve"})
    wait_for(lambda: len(model_calls()) >= 5)                   # 2단계의 도구가 도는 중
    WORKER[0].kill()
    WORKER[0].wait()
    assert alice.status(run) == "running"
    start_worker()
    evs = events(alice.get(f"/api/runs/{run}/events").text)
    assert [d["step"] for d in of(evs, "step_done")] == ["S1", "S2"]       # 끝난 1단계는 다시 하지 않는다
    assert len(of(evs, "step_started")) == 3 and of(evs, "run_done")[0]["ok"]


def test_data_task_is_verified_by_rerunning_the_analysis(alice):
    sid = alice.session()
    alice.upload(sid, "매출.csv", "지점,매출\n강남,120\n종로,80\n".encode())
    analysis = "import json\nimport pandas as pd\n\ndf = pd.read_csv('매출.csv')\njson.dump({'합계': int(df['매출'].sum())}, open('result.json', 'w'), ensure_ascii=False)\n"
    spec = {"json": {"template": "data", "criteria": ["매출 합계를 result.json에 쓴다"]}}
    plan = {"json": {"steps": [{"task": "analysis.py 작성", "criteria": ["C1"], "check": "python analysis.py && test -s result.json"}]}}
    run, _ = start(alice, sid, spec, plan, write("analysis.py", analysis), {"content": "합계를 계산했다."}, NO_ISSUES, request="매출 합계 보고서를 만들어줘")
    evs = alice.control(run, "approve")
    assert of(evs, "check_result")[-1] == {**of(evs, "check_result")[-1], "check": "재실행 결과 일치", "ok": True}
    assert of(evs, "run_done")[0]["ok"] and alice.get(f"/api/sessions/{sid}/files/result.json").json() == {"합계": 200}


def test_doc_task_requires_citations_that_exist(alice):
    sid = alice.session()
    alice.upload(sid, "규정.txt", "일비는 출장 1일당 25,000원을 정액으로 지급한다.".encode())
    source = alice.chat(sid, "일비?", mode="quick")[-1][1]["sources"][0]["id"]
    spec = {"json": {"template": "doc", "criteria": ["일비 금액을 근거와 함께 답한다"]}}
    plan = {"json": {"steps": [{"task": "answer.md 작성", "criteria": ["C1"], "check": "test -s answer.md"}]}}
    run, _ = start(alice, sid, spec, plan, write("answer.md", "일비는 25,000원이다 [#99999999]."), {"content": "1차"},
                   write("answer.md", f"일비는 25,000원이다 [#{source}]."), {"content": "인용을 고쳤다."}, NO_ISSUES, request="일비 규정을 정리한 보고서를 만들어줘")
    evs = alice.control(run, "approve")
    checks = [(d["check"], d["ok"]) for d in of(evs, "check_result") if d["check"] == "인용 실재"]
    assert checks == [("인용 실재", False), ("인용 실재", True)]
    assert of(evs, "run_done")[0]["ok"] and f"일비는 25,000원이다 [#{source}]." in of(evs, "run_done")[0]["report"]


def test_auto_picks_agent_for_deliverables_and_asks_before_running(alice):
    script(SPEC, PLAN)
    evs = alice.chat(alice.session(), "두 수를 더하는 프로그램을 만들어줘")
    assert of(evs, "mode")[0]["mode"] == "agent" and of(evs, "approval_required") and not of(evs, "step_started")
