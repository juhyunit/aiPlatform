"""Validator. 실행 기반과 규칙 기반 검사가 통과와 실패를 정한다. LLM 검토는 문제 제기만 한다."""
import re

from .policy import HARNESS

_CODE_BLOCK = re.compile(r"```.*?```", re.S)


def citations(answer: str, known: set[int]) -> list[str]:
    """인용 실재 확인. 답이 인용한 번호가 이번 실행에서 모델에 준 자료에 있어야 한다."""
    missing = sorted({int(n) for n in re.findall(r"\[#(\d+)\]", answer)} - known)
    return [f"인용 [#{n}]은 제공된 자료에 없다" for n in missing]


def numbers(answer: str, evidence: str) -> list[str]:
    """수치 재계산 확인. 답에 나온 세 자리 이상 수치는 질문, 자료, 도구 실행 결과 어딘가에 있어야 한다."""
    if not HARNESS["number_check"]:
        return []
    prose = _CODE_BLOCK.sub("", answer)
    evidence = evidence.replace(",", "")
    found = {n.replace(",", "").rstrip(".") for n in re.findall(r"(?<![#\d])\d[\d,]*(?:\.\d+)?", prose)}
    return [f"수치 {n}의 근거가 자료나 실행 결과에 없다. run_python으로 계산해 확인한다" for n in sorted(found)
            if len(n.replace(".", "")) >= 3 and n not in evidence]
