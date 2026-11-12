"""프롬프트 관리. 템플릿은 Git으로 관리하는 파일이고, 모델별 차이는 patches/<모델 ID>/에 덧붙일 내용만 둔다."""
import hashlib
from pathlib import Path

DIR = Path(__file__).parent.parent / "prompts"
# 추적 기록에 남기는 프롬프트 버전. 파일 내용의 해시라 컨테이너 안에서도 같은 값이 나온다.
VERSION = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(DIR.rglob("*.md")))).hexdigest()[:12]


def load(name: str, model_id: str) -> str:
    text = (DIR / f"{name}.md").read_text(encoding="utf-8")
    patch = DIR / "patches" / model_id / f"{name}.md"
    return text + "\n" + patch.read_text(encoding="utf-8") if patch.exists() else text
