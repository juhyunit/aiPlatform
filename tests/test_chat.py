"""0단계 관문: 채팅이 끝에서 끝까지 동작한다."""
from conftest import model_calls, script, text_of


def test_quick_chat_streams_and_saves(alice):
    sid = alice.session()
    script({"content": "여비 규정은 인사부 소관입니다."})
    evs = alice.chat(sid, "여비 규정은 어느 부서 소관인가요?", mode="quick")
    types = [t for t, _ in evs]
    assert types[0] == "run" and types[-1] == "run_done" and "llm_call" in types
    assert text_of(evs) == "여비 규정은 인사부 소관입니다."
    assert evs[-1][1]["ok"]
    assert alice.get(f"/api/sessions/{sid}/messages").json() == [
        {"role": "user", "content": "여비 규정은 어느 부서 소관인가요?"},
        {"role": "assistant", "content": "여비 규정은 인사부 소관입니다."}]


def test_history_is_sent_on_next_turn(alice):
    sid = alice.session()
    alice.chat(sid, "첫 질문", mode="quick")
    alice.chat(sid, "둘째 질문", mode="quick")
    roles = [m["role"] for m in model_calls()[-1]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


def test_quick_turns_reasoning_off(alice):
    alice.chat(alice.session(), "안녕", mode="quick")
    assert model_calls()[-1]["x_reasoning"] == "off"
