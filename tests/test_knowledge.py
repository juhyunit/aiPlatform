"""RAG와 권한 필터. 권한은 검색 뒤가 아니라 검색 조건이다."""
import itertools

import pytest
from conftest import DATA, Client, admin, model_calls

from app import knowledge


def test_keywords_are_morphemes_not_whitespace_tokens():
    assert {"여비", "지급"} <= set(knowledge.keywords("출장 여비를 지급한다"))


def test_chunk_follows_headings_and_never_splits_a_table():
    table = {"kind": "table", "text": "\n".join(f"행{i} | 값" for i in range(300)), "page": 2}
    chunks = knowledge.chunk([
        {"kind": "heading", "text": "1장", "page": 1, "level": 1},
        {"kind": "heading", "text": "1절", "page": 1, "level": 2},
        {"kind": "paragraph", "text": "가" * 300, "page": 1},
        {"kind": "paragraph", "text": "나" * 300, "page": 2},
        table,
        {"kind": "heading", "text": "2장", "page": 3, "level": 1},
        {"kind": "paragraph", "text": "다", "page": 3},
    ])
    assert [(c["heading"], c["page"]) for c in chunks] == [("1장 > 1절", 1), ("1장 > 1절", 2), ("1장 > 1절", 2), ("2장", 3)]
    assert chunks[2]["text"] == table["text"]


def test_chunk_splits_oversized_paragraph_along_lines():
    long_para = "\n".join(f"{i}조 규정 본문 " + "가" * 80 for i in range(20))
    chunks = knowledge.chunk([{"kind": "paragraph", "text": long_para, "page": 1}])
    assert len(chunks) > 1 and all(knowledge.context.tokens(c["text"]) <= knowledge.CHUNK_TOKENS for c in chunks)


def retrieved(client, sid, question, **kw) -> str:
    client.chat(sid, question, mode="quick", search=True, **kw)
    return model_calls()[-1]["messages"][-1]["content"]


@pytest.fixture(scope="module")
def library():
    """등급 다섯 가지의 사내 문서와, 접근 그룹이 걸린 문서 둘."""
    for grade in ["public", "internal", "restricted", "pseudonymized", "personal"]:
        path = DATA / f"연체율-{grade}.txt"
        path.write_text(f"연체율 관리 기준 문서. 표식 MARK-{grade}.", encoding="utf-8")
        admin("index", "librarian", str(path), grade)
    for group in ["retail", "risk"]:
        path = DATA / f"연체율-{group}.txt"
        path.write_text(f"연체율 관리 기준 문서. 표식 GROUP-{group}.", encoding="utf-8")
        admin("index", "librarian", str(path), "internal", group)


GRADES = ["public", "internal", "restricted", "pseudonymized", "personal"]
USERS = {"alice": 1, "carol": 2, "bob": 4}      # 사용자 → 열람 등급
MODELS = {"small": 4, "cloud": 0}               # 모델 → 실행 환경이 받을 수 있는 최고 등급


@pytest.mark.parametrize("user,model", list(itertools.product(USERS, MODELS)))
def test_every_combination_of_user_grade_document_grade_and_model_env(library, user, model):
    client = Client(user)
    text = retrieved(client, client.session("public"), "연체율 관리 기준", model=model)
    seen = {g for g in GRADES if f"MARK-{g}." in text}
    assert seen == {g for i, g in enumerate(GRADES) if i <= min(USERS[user], MODELS[model])}


def test_access_group_must_match(library, alice, bob):
    assert "GROUP-retail" in (a := retrieved(alice, alice.session(), "연체율 관리 기준")) and "GROUP-risk" not in a
    assert "GROUP-risk" in (b := retrieved(bob, bob.session(), "연체율 관리 기준")) and "GROUP-retail" not in b


def test_org_knowledge_is_searched_only_when_asked(library, alice):
    alice.chat(alice.session(), "연체율 관리 기준", mode="quick")
    assert "MARK-" not in model_calls()[-1]["messages"][-1]["content"]


def test_attachments_stay_in_their_session_and_with_their_owner(alice, carol):
    sid = alice.session()
    alice.upload(sid, "메모.txt", "연체율 관리 기준 비공개 메모 SECRET-NOTE".encode())
    assert "SECRET-NOTE" in retrieved(alice, sid, "연체율 관리 기준")
    assert "SECRET-NOTE" not in retrieved(alice, alice.session(), "연체율 관리 기준")
    assert "SECRET-NOTE" not in retrieved(carol, carol.session(), "연체율 관리 기준")


def test_large_attachment_is_searched_instead_of_stuffed(alice):
    """예산을 넘는 첨부는 전문을 넣지 않고, 질문과 관련된 대목만 찾아 넣는다."""
    sid = alice.session()
    paragraphs = [f"제{i}조 일반 조항. " + "가나다라마바사 " * 40 for i in range(60)]
    paragraphs[41] = "제41조 휴면계좌 복원 수수료는 면제한다. " + "가나다라마바사 " * 40
    alice.upload(sid, "약관.txt", "\n\n".join(paragraphs).encode())
    alice.chat(sid, "휴면계좌 복원 수수료", mode="quick")
    last = model_calls()[-1]["messages"][-1]["content"]
    assert "휴면계좌 복원 수수료는 면제한다" in last and last.count("[#") <= 8


def test_memory_is_private_and_can_be_deleted(alice, carol):
    mem = alice.post("/api/memories", json={"text": "보고서는 항상 개조식으로 받는다 MEMO-1"}).json()["id"]
    alice.chat(alice.session(), "안녕", mode="quick")
    assert "MEMO-1" in model_calls()[-1]["messages"][0]["content"]
    carol.chat(carol.session(), "안녕", mode="quick")
    assert "MEMO-1" not in model_calls()[-1]["messages"][0]["content"]
    alice.delete(f"/api/memories/{mem}")
    alice.chat(alice.session(), "안녕", mode="quick")
    assert "MEMO-1" not in model_calls()[-1]["messages"][0]["content"]


@pytest.fixture
def carol():
    return Client("carol")
