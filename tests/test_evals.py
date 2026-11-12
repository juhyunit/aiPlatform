"""평가셋 자체 검사와 러너. 채점기가 틀리면 모델 선택이 조용히 틀어지므로, 모든 채점기가 모범 답은 통과시키고 빈 답은 떨어뜨리는지 본다."""
import subprocess
import sys

import pytest
from conftest import API, ENV, ROOT, script

from evals.cases import CASES


def no_file(path):
    raise KeyError(path)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_grader_accepts_reference_and_rejects_empty(case):
    ref = case["ref"] if isinstance(case["ref"], dict) else {"answer": case["ref"]}
    assert case["grade"](ref["answer"], ref.get("events", []), lambda path: ref["files"][path].encode())
    assert not case["grade"]("", [], no_file)


def test_case_ids_are_unique_and_every_type_is_covered():
    assert len({c["id"] for c in CASES}) == len(CASES)
    assert {c["type"] for c in CASES} == {"ko_qa", "math", "coding", "doc", "image", "tool_call", "agent"}


def test_runner_prints_scoreboard_and_flags_regression(tmp_path):
    """가짜 모델에 수리 추론 1번의 정답만 넣어 둔다. 러너는 점수표를 내고, 더 높은 기준선과 비교하면 하락으로 끝난다."""
    script({"content": "이자는 150,000원입니다."})
    run = [sys.executable, "-m", "evals.run", "--url", API, "--user", "bob", "--password", "pw", "--only", "math"]
    out = subprocess.run(run, cwd=ROOT, env=ENV, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "통과 math-01" in out.stdout and "실패 math-02" in out.stdout and "1/10" in out.stdout and "0.10" in out.stdout
    baseline = tmp_path / "base.json"
    baseline.write_text('{"scores": {"math": 0.9}}')
    out = subprocess.run([*run, "--baseline", str(baseline)], cwd=ROOT, env=ENV, capture_output=True, text=True)
    assert out.returncode == 1 and "하락: math 0.90 → 0.00" in out.stdout
