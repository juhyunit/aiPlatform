"""컨텍스트 예산 조립기."""
import pytest
from conftest import model_calls, script

from app import context

MODEL = {"capabilities": {"context": 1000}}


def test_history_keeps_newest_within_budget_and_starts_with_user():
    history = [{"role": r, "content": "가" * 60} for r in ["user", "assistant"] * 4]
    messages, _ = context.assemble(MODEL, "quick", "시스템", history, "", [], "질문")
    kept = messages[1:-1]
    assert sum(context.tokens(m["content"]) for m in kept) <= 250
    assert kept and kept[0]["role"] == "user" and kept == history[-len(kept):]


def test_retrieved_stops_at_budget_and_state_goes_last():
    chunks = [f"[#{i}] " + "나" * 100 for i in range(10)]
    messages, max_tokens = context.assemble(MODEL, "quick", "시스템", [], "", chunks, "질문", state="현재 단계: 2")
    last = messages[-1]["content"]
    assert "[#0]" in last and "[#2]" in last and "[#3]" not in last        # 35% = 350토큰
    assert last.endswith("[작업 상태]\n현재 단계: 2")
    assert max_tokens >= 150                                               # 출력 예약 15% 이상


def test_oversized_input_is_rejected():
    with pytest.raises(ValueError):
        context.assemble(MODEL, "quick", "시스템", [], "", [], "다" * 1000)


def test_long_history_is_compressed_into_summary(alice):
    sid = alice.session()
    for i in range(4):
        script({"content": "응답 " + "라" * 400})
        alice.chat(sid, f"질문 {i} " + "마" * 400, mode="quick")
    script({"content": "요약: 사용자는 여비 규정을 물었다."}, {"content": "마지막 답"})
    alice.chat(sid, "이어서 질문", mode="quick")
    system = model_calls()[-1]["messages"][0]["content"]
    assert "[이전 대화 요약]\n요약: 사용자는 여비 규정을 물었다." in system
    assert len(alice.get(f"/api/sessions/{sid}/messages").json()) == 10     # 원문은 그대로 남는다
