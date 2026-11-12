"""샌드박스. 모델이 쓴 코드와 파일 파서를 작업마다 일회용 컨테이너에서 실행한다.

네트워크 없음, 읽기 전용 루트, 비루트 사용자, 권한 제거, CPU·메모리·프로세스 수·시간 제한.
컨테이너에 보이는 것은 그 세션의 작업 폴더(/work)뿐이다.
"""
import asyncio
import contextlib
import os
import uuid
from pathlib import Path

DATA = Path(os.environ["DATA_DIR"])
# 컨테이너로 배포하면 작업 폴더는 도커 볼륨 안에 있다. 그때는 볼륨 이름을 받아 세션 폴더만 샌드박스에 붙인다.
VOLUME = os.environ.get("SANDBOX_VOLUME")
IMAGE = "agent-sandbox"
MAX_OUTPUT = 2**20


def workspace(session_id) -> Path:
    path = DATA / str(session_id)
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o777)       # 컨테이너 안의 비루트 사용자가 결과 파일을 쓸 수 있어야 한다
    return path


async def run(session_id, argv: list[str], timeout: int = 60) -> tuple[int, str]:
    """명령을 실행하고 (종료 코드, 출력)을 돌려준다. 시간이나 출력 크기 상한을 넘기면 컨테이너를 없애고 124를 돌려준다."""
    name = "sbx-" + uuid.uuid4().hex[:12]
    path = workspace(session_id)
    mount = ["--mount", f"type=volume,src={VOLUME},dst=/work,volume-subpath={session_id}"] if VOLUME else ["-v", f"{path}:/work"]
    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "--rm", "--name", name, "--network", "none", "--read-only", "--tmpfs", "/tmp:size=64m",
        "--user", "65534:65534", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--cpus", "1", "--memory", "1g", "--pids-limit", "128",
        *mount, IMAGE, *argv,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

    async def read() -> bytes:
        out = b""
        while len(out) <= MAX_OUTPUT and (chunk := await proc.stdout.read(65536)):
            out += chunk
        return out

    async def cleanup():
        with contextlib.suppress(ProcessLookupError):
            proc.kill()         # 읽기를 멈췄으므로 먼저 끊는다. 그대로 두면 출력을 쓰다 멈춰 있다
        await (await asyncio.create_subprocess_exec("docker", "rm", "-f", name, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)).wait()
        await proc.wait()

    try:
        out = await asyncio.wait_for(read(), timeout)
    except TimeoutError:
        out = None
    except BaseException:
        await cleanup()
        raise
    if out is None or len(out) > MAX_OUTPUT:
        await cleanup()
        return 124, f"실행 시간 {timeout}초를 넘겨 중단했습니다" if out is None else "출력이 1MB를 넘어 중단했습니다"
    return await proc.wait(), out.decode(errors="replace")
