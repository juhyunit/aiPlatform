"""운영 통제: 행 수준 보안, 감사 로그, 마스킹, 사용량 제한."""
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg
import pytest
from conftest import ENV, ROOT, Client, script

from app.policy import mask


def app_conn(user=None):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    if user:
        conn.execute("select set_config('app.user', %s, false), set_config('app.clearance', '4', false)", (user,))
    return conn


def test_rls_hides_other_users_rows_even_without_where(alice, bob):
    alice.session(), bob.session("personal")
    assert app_conn().execute("select count(*) from sessions").fetchone()[0] == 0
    owners = {r[0] for r in app_conn("alice").execute("select user_id from sessions")}
    assert owners == {"alice"}


def test_other_users_session_is_not_found(alice, bob):
    sid = bob.session("personal")
    assert alice.get(f"/api/sessions/{sid}/messages").json() == []
    assert alice.post("/api/chat", json={"session_id": sid, "message": "x"}).status_code == 404


def test_session_grade_cannot_exceed_clearance(alice):
    assert alice.post("/api/sessions", json={"title": "t", "grade": "personal"}).status_code == 403


def test_audit_log_is_append_only_and_hash_chained(alice):
    alice.chat(alice.session(), "감사 대상 요청", mode="quick")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn().execute("update audit_log set user_id = 'x'")
    verify = [sys.executable, "-m", "app.admin", "verify-audit"]
    assert subprocess.run(verify, cwd=ROOT, env=ENV).returncode == 0
    with psycopg.connect(os.environ["ADMIN_DATABASE_URL"]) as owner:      # 소유자 권한으로 한 행을 고치면 연쇄가 깨진다
        owner.execute("update audit_log set user_id = 'x' where id = (select min(id) from audit_log)")
    assert subprocess.run(verify, cwd=ROOT, env=ENV).returncode == 1
    with psycopg.connect(os.environ["ADMIN_DATABASE_URL"]) as owner:
        owner.execute("truncate audit_log")


def test_mask_hides_sensitive_patterns_in_nested_values():
    out = mask({"q": "주민번호 900101-1234567, 카드 1234-5678-9012-3456", "n": [1, "전화 010-1234-5678 계좌 110-234-567890"]})
    assert out == {"q": "주민번호 **************, 카드 *******************", "n": [1, "전화 ************* 계좌 **************"]}


def test_unauthenticated_request_is_rejected():
    assert httpx.get("http://127.0.0.1:9101/api/sessions").status_code == 401


def test_per_user_concurrency_and_daily_token_limits():
    dave = Client("dave")
    sid = dave.session()
    slow = {"tool": "run_python", "arguments": {"code": "import time\ntime.sleep(3)"}}
    script(slow, slow, {"content": "끝"}, {"content": "끝"})
    with ThreadPoolExecutor() as pool:
        running = [pool.submit(dave.chat, sid, "x", mode="thinking") for _ in range(2)]
        time.sleep(1.5)
        third = dave.post("/api/chat", json={"session_id": sid, "message": "x", "mode": "quick"})
        assert third.status_code == 429 and "동시" in third.json()["detail"]
        assert all(f.result()[-1][1]["ok"] for f in running)
    assert dave.post("/api/chat", json={"session_id": sid, "message": "x", "mode": "quick"}).status_code == 200
    with psycopg.connect(os.environ["ADMIN_DATABASE_URL"]) as owner:
        owner.execute("update runs set tokens = 2000000 where id = (select id from runs where user_id = 'dave' limit 1)")
    over = dave.post("/api/chat", json={"session_id": sid, "message": "x", "mode": "quick"})
    assert over.status_code == 429 and "토큰" in over.json()["detail"]


def test_run_is_audited_with_model_tools_and_documents(alice):
    sid = alice.session()
    alice.upload(sid, "규정.txt", "일비는 정액이다.".encode())
    script({"tool": "run_python", "arguments": {"code": "print(1)"}}, {"content": "끝"})
    alice.chat(sid, "x", mode="thinking")
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        detail = conn.execute("select detail from audit_log where action = 'run' order by id desc limit 1").fetchone()[0]
    assert detail["mode"] == "thinking" and detail["models"] == ["small"] and detail["tools"] == ["run_python"] and detail["documents"] == ["규정.txt"]


def test_image_names_cannot_escape_the_session(alice):
    evs = alice.chat(alice.session(), "x", mode="quick", images=["../../../../etc/passwd"])
    assert not evs[-1][1]["ok"] and "첨부하지 않은 이미지" in evs[-1][1]["error"]


def test_malformed_ids_are_rejected_not_crashed(alice):
    assert alice.get("/api/sessions/not-a-uuid/messages").status_code == 422
    assert alice.get("/api/sessions/not-a-uuid/files/x").status_code == 422
    assert alice.post("/api/sessions", json={"title": "t", "grade": "invalid"}).status_code == 422
    assert alice.post("/api/chat", json={"session_id": alice.session(), "message": "x", "mode": "invalid"}).status_code == 422
