"""정책: 데이터 등급, 한도, 하네스 스위치, 민감정보 마스킹."""
import os
import re
from pathlib import Path

import yaml

CONFIG = Path(os.environ.get("CONFIG_DIR", Path(__file__).parent.parent / "config"))
P = yaml.safe_load((CONFIG / "policy.yaml").read_text(encoding="utf-8"))
GRADES: list[str] = P["grades"]
TOP = len(GRADES) - 1          # 태그가 없는 자료는 최고 등급으로 취급한다
LIMITS, HARNESS, BUDGET = P["limits"], P["harness"], P["budget"]
_MASK = [re.compile(p) for p in P["mask"]]


def rank(grade: str) -> int:
    if grade not in GRADES:
        raise ValueError(f"올바르지 않은 데이터 등급입니다: {grade}")
    return GRADES.index(grade)


def mask(value):
    """문자열 값에서 주민등록번호·계좌·카드·전화번호 패턴을 가린다."""
    if isinstance(value, str):
        for pattern in _MASK:
            value = pattern.sub(lambda m: "*" * len(m.group()), value)
        return value
    if isinstance(value, dict):
        return {k: mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    return value
