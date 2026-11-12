"""File Pipeline. 형식을 확인하고, 격리된 프로세스에서 공통 문서 표현으로 바꾼 뒤 색인한다."""
import json
from pathlib import Path

from . import knowledge, sandbox
from .policy import LIMITS

IMAGES = {".png": b"\x89PNG", ".jpg": b"\xff\xd8", ".jpeg": b"\xff\xd8"}
MAGIC = {".pdf": b"%PDF", ".docx": b"PK\x03\x04", ".pptx": b"PK\x03\x04", ".xlsx": b"PK\x03\x04", ".hwpx": b"PK\x03\x04", **IMAGES}
UNSUPPORTED = {".hwp": "HWP(5.x 바이너리)는 아직 처리하지 못합니다. 한글에서 HWPX 또는 PDF로 저장해 다시 올려 주세요.",
               ".doc": "구형 Office 형식입니다. DOCX로 저장해 다시 올려 주세요.",
               ".xls": "구형 Office 형식입니다. XLSX로 저장해 다시 올려 주세요.",
               ".ppt": "구형 Office 형식입니다. PPTX로 저장해 다시 올려 주세요."}


async def parse(workspace_id, name: str, data: bytes) -> tuple[str, list[dict]]:
    """파일을 작업 폴더에 두고 청크 목록으로 바꾼다. 실패는 숨기지 않고 사유를 ValueError로 알린다."""
    name = Path(name).name
    suffix = Path(name).suffix.lower()
    if not name or name.startswith("."):
        raise ValueError("파일 이름이 올바르지 않습니다")
    if len(data) > LIMITS["upload_mb"] * 2**20:
        raise ValueError(f"파일이 {LIMITS['upload_mb']}MB를 넘습니다")
    if suffix in UNSUPPORTED:
        raise ValueError(UNSUPPORTED[suffix])
    if suffix in MAGIC and not data.startswith(MAGIC[suffix]):      # 확장자와 실제 형식 대조
        raise ValueError("파일 내용이 확장자와 맞지 않습니다")
    path = sandbox.workspace(workspace_id) / name
    path.write_bytes(data)
    path.chmod(0o666)
    if suffix in IMAGES:        # 이미지는 색인하지 않고 비전 모델에 그대로 준다
        return name, []
    code, out = await sandbox.run(workspace_id, ["python", "/opt/parse.py", name])
    result = json.loads(out) if code == 0 else {"error": "파일을 읽지 못했습니다. PDF로 저장해 다시 올려 주세요."}
    if "error" in result:
        path.unlink()
        raise ValueError(result["error"])
    return name, knowledge.chunk(result["blocks"])


async def ingest(user: dict, session_id, name: str, data: bytes, grade: int) -> dict:
    """첨부 파일 하나를 세션에 색인한다."""
    name, chunks = await parse(session_id, name, data)
    doc_id = await knowledge.index(user, "session", name, grade, chunks, session_id)
    return {"id": doc_id, "name": name, "chunks": len(chunks)}
