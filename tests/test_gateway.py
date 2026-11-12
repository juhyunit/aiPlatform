"""게이트웨이: 정책 필터가 먼저이고, 그 안에서만 라우터가 고른다."""
import os

import psycopg
import pytest

from app import gateway
from app.policy import rank


def ids(grade, **kw):
    return [m["id"] for m in gateway.select(rank(grade), **kw)]


def test_policy_filter_keeps_external_models_below_env_ceiling():
    assert ids("public") == ["cloud", "small", "big"]          # 가벼운 순
    assert ids("internal") == ["small", "big"]                 # 외부 모델은 레지스트리 clearance가 높아도 env_max에 막힌다
    assert ids("personal") == ["small", "big"]


def test_named_model_must_be_in_allowed_set():
    assert ids("public", model="cloud") == ["cloud"]
    with pytest.raises(gateway.Denied):
        ids("internal", model="cloud")


def test_router_filters_by_capability():
    assert ids("internal", need=("vision",)) == ["small"]
    assert ids("internal", need=("embedding",)) == ["embedder"]


def test_denied_request_is_refused_and_audited(alice):
    r = alice.post("/api/chat", json={"session_id": alice.session(), "message": "x", "model": "cloud"})
    assert r.status_code == 403
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        assert conn.execute("select action, user_id from audit_log order by id desc limit 1").fetchone() == ("denied", "alice")


def test_parse_json_strips_thinking_tags_and_markdown_fences():
    raw = "<think>\n복잡한 추론 과정\n</think>\n```json\n{\"mode\": \"thinking\"}\n```"
    assert gateway.parse_json(raw) == {"mode": "thinking"}
