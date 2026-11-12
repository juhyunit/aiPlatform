"""Tool Manager. 도구 정의는 MCP와 같은 모양(name, description, inputSchema)이다.

등급: read는 자동 실행, write는 자동 실행 + 기록. 어떤 도구를 쓸 수 있는지는 콘텐츠가 아니라 프로필이 정한다.
외부에 영향을 주는 도구(승인 필요 등급)는 아직 없다. 처음 추가할 때 승인 절차를 함께 넣는다.
"""
from . import knowledge, sandbox


def _path(run, path: str):
    """작업 폴더 밖은 접근할 수 없다."""
    root = sandbox.workspace(run.session_id).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ValueError("작업 폴더 밖의 경로입니다")
    return target


async def search(run, query: str) -> str:
    rows = await knowledge.search(run.user, run.clear, query, run.session_id, org=True)
    run.sources.update({r["id"]: r for r in rows})
    return "\n\n".join(knowledge.cite(r) for r in rows) or "검색 결과가 없습니다"


async def list_files(run) -> str:
    root = sandbox.workspace(run.session_id)
    files = sorted(p for p in root.rglob("*") if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts))
    return "\n".join(f"{p.relative_to(root)} ({p.stat().st_size}바이트)" for p in files) or "파일이 없습니다"


async def read_file(run, path: str, start_line: int = 1) -> str:
    lines = _path(run, path).read_text(encoding="utf-8").splitlines()
    part = lines[start_line - 1:start_line + 199]
    more = f"\n… (전체 {len(lines)}줄. 이어 읽으려면 start_line={start_line + 200})" if start_line + 199 < len(lines) else ""
    return "\n".join(part) + more


async def write_file(run, path: str, content: str) -> str:
    target = _path(run, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.parent.chmod(0o777)
    target.write_text(content, encoding="utf-8")
    target.chmod(0o666)
    return f"{path} 저장({len(content)}자)"


async def run_python(run, code: str) -> str:
    await write_file(run, ".agent/run.py", code)
    status, out = await sandbox.run(run.session_id, ["python", ".agent/run.py"])
    if status:
        raise RuntimeError(f"종료 코드 {status}\n{out}")
    return out or "(출력 없음)"


def _tool(fn, tier, description, properties, required):
    return {"name": fn.__name__, "fn": fn, "tier": tier, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}


_S = {"type": "string"}
TOOLS = {t["name"]: t for t in [
    _tool(search, "read", "첨부 파일과 사내 문서에서 관련 대목을 찾는다. 결과의 [#번호]로 인용한다.", {"query": _S}, ["query"]),
    _tool(list_files, "read", "작업 폴더의 파일 목록을 본다.", {}, []),
    _tool(read_file, "read", "작업 폴더의 텍스트 파일을 200줄씩 읽는다.", {"path": _S, "start_line": {"type": "integer"}}, ["path"]),
    _tool(write_file, "write", "작업 폴더에 텍스트 파일을 쓴다.", {"path": _S, "content": _S}, ["path", "content"]),
    _tool(run_python, "write", "파이썬 코드를 격리된 샌드박스에서 실행하고 출력을 돌려준다. 작업 폴더가 현재 폴더다. "
          "네트워크는 없다. pandas, openpyxl, python-docx, python-pptx, pytest를 쓸 수 있다.", {"code": _S}, ["code"]),
]}
