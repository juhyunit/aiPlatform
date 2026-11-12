"""통합 테스트 환경. DB 컨테이너, 가짜 모델 서버, API 서버, 작업 워커를 띄우고 HTTP로만 시험한다.

외부 통신은 쓰지 않는다. 테스트 프로세스가 루프백이 아닌 주소로 연결하려 하면 실패시킨다.
DB에는 유닉스 소켓으로 붙는다. 그래서 scripts/test-offline.sh가 네트워크를 끈 상태에서 이 테스트 전체를 그대로 돌릴 수 있다.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).parent.parent
DATA = Path(tempfile.mkdtemp(prefix="ap-test-"))
SOCK = Path(tempfile.mkdtemp(prefix="ap-sock-"))
API, FAKE = "http://127.0.0.1:9101", "http://127.0.0.1:9100"
ENV = {**os.environ,
       "DATABASE_URL": f"postgresql://app:app@/postgres?host={SOCK}",
       "ADMIN_DATABASE_URL": f"postgresql://postgres:owner@/postgres?host={SOCK}",
       "SECRET_KEY": "test", "MODELS_FILE": str(ROOT / "tests/models.yaml"), "DATA_DIR": str(DATA)}
os.environ.update(ENV)

_connect = socket.socket.connect


def _local_only(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ("127.0.0.1", "::1", "localhost"):
        raise AssertionError(f"외부 연결 시도: {address}")
    return _connect(sock, address)


socket.socket.connect = _local_only


WORKER = []


def start_worker():
    WORKER[:] = [subprocess.Popen([sys.executable, "-m", "app.worker"], cwd=ROOT, env=ENV)]


def _wait(url):
    for _ in range(100):
        try:
            httpx.get(url)
            return
        except httpx.TransportError:
            time.sleep(0.2)
    raise RuntimeError(f"{url} 이 뜨지 않았습니다")


@pytest.fixture(scope="session", autouse=True)
def stack():
    subprocess.run(["docker", "rm", "-f", "ap-test-db"], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", "ap-test-db", "--network", "none", "-v", f"{SOCK}:/var/run/postgresql",
                    "-e", "POSTGRES_PASSWORD=owner", "-e", "APP_DB_PASSWORD=app",
                    "-v", f"{ROOT}/app/schema.sql:/docker-entrypoint-initdb.d/schema.sql:ro",
                    "pgvector/pgvector:pg17"], check=True, capture_output=True)
    # 초기화용 임시 서버는 소켓으로만 듣는다. TCP로 응답하면 초기화가 끝난 진짜 서버다
    while subprocess.run(["docker", "exec", "ap-test-db", "pg_isready", "-q", "-h", "127.0.0.1"]).returncode:
        time.sleep(0.2)
    py = [sys.executable, "-m"]
    for user in (["alice", "pw", "internal", "retail"], ["bob", "pw", "personal", "risk"], ["carol", "pw", "restricted"],
                 ["librarian", "pw", "personal"], ["dave", "pw", "internal"]):
        subprocess.run([*py, "app.admin", "adduser", *user], cwd=ROOT, env=ENV, check=True)
    procs = [subprocess.Popen([*py, "uvicorn", "tests.fake_model:app", "--port", "9100", "--log-level", "warning"], cwd=ROOT, env=ENV),
             subprocess.Popen([*py, "uvicorn", "app.api:app", "--port", "9101", "--log-level", "warning"], cwd=ROOT, env=ENV),
]
    start_worker()
    _wait(FAKE + "/calls")
    _wait(API + "/api/me")
    yield
    for p in [*procs, WORKER[0]]:
        p.terminate()
    subprocess.run(["docker", "rm", "-f", "ap-test-db"], capture_output=True)
    shutil.rmtree(DATA, ignore_errors=True)
    shutil.rmtree(SOCK, ignore_errors=True)


class Client(httpx.Client):
    """로그인한 사용자 한 명."""

    def __init__(self, user):
        super().__init__(base_url=API, timeout=120)
        self.post("/api/login", json={"id": user, "password": "pw"}).raise_for_status()

    def session(self, grade="internal") -> str:
        return self.post("/api/sessions", json={"title": "t", "grade": grade}).json()["id"]

    def upload(self, session_id, name, data: bytes, grade="internal"):
        return self.put(f"/api/sessions/{session_id}/files/{name}", content=data, params={"grade": grade} if grade else {})

    def control(self, run_id, action, **kw):
        """개입한 뒤, 작업이 끝나거나 다시 사용자를 기다릴 때까지의 이벤트 전체를 돌려준다."""
        self.post(f"/api/runs/{run_id}/control", json={"action": action, **kw}).raise_for_status()
        return events(self.get(f"/api/runs/{run_id}/events").text)

    def status(self, run_id) -> str:
        return next(r["status"] for r in self.get("/api/runs").json() if r["id"] == run_id)

    def chat(self, session_id, message, **kw) -> list[tuple[str, dict]]:
        """요청을 보내고 받은 이벤트를 (종류, 내용) 목록으로 돌려준다."""
        r = self.post("/api/chat", json={"session_id": session_id, "message": message, **kw})
        r.raise_for_status()
        return events(r.text)


def events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        type, data = block.split("\n", 1)
        out.append((type.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


def text_of(evs) -> str:
    """화면이 그리는 답. reset이 오면 그때까지의 초안을 지운다."""
    text = ""
    for t, d in evs:
        if t == "token":
            text = "" if d.get("reset") else text + d["text"]
    return text


def of(evs, type) -> list[dict]:
    return [d for t, d in evs if t == type]


def admin(*args):
    subprocess.run([sys.executable, "-m", "app.admin", *args], cwd=ROOT, env=ENV, check=True, capture_output=True)


def script(*items):
    httpx.post(FAKE + "/script", json=list(items)).raise_for_status()


def model_calls() -> list[dict]:
    return httpx.get(FAKE + "/calls").json()


@pytest.fixture
def alice():
    return Client("alice")


@pytest.fixture
def bob():
    return Client("bob")


@pytest.fixture(autouse=True)
def reset():
    script()
    httpx.post(FAKE + "/down", json=[])
