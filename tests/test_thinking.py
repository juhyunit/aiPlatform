"""Thinking 모드와 Agent Runtime: 도구 호출 루프, 샌드박스, 인용·수치 검증, Auto 판정과 승격."""
from conftest import model_calls, of, script, text_of


def test_tool_loop_runs_code_in_sandbox_and_uses_its_output(alice):
    script({"tool": "run_python", "arguments": {"code": "print(123 * 456)"}}, {"content": "답은 56088입니다."})
    evs = alice.chat(alice.session(), "123 곱하기 456을 계산해줘")
    assert of(evs, "mode")[0]["mode"] == "thinking"                  # Auto 규칙: 계산
    assert of(evs, "tool_call")[0] == {"tool": "run_python", "arguments": {"code": "print(123 * 456)"}}
    result = of(evs, "tool_result")[0]
    assert result["valid"] and result["ok"] and "56088" in result["preview"]
    assert of(evs, "check_result")[0]["ok"] and text_of(evs) == "답은 56088입니다."
    calls = model_calls()
    assert calls[0]["x_reasoning"] == "high" and [t["function"]["name"] for t in calls[0]["tools"]] == ["search", "list_files", "read_file", "run_python"]
    assert calls[1]["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": "56088\n"}


def test_schema_constrained_tool_calls_for_models_without_native_format(alice):
    script({"tool": "run_python", "arguments": {"code": "print(7 * 6)"}}, {"content": "42입니다."})
    evs = alice.chat(alice.session(), "7 곱하기 6을 계산해줘", model="big")
    assert of(evs, "tool_result")[0]["ok"] and text_of(evs) == "42입니다."
    first, second = model_calls()
    assert "tools" not in first and "anyOf" in first["response_format"]["json_schema"]["schema"]
    assert "run_python:" in first["messages"][0]["content"]
    assert [m["role"] for m in second["messages"][-2:]] == ["assistant", "user"] and "[도구 결과]\n42" in second["messages"][-1]["content"]


def test_number_without_evidence_gets_one_fix_round(alice):
    script({"content": "지난달 매출 합계는 987654원입니다."}, {"content": "매출 자료가 없어 합계를 확인하지 못했습니다."})
    evs = alice.chat(alice.session(), "지난달 매출 합계를 알려줘", mode="thinking")
    assert not of(evs, "check_result")[0]["ok"] and "987654" in of(evs, "issue")[0]["problems"][0]
    assert of(evs, "fix")[0]["ok"] and text_of(evs) == "매출 자료가 없어 합계를 확인하지 못했습니다."
    assert evs[-1][1]["ok"]


def test_citation_must_exist_in_given_sources(alice):
    sid = alice.session()
    alice.upload(sid, "규정.txt", "일비는 정액으로 지급한다.".encode())
    script({"content": "일비는 정액입니다 [#99999999]."}, {"content": "일비는 정액입니다 [#99999999]."})
    evs = alice.chat(sid, "일비 지급 방식은?", mode="thinking")
    assert "인용 [#99999999]" in of(evs, "issue")[0]["problems"][0]
    assert not evs[-1][1]["ok"]                                      # 고치지 못하면 통과로 표시하지 않는다
    source = evs[-1][1]["sources"][0]
    script({"content": f"일비는 정액입니다 [#{source['id']}]."})
    assert alice.chat(sid, "일비 지급 방식은?", mode="thinking")[-1][1]["ok"]


def test_invalid_tool_calls_are_reported_to_the_model_not_executed(alice):
    script({"tool": "delete_everything", "arguments": {}},
           {"tool": "read_file", "arguments": {"path": "../../../etc/passwd"}},
           {"tool": "run_python", "arguments": {"code": "import urllib.request\nurllib.request.urlopen('http://example.com', timeout=3)"}},
           {"content": "할 수 없습니다."})
    evs = alice.chat(alice.session(), "x", mode="thinking")
    unknown, escape, network = of(evs, "tool_result")
    assert not unknown["valid"] and not unknown["ok"]                                # 프로필에 없는 도구
    assert escape["valid"] and not escape["ok"] and "작업 폴더 밖" in escape["preview"]   # 작업 폴더 밖
    assert network["valid"] and not network["ok"]                                    # 샌드박스에는 네트워크가 없다


def test_large_tool_output_is_saved_to_file_with_preview(alice):
    script({"tool": "run_python", "arguments": {"code": "print('가' * 5000)"}}, {"content": "끝"})
    alice.chat(alice.session(), "x", mode="thinking")
    content = model_calls()[-1]["messages"][-1]["content"]
    assert len(content) < 1700 and "read_file로 .agent/tool-" in content


def test_auto_rules_and_model_fallback_classifier(alice):
    sid = alice.session()
    assert of(alice.chat(sid, "안녕하세요"), "mode")[0]["mode"] == "quick"
    script({"json": {"mode": "quick"}}, {"content": "설명입니다."})
    evs = alice.chat(sid, "우리 은행의 여신 심사 절차가 어떤 단계로 이루어지는지 그리고 각 단계의 담당 부서가 어디인지 알려주세요")
    assert of(evs, "mode")[0]["mode"] == "quick" and text_of(evs) == "설명입니다."
    assert model_calls()[0]["max_tokens"] == 16 and "response_format" in model_calls()[0]


def test_quick_is_promoted_to_thinking_when_format_check_fails(alice):
    script({"content": "잘린 답", "finish": "length"}, {"content": "온전한 답"})
    evs = alice.chat(alice.session(), "안녕", mode="quick")
    assert [m["mode"] for m in of(evs, "mode")] == ["quick", "thinking"] and of(evs, "mode")[1]["promoted"]
    assert text_of(evs) == "온전한 답"


def test_runaway_code_is_stopped_by_output_and_time_limits(alice):
    script({"tool": "run_python", "arguments": {"code": "while True:\n    print('x' * 1000)"}}, {"content": "끝"})
    evs = alice.chat(alice.session(), "x", mode="thinking")
    assert not of(evs, "tool_result")[0]["ok"] and "출력이 1MB를 넘어" in of(evs, "tool_result")[0]["preview"]
