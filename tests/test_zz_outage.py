"""모델 서버 장애. 장애가 난 모델은 API 서버 안에서 30초 동안 차단되므로 이 파일은 맨 마지막에 돈다."""
import httpx
from conftest import FAKE, Client, script, text_of


def test_failover_to_next_allowed_model(alice):
    httpx.post(FAKE + "/down", json=["small"])
    script({"content": "대체 모델의 답"})
    evs = alice.chat(alice.session(), "질문", mode="quick")
    call = next(d for t, d in evs if t == "llm_call")
    assert text_of(evs) == "대체 모델의 답"
    assert call["model"] == "big" and call["failover_from"] == ["small"]
    assert call["prompt_tokens"] > 0 and call["ttft_ms"] >= 0


def test_all_models_down_is_a_clear_error(alice):
    httpx.post(FAKE + "/down", json=["small", "big"])
    evs = Client("carol").chat(Client("carol").session(), "질문", mode="quick")
    assert not evs[-1][1]["ok"] and "모델 서버" in evs[-1][1]["error"]
