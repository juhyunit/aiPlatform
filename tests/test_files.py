"""파일 파이프라인: 형식별 파서가 격리된 샌드박스에서 돌고, 결과가 검색과 컨텍스트로 이어진다."""
import io
import zipfile

import pytest
from conftest import ROOT, model_calls

FIX = ROOT / "tests/fixtures"


def docx_bytes():
    import docx
    d = docx.Document()
    d.add_heading("여비 규정", 1)
    d.add_paragraph("숙박비는 실비로 지급한다.")
    t = d.add_table(rows=2, cols=2)
    for cell, v in zip([c for r in t.rows for c in r.cells], ["직급", "상한", "부장", "120000"]):
        cell.text = v
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def pptx_bytes():
    from pptx import Presentation
    p = Presentation()
    s = p.slides.add_slide(p.slide_layouts[1])
    s.shapes.title.text = "분기 실적"
    s.placeholders[1].text = "순이익은 전 분기보다 늘었다."
    buf = io.BytesIO()
    p.save(buf)
    return buf.getvalue()


def xlsx_bytes():
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.append(["지점", "매출"])
    for row in [["강남", 120], ["종로", 80], ["부산", 100]]:
        wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


CASES = [
    ("규정.txt", "출장 여비는 인사부가 관리한다.\n\n일비는 정액이다.".encode(), "출장 여비는 인사부가 관리한다."),
    ("규정.pdf", (FIX / "sample.pdf").read_bytes(), "제6조(일비) 일비는 출장 1일당 25,000원을 정액으로 지급한다."),
    ("규정.hwpx", (FIX / "sample.hwpx").read_bytes(), "부장 | 120,000원"),
    ("규정.docx", docx_bytes(), "부장 | 120000"),
    ("실적.pptx", pptx_bytes(), "순이익은 전 분기보다 늘었다."),
    ("매출.xlsx", xlsx_bytes(), "3행 2열"),
    ("매출.csv", "지점,매출\n강남,120\n종로,80\n".encode("cp949"), "열: 지점(str), 매출(int64)"),
    ("calc.py", b"def add(a, b):\n    return a + b\n", "def add(a, b):"),
]


@pytest.mark.parametrize("name,data,expected", CASES, ids=[c[0] for c in CASES])
def test_attachment_reaches_the_model_as_cited_data(alice, name, data, expected):
    sid = alice.session()
    doc = alice.upload(sid, name, data)
    assert doc.status_code == 200 and doc.json()["chunks"] >= 1, doc.text
    alice.chat(sid, "내용을 알려줘", mode="quick")
    last = model_calls()[-1]["messages"][-1]["content"]
    assert expected in last and "<자료>\n[#" in last and name in last


def test_table_in_docx_is_kept_whole_under_its_heading(alice):
    sid = alice.session()
    alice.upload(sid, "규정.docx", docx_bytes())
    alice.chat(sid, "숙박비 상한은?", mode="quick")
    last = model_calls()[-1]["messages"][-1]["content"]
    assert "규정.docx · 여비 규정)\n직급 | 상한\n부장 | 120000" in last


@pytest.mark.parametrize("name,data,reason", [
    ("old.hwp", b"\xd0\xcf\x11\xe0", "HWPX 또는 PDF"),
    ("fake.pdf", b"MZ\x90\x00 not a pdf", "확장자와 맞지 않습니다"),
    ("scan.pdf", None, "스캔 문서"),
    ("bomb.docx", None, "허용 범위"),
])
def test_bad_files_are_refused_with_a_reason(alice, name, data, reason):
    if name == "scan.pdf":      # 글자가 없는 한 쪽짜리 PDF
        import pypdfium2
        pdf = pypdfium2.PdfDocument.new()
        pdf.new_page(200, 200)
        buf = io.BytesIO()
        pdf.save(buf)
        data = buf.getvalue()
    if name == "bomb.docx":     # 압축비가 지나치게 큰 zip
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", b"0" * 50_000_000)
        data = buf.getvalue()
    r = alice.upload(alice.session(), name, data)
    assert r.status_code == 422 and reason in r.json()["detail"], r.text


def test_untagged_upload_is_treated_as_top_grade(alice, bob):
    assert alice.upload(alice.session(), "a.txt", b"x", grade=None).status_code == 403
    assert bob.upload(bob.session("personal"), "a.txt", b"x", grade=None).status_code == 200


def test_attachment_grade_raises_request_grade_above_session_grade(bob):
    """세션은 공개 등급이어도 개인신용정보 파일을 붙이면 외부 모델로는 갈 수 없다."""
    sid = bob.session("public")
    assert bob.post("/api/chat", json={"session_id": sid, "message": "x", "model": "cloud", "mode": "quick"}).status_code == 200
    bob.upload(sid, "고객.txt", "고객 원장".encode(), grade="personal")
    assert bob.post("/api/chat", json={"session_id": sid, "message": "x", "model": "cloud", "mode": "quick"}).status_code == 403


def test_image_goes_to_a_vision_model_as_image_part(alice):
    sid = alice.session()
    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")
    assert alice.upload(sid, "chart.png", png).json()["chunks"] == 0
    alice.chat(sid, "이 그림을 설명해줘", mode="quick", images=["chart.png"])
    call = model_calls()[-1]
    assert call["model"] == "small"       # 비전이 되는 모델만 후보다
    assert call["messages"][-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")
