"""평가셋 초판. 문항마다 입력, 첨부, 코드로 쓴 채점기, 그리고 채점기가 통과시켜야 하는 모범 답(ref)을 갖는다.

현업 문항이 오기 전까지 쓰는 씨앗이다. 운영 중 실패 사례를 여기에 계속 편입한다.
유형: ko_qa(한국어 질의응답), math(수리 추론), coding(코딩), doc(문서 이해), image(이미지 분석),
tool_call(도구 호출), agent(작업 완결).
"""
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import date, timedelta
from pathlib import Path

FILES = Path(__file__).parent / "files"


def has(*words):
    """답에 낱말이 모두 들어 있다."""
    return lambda answer, events, fetch: all(w in answer for w in words)


def any_of(*words):
    return lambda answer, events, fetch: any(w in answer for w in words)


def number(n):
    """쉼표를 뺀 답에 그 수가 들어 있다."""
    return lambda answer, events, fetch: str(n) in answer.replace(",", "")


def cited(grade):
    """근거 대목을 [#번호]로 인용했고 답도 맞다."""
    return lambda answer, events, fetch: "[#" in answer and grade(answer, events, fetch)


def used(tool, grade):
    """지정한 도구를 형식에 맞게 불러 성공했고 답도 맞다."""
    return lambda answer, events, fetch: grade(answer, events, fetch) and any(
        t == "tool_result" and d["tool"] == tool and d["valid"] and d["ok"] for t, d in events)


def pytest_passes(files: dict[str, str]) -> bool:
    """파일들을 샌드박스 컨테이너에 읽기 전용으로 넣고 pytest를 돌린다. 모델이 쓴 코드를 호스트에서 실행하지 않는다."""
    with tempfile.TemporaryDirectory() as d:
        os.chmod(d, 0o755)
        for name, text in files.items():
            Path(d, name).write_text(text, encoding="utf-8")
        return subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp", "--user", "65534:65534", "-v", f"{d}:/work:ro",
             "agent-sandbox", "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "/work"],
            capture_output=True, timeout=120).returncode == 0


def code(tests: str):
    """답의 마지막 코드 블록이 숨은 테스트를 통과한다."""
    def grade(answer, events, fetch):
        blocks = re.findall(r"```(?:python)?\n(.*?)```", answer, re.S)
        return bool(blocks) and pytest_passes({"solution.py": blocks[-1], "test_hidden.py": "from solution import *\n\n" + tests})
    return grade


def produced(path: str, tests: str):
    """Agent 작업이 완료로 끝났고, 만든 파일이 숨은 테스트를 통과한다."""
    def grade(answer, events, fetch):
        ok = any(t == "run_done" and d["ok"] for t, d in events)
        return ok and pytest_passes({path: fetch(path).decode(), "test_hidden.py": tests})
    return grade


def case(id, type, mode, prompt, grade, ref, files=(), images=()):
    return {"id": id, "type": type, "mode": mode, "prompt": prompt, "grade": grade, "ref": ref,
            "files": list(files), "images": list(images)}


def block(source: str) -> str:
    return "```python\n" + source + "```"


TOOL_OK = [("tool_result", {"tool": "run_python", "valid": True, "ok": True})]
SHA = hashlib.sha256("은행".encode()).hexdigest()[:8]
AFTER = date(2026, 1, 15) + timedelta(days=100)
SHORT = " 한 단어로 답하세요."
PRIMES = sum(all(n % d for d in range(2, int(n**0.5) + 1)) for n in range(2, 1001))
DIGITS = sum(int(c) for c in str(math.factorial(30)))

CASES = [
    # 한국어 질의응답 — Quick
    case("qa-01", "ko_qa", "quick", "대한민국의 수도는 어디인가요?" + SHORT, has("서울"), "서울"),
    case("qa-02", "ko_qa", "quick", "금리가 오르면 이미 발행된 채권의 가격은 오르나요, 내리나요?" + SHORT, any_of("내린", "내립", "내려", "하락", "떨어"), "내립니다"),
    case("qa-03", "ko_qa", "quick", "은행의 '여신'과 '수신' 가운데 예금을 받는 업무를 뜻하는 말은 무엇인가요?" + SHORT, has("수신"), "수신"),
    case("qa-04", "ko_qa", "quick", "한국은행 금융통화위원회가 결정하는 정책금리를 무엇이라고 부르나요?", has("기준금리"), "기준금리"),
    case("qa-05", "ko_qa", "quick", "BIS 자기자본비율의 BIS는 어느 기관을 가리키나요? 한국어 기관명으로 답하세요.", has("국제결제은행"), "국제결제은행"),
    case("qa-06", "ko_qa", "quick", "통화 코드 KRW는 어느 나라의 통화인가요?", any_of("대한민국", "한국"), "대한민국"),
    case("qa-07", "ko_qa", "quick", "가계대출 규제 지표 DSR은 무엇의 약자인가요? 한국어 용어로 답하세요.", has("총부채원리금상환비율"), "총부채원리금상환비율"),
    case("qa-08", "ko_qa", "quick", "주민등록번호는 모두 몇 자리 숫자인가요? 숫자만 답하세요.", number(13), "13"),
    case("qa-09", "ko_qa", "quick", "'대금을 결재하다'와 '대금을 결제하다' 가운데 맞춤법이 맞는 표현을 그대로 쓰세요.", has("결제하다"), "대금을 결제하다"),
    case("qa-10", "ko_qa", "quick", "영어 'interest rate'를 한국어 금융 용어로 옮기세요.", any_of("금리", "이자율"), "금리"),

    # 수리 추론 — Thinking
    case("math-01", "math", "thinking", "원금 1,000,000원을 연 5% 단리로 3년 맡기면 이자는 모두 얼마인가요?", number(150000), "150,000원"),
    case("math-02", "math", "thinking", "원금 1,000,000원을 연 4% 복리(연 1회)로 2년 맡기면 원리금은 얼마인가요?", number(1081600), "1,081,600원"),
    case("math-03", "math", "thinking", "1234 곱하기 5678은 얼마인가요?", number(1234 * 5678), str(1234 * 5678)),
    case("math-04", "math", "thinking", "250,000원의 12%는 얼마인가요?", number(30000), "30,000원"),
    case("math-05", "math", "thinking", "연체 대출이 37억 원이고 총대출이 1,480억 원이면 연체율은 몇 %인가요?", number(2.5), "2.5%"),
    case("math-06", "math", "thinking", "환율이 1달러에 1,380원일 때 2,500달러는 몇 원인가요?", number(3450000), "3,450,000원"),
    case("math-07", "math", "thinking", "5,000만 원을 연 6%로 빌렸습니다. 원금은 만기에 갚을 때 매달 내는 이자는 얼마인가요?", number(250000), "250,000원"),
    case("math-08", "math", "thinking", "17의 제곱과 23의 제곱을 더하면 얼마인가요?", number(17**2 + 23**2), str(17**2 + 23**2)),
    case("math-09", "math", "thinking", "2026년 1월 15일에서 100일 뒤는 몇 월 며칠인가요?", has(f"{AFTER.month}월 {AFTER.day}일"), f"{AFTER.month}월 {AFTER.day}일"),
    case("math-10", "math", "thinking", "세 지점의 매출이 127억, 84억, 104억 원입니다. 가장 큰 매출은 세 지점 평균보다 몇 억 원 많은가요?", number(22), "22억 원"),

    # 코딩 — Thinking. 답의 코드 블록을 숨은 테스트로 채점한다
    case("code-01", "coding", "thinking", "윤년이면 True를 돌려주는 파이썬 함수 is_leap_year(year)를 코드 블록 하나로 작성하세요.",
         code("def test():\n    assert is_leap_year(2024) and is_leap_year(2000)\n    assert not is_leap_year(1900) and not is_leap_year(2026)\n"),
         block("def is_leap_year(year):\n    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)\n")),
    case("code-02", "coding", "thinking", "문자열 안의 주민등록번호(6자리-7자리)의 뒤 7자리를 *로 바꾸는 파이썬 함수 mask_rrn(text)를 코드 블록 하나로 작성하세요.",
         code("def test():\n    assert mask_rrn('번호 900101-1234567 확인') == '번호 900101-******* 확인'\n    assert mask_rrn('없음') == '없음'\n"),
         block("import re\n\n\ndef mask_rrn(text):\n    return re.sub(r'(\\d{6})-\\d{7}', r'\\1-*******', text)\n")),
    case("code-03", "coding", "thinking", "'1,234,500원' 같은 문자열을 정수 1234500으로 바꾸는 파이썬 함수 parse_amount(text)를 코드 블록 하나로 작성하세요.",
         code("def test():\n    assert parse_amount('1,234,500원') == 1234500\n    assert parse_amount('500원') == 500\n"),
         block("def parse_amount(text):\n    return int(text.replace(',', '').replace('원', ''))\n")),
    case("code-04", "coding", "thinking", "정수 금액을 '1,234,500원' 형식의 문자열로 바꾸는 파이썬 함수 to_won(amount)를 코드 블록 하나로 작성하세요.",
         code("def test():\n    assert to_won(1234500) == '1,234,500원'\n    assert to_won(0) == '0원'\n"),
         block("def to_won(amount):\n    return f'{amount:,}원'\n")),
    case("code-05", "coding", "thinking", "원금, 연이율(0.05 같은 소수), 기간(년)을 받아 단리 이자를 돌려주는 파이썬 함수 simple_interest(principal, rate, years)를 코드 블록 하나로 작성하세요.",
         code("def test():\n    assert abs(simple_interest(1000000, 0.05, 3) - 150000) < 1e-6\n"),
         block("def simple_interest(principal, rate, years):\n    return principal * rate * years\n")),
    case("code-06", "coding", "thinking", "원리금균등상환 대출의 월 상환액을 원 단위로 반올림해 돌려주는 파이썬 함수 monthly_payment(principal, annual_rate, months)를 코드 블록 하나로 작성하세요. 연이율은 0.06 같은 소수입니다.",
         code("def test():\n    assert monthly_payment(10000000, 0.06, 12) == 860664\n    assert monthly_payment(1200000, 0.0, 12) == 100000\n"),
         block("def monthly_payment(principal, annual_rate, months):\n    r = annual_rate / 12\n    if r == 0:\n        return round(principal / months)\n"
               "    return round(principal * r / (1 - (1 + r) ** -months))\n")),
    case("code-07", "coding", "thinking", "리스트에서 중복을 없애되 처음 나온 순서를 지키는 파이썬 함수 dedupe(items)를 코드 블록 하나로 작성하세요.",
         code("def test():\n    assert dedupe([3, 1, 3, 2, 1]) == [3, 1, 2]\n    assert dedupe([]) == []\n"),
         block("def dedupe(items):\n    return list(dict.fromkeys(items))\n")),
    case("code-08", "coding", "thinking", "{이름: 값} 딕셔너리에서 값이 큰 순서로 이름 n개를 리스트로 돌려주는 파이썬 함수 top_n(scores, n)을 코드 블록 하나로 작성하세요.",
         code("def test():\n    assert top_n({'강남': 150, '종로': 95, '부산': 70}, 2) == ['강남', '종로']\n"),
         block("def top_n(scores, n):\n    return sorted(scores, key=scores.get, reverse=True)[:n]\n")),
    case("code-09", "coding", "thinking", "계좌번호 문자열에서 하이픈을 뺀 숫자가 10~14자리이고 숫자와 하이픈만 있으면 True를 돌려주는 파이썬 함수 is_valid_account(text)를 코드 블록 하나로 작성하세요.",
         code("def test():\n    assert is_valid_account('110-234-567890')\n    assert not is_valid_account('110-234') and not is_valid_account('110-23a-567890')\n"),
         block("def is_valid_account(text):\n    digits = text.replace('-', '')\n    return digits.isdigit() and 10 <= len(digits) <= 14\n")),
    case("code-10", "coding", "thinking", "두 날짜 문자열('2026-03-02' 형식) 사이의 평일(월~금) 수를 양 끝을 포함해 세는 파이썬 함수 weekdays_between(start, end)를 코드 블록 하나로 작성하세요.",
         code("def test():\n    assert weekdays_between('2026-03-02', '2026-03-08') == 5\n    assert weekdays_between('2026-03-07', '2026-03-08') == 0\n"),
         block("from datetime import date, timedelta\n\n\ndef weekdays_between(start, end):\n    a, b = date.fromisoformat(start), date.fromisoformat(end)\n"
               "    return sum((a + timedelta(i)).weekday() < 5 for i in range((b - a).days + 1))\n")),

    # 문서 이해 — Quick + 첨부. 근거 인용까지 본다
    *[case(f"doc-{i + 1:02d}", "doc", "quick", q, cited(g), ref + " [#1]", files=["여비규정.txt"]) for i, (q, g, ref) in enumerate([
        ("첨부한 규정에서 출장 1일당 일비는 얼마인가요?", number(25000), "25,000원"),
        ("첨부한 규정에서 서울의 1박당 숙박비 상한은 얼마인가요?", number(120000), "120,000원"),
        ("첨부한 규정에서 자가용을 이용하면 1km당 얼마를 지급하나요?", number(280), "280원"),
        ("첨부한 규정에서 출장 복귀 후 며칠 안에 여비를 정산해야 하나요?", has("7일"), "7일"),
        ("첨부한 규정에서 5일을 넘는 출장은 누구의 승인을 받아야 하나요?", has("본부장"), "본부장"),
        ("첨부한 규정에서 이 규정의 해석과 운용을 맡는 부서는 어디인가요?", has("인사부"), "인사부"),
        ("첨부한 규정에서 인턴에게 지급하는 여비 항목은 무엇인가요?", has("일비"), "일비만 지급"),
        ("첨부한 규정에서 편도 50km 미만 출장의 일비는 얼마인가요?", number(12500), "절반인 12,500원"),
        ("첨부한 규정에서 개인 사유로 출장을 취소했을 때 수수료는 누가 부담하나요?", has("본인"), "본인"),
        ("첨부한 규정의 시행일은 언제인가요?", has("4월 1일"), "2026년 4월 1일"),
    ])],

    # 이미지 분석 — Quick + 이미지
    case("img-01", "image", "quick", "이 그림에 적힌 승인 번호 네 자리는 무엇인가요?", number(4821), "4821", files=["승인번호.png"], images=["승인번호.png"]),
    case("img-02", "image", "quick", "이 막대그래프에서 매출이 가장 큰 지점은 어디인가요?", has("강남"), "강남", files=["막대그래프.png"], images=["막대그래프.png"]),
    case("img-03", "image", "quick", "이 표에서 과장의 숙박비 상한은 얼마인가요?", number(110000), "110,000원", files=["표.png"], images=["표.png"]),

    # 도구 호출 — Thinking. 도구를 형식에 맞게 불러 성공해야 한다
    *[case(f"tool-{i + 1:02d}", "tool_call", "thinking", q, used("run_python", g), {"answer": ref, "events": TOOL_OK}, files=f) for i, (q, g, ref, f) in enumerate([
        ("문자열 '은행'을 UTF-8로 인코딩한 SHA-256 해시의 앞 8자리(16진수)를 코드로 계산해 알려주세요.", has(SHA), SHA, []),
        ("2의 100제곱을 코드로 계산해 모든 자릿수를 알려주세요.", number(2**100), str(2**100), []),
        ("1부터 1000까지의 소수는 모두 몇 개인지 코드로 세어 알려주세요.", number(PRIMES), f"{PRIMES}개", []),
        ("1부터 100까지의 정수 가운데 3의 배수이거나 7의 배수인 수의 합을 코드로 계산해 알려주세요.", number(sum(n for n in range(1, 101) if n % 3 == 0 or n % 7 == 0)), "2208", []),
        ("첨부한 매출.csv의 매출 합계를 코드로 계산해 알려주세요.", number(615), "615", ["매출.csv"]),
        ("첨부한 매출.csv에서 지점별 매출 합계가 가장 큰 지점을 코드로 찾아 알려주세요.", has("강남"), "강남", ["매출.csv"]),
        ("첨부한 매출.csv에서 2분기 매출의 평균을 코드로 계산해 알려주세요.", number(105), "105", ["매출.csv"]),
        ("첨부한 매출.csv에서 1분기보다 2분기 매출이 줄어든 지점을 코드로 찾아 알려주세요.", has("부산"), "부산", ["매출.csv"]),
        ("365일을 초로 바꾸면 몇 초인지 코드로 계산해 알려주세요.", number(365 * 86400), str(365 * 86400), []),
        ("30의 계승(30!)의 각 자릿수를 모두 더한 값을 코드로 계산해 알려주세요.", number(DIGITS), str(DIGITS), []),
    ])],

    # 작업 완결 — Agent. 계획을 승인한 뒤 끝까지 돌려, 만든 결과물을 숨은 테스트로 채점한다
    case("agent-01", "agent", "agent", "문자열이 회문이면 True를 돌려주는 함수 is_palindrome(text)를 palindrome.py에 만들어줘. 공백과 대소문자는 무시한다.",
         produced("palindrome.py", "from palindrome import is_palindrome\n\n\ndef test():\n    assert is_palindrome('A man a plan a canal Panama')\n    assert not is_palindrome('bank')\n"),
         {"answer": "", "events": [("run_done", {"ok": True})],
          "files": {"palindrome.py": "def is_palindrome(text):\n    s = text.replace(' ', '').lower()\n    return s == s[::-1]\n"}}),
    case("agent-02", "agent", "agent", "첨부한 매출.csv에서 지점별 매출 합계를 구해 {지점: 합계} 형식으로 result.json에 저장하는 분석을 만들어줘.",
         lambda answer, events, fetch: any(t == "run_done" and d["ok"] for t, d in events)
         and json.loads(fetch("result.json")) == {"강남": 270, "종로": 175, "부산": 170},
         {"answer": "", "events": [("run_done", {"ok": True})], "files": {"result.json": '{"강남": 270, "종로": 175, "부산": 170}'}},
         files=["매출.csv"]),
    case("agent-03", "agent", "agent", "첨부한 여비규정에서 일비, 서울 숙박비 상한, 정산 기한을 정리해 answer.md로 작성해줘.",
         lambda answer, events, fetch: any(t == "run_done" and d["ok"] for t, d in events)
         and "[#" in (md := fetch("answer.md").decode()) and all(w in md for w in ("25,000", "120,000", "7일")),
         {"answer": "", "events": [("run_done", {"ok": True})],
          "files": {"answer.md": "일비: 25,000원, 서울 숙박비 상한: 120,000원, 정산 기한: 7일 [#1]"}},
         files=["여비규정.txt"]),
]
