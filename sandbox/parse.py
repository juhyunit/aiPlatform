"""파일 파서. 샌드박스 안에서 실행되고, 파일 하나를 공통 문서 표현(블록 목록)으로 바꿔 JSON으로 출력한다.

블록: {"kind": "heading" | "paragraph" | "table", "text": str, "page": int | null, "level": int, "cells": [[str]]}
사용법: python /opt/parse.py <파일>
"""
import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

MAX_PAGES, MAX_UNZIPPED, MAX_RATIO = 2000, 500 * 2**20, 200


def fail(message: str):
    print(json.dumps({"error": message}, ensure_ascii=False))
    sys.exit(0)


def block(kind, text, page=None, **extra):
    return {"kind": kind, "text": text.strip(), "page": page, **extra}


def table(rows, page=None):
    return block("table", "\n".join(" | ".join(r) for r in rows), page, cells=rows)


def check_zip(path):
    """압축 폭탄 방지. 풀린 크기와 압축비에 상한을 둔다."""
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
    except zipfile.BadZipFile:
        fail("손상된 압축 문서입니다")
    size = sum(i.file_size for i in infos)
    if size > MAX_UNZIPPED or size > MAX_RATIO * max(1, sum(i.compress_size for i in infos)):
        fail("압축을 풀었을 때 크기가 허용 범위를 넘습니다")


def read_text(path) -> str:
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    fail("글자 인코딩을 알 수 없습니다. UTF-8 또는 CP949로 저장해 주세요")


def pdf(path):
    import pypdfium2
    doc = pypdfium2.PdfDocument(path)
    if len(doc) > MAX_PAGES:
        fail(f"쪽수가 {MAX_PAGES}쪽을 넘습니다")
    blocks = [block("paragraph", line, n + 1)
              for n, page in enumerate(doc) for line in page.get_textpage().get_text_range().splitlines() if line.strip()]
    if not blocks:
        fail("글자가 없는 PDF입니다(스캔 문서로 보입니다). OCR 엔진이 확정되기 전에는 텍스트가 든 PDF만 처리합니다")
    return blocks


def docx(path):
    import docx as python_docx
    from docx.table import Table
    blocks = []
    for item in python_docx.Document(path).iter_inner_content():
        if isinstance(item, Table):
            blocks.append(table([[c.text.strip() for c in row.cells] for row in item.rows]))
        elif item.text.strip():
            style = item.style.name
            if style.startswith(("Heading", "제목")) and style[-1].isdigit():
                blocks.append(block("heading", item.text, level=int(style[-1])))
            else:
                blocks.append(block("paragraph", item.text))
    return blocks


def pptx(path):
    from pptx import Presentation
    blocks = []
    for n, slide in enumerate(Presentation(path).slides, 1):
        for shape in slide.shapes:
            if shape.has_table:
                blocks.append(table([[c.text.strip() for c in row.cells] for row in shape.table.rows], n))
            elif shape.has_text_frame and shape.text_frame.text.strip():
                is_title = shape == slide.shapes.title
                blocks.append(block("heading" if is_title else "paragraph", shape.text_frame.text, n, **({"level": 1} if is_title else {})))
    return blocks


def sheet(path):
    """표 데이터는 프롬프트에 붓지 않는다. 열 이름, 자료형, 표본 행, 요약 통계만 낸다. 원본은 작업 폴더에 그대로 있다."""
    import pandas
    if path.lower().endswith(".csv"):
        import io
        frames = {"": pandas.read_csv(io.StringIO(read_text(path)))}
    else:
        frames = pandas.read_excel(path, sheet_name=None)
    blocks = []
    for name, df in frames.items():
        columns = ", ".join(f"{c}({t})" for c, t in df.dtypes.astype(str).items())
        text = (f"파일 {Path(path).name}" + (f" 시트 {name}" if name else "") + f": {len(df)}행 {len(df.columns)}열\n"
                f"열: {columns}\n표본:\n{df.head(5).to_csv(index=False)}요약 통계:\n{df.describe(include='all').round(2).to_csv()}")
        blocks.append(block("table", text))
    return blocks


def hwpx(path):
    """HWPX는 zip 안의 XML이다. 문단(hp:p)과 표(hp:tbl)만 읽는다. 차트, 글상자, 머리말·꼬리말은 읽지 않는다."""
    def tag(el):
        return el.tag.rsplit("}", 1)[-1]

    def own_text(p):     # 문단 바로 아래의 글만. 표 안쪽은 따로 읽는다
        return "".join("".join(t.itertext()) for run in p if tag(run) == "run" for t in run if tag(t) == "t")

    blocks = []
    with zipfile.ZipFile(path) as archive:
        for name in sorted(n for n in archive.namelist() if n.startswith("Contents/section") and n.endswith(".xml")):
            for p in ElementTree.fromstring(archive.read(name)):
                if tag(p) != "p":
                    continue
                if own_text(p).strip():
                    blocks.append(block("paragraph", own_text(p)))
                for tbl in (el for run in p if tag(run) == "run" for el in run if tag(el) == "tbl"):
                    blocks.append(table([[" ".join(own_text(q) for q in tc.iter() if tag(q) == "p").strip()
                                          for tc in tr if tag(tc) == "tc"] for tr in tbl if tag(tr) == "tr"]))
    return blocks


def text(path):
    return [block("paragraph", part) for part in read_text(path).split("\n\n") if part.strip()]


PARSERS = {".pdf": pdf, ".docx": docx, ".pptx": pptx, ".xlsx": sheet, ".csv": sheet, ".hwpx": hwpx}

if __name__ == "__main__":
    path = sys.argv[1]
    suffix = Path(path).suffix.lower()
    if suffix in (".docx", ".pptx", ".xlsx", ".hwpx"):
        check_zip(path)
    blocks = PARSERS.get(suffix, text)(path)
    if not blocks:
        fail("문서에서 읽을 수 있는 글자가 없습니다")
    print(json.dumps({"blocks": blocks}, ensure_ascii=False))
