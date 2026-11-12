"""Knowledge·Memory. 청킹, 색인, 하이브리드 검색. 첨부 파일·기억·사내 지식이 같은 표와 같은 등급 규칙을 쓴다."""
import re

from kiwipiepy import Kiwi

from . import context, db, gateway
from .policy import HARNESS

CHUNK_TOKENS = 400
_kiwi = Kiwi()

# 권한 필터. 검색 뒤가 아니라 검색 조건이다: 문서 등급 ≤ 사용자 등급, 문서 등급 ≤ 모델 등급, 소유자 또는 접근 그룹.
_VISIBLE = """
  d.grade <= %(clear)s
  and (d.owner = %(user)s or (d.scope = 'org' and (d.groups = '{}' or d.groups && %(groups)s::text[])))
  and (d.session_id = %(session)s or (d.scope = 'org' and %(org)s))
"""
_SEARCH = f"""
with dense as (
  select c.id, row_number() over (order by c.embedding <=> %(vec)s::vector) as r
  from chunks c join documents d on d.id = c.document_id where {_VISIBLE}
  order by c.embedding <=> %(vec)s::vector limit 30),
sparse as (
  select c.id, row_number() over (order by ts_rank(c.tsv, q) desc) as r
  from chunks c join documents d on d.id = c.document_id, to_tsquery('simple', %(tsq)s) q
  where c.tsv @@ q and {_VISIBLE} order by ts_rank(c.tsv, q) desc limit 30)
select c.id, d.name, c.page, c.heading, c.text
from (select id, sum(1.0 / (60 + r)) as score from (select * from dense union all select * from sparse) u group by id) f
join chunks c on c.id = f.id join documents d on d.id = c.document_id
order by f.score desc, c.id limit %(k)s
"""


def keywords(text: str) -> list[str]:
    """형태소 분석으로 뽑은 검색어. 한국어는 공백 단위로 자르면 조사가 붙어 검색이 되지 않는다."""
    return [t.form.lower() for t in _kiwi.tokenize(text)
            if (t.tag[0] in "NV" or t.tag in ("SL", "SN", "SH", "XR")) and re.fullmatch(r"\w+", t.form)]


def chunk(blocks: list[dict]) -> list[dict]:
    """공통 문서 표현을 제목 구조에 따라 자른다. 표는 쪼개지 않는다."""
    chunks, path, buf, page = [], {}, [], None

    def flush():
        if buf:
            chunks.append({"page": page, "heading": " > ".join(path[k] for k in sorted(path)), "text": "\n".join(buf)})
            buf.clear()

    for b in blocks:
        if b["kind"] == "heading":
            flush()
            path = {k: v for k, v in path.items() if k < b["level"]} | {b["level"]: b["text"]}
        elif b["kind"] == "table":
            flush()
            page = b["page"]
            buf.append(b["text"])
            flush()
        else:
            parts = b["text"].splitlines() if context.tokens(b["text"]) > CHUNK_TOKENS else [b["text"]]
            for part in (p for p in parts if p.strip()):
                if buf and context.tokens("\n".join(buf)) + context.tokens(part) > CHUNK_TOKENS:
                    flush()
                if not buf:
                    page = b["page"]
                buf.append(part)
    flush()
    return chunks


async def index(user: dict, scope: str, name: str, grade: int, chunks: list[dict],
                session_id=None, groups: list[str] = ()) -> str:
    """문서 한 건을 색인한다. 임베딩 모델도 문서 등급으로 정책 필터를 거친다."""
    model, vectors = await gateway.embed([c["text"] for c in chunks], grade)
    tsvs = [" ".join(keywords(c["heading"] + " " + c["text"])) for c in chunks]
    async with db.tx(user) as conn:
        doc_id = (await (await conn.execute(
            "insert into documents (scope, owner, session_id, name, grade, groups, embed_model)"
            " values (%s, %s, %s, %s, %s, %s, %s) returning id",
            (scope, user["id"], session_id, name, grade, list(groups), model))).fetchone())["id"]
        if chunks:
            async with conn.cursor() as cur:
                await cur.executemany(
                    "insert into chunks (document_id, page, heading, text, tsv, embedding)"
                    " values (%s, %s, %s, %s, to_tsvector('simple', %s), %s::vector)",
                    [(doc_id, c["page"], c["heading"], c["text"], t, str(v)) for c, t, v in zip(chunks, tsvs, vectors)])
    return doc_id


def _params(user: dict, clear: int, session_id, org: bool) -> dict:
    return {"clear": min(clear, user["clearance"]), "user": user["id"], "groups": user["groups"],
            "session": session_id, "org": org}


async def search(user: dict, clear: int, query: str, session_id, org: bool, k: int = 8) -> list[dict]:
    """밀집 검색과 키워드 검색을 순위 융합(RRF)으로 합친다. clear는 답할 모델이 받을 수 있는 최고 등급이다."""
    _, [vec] = await gateway.embed([query], clear)
    words = list(dict.fromkeys(keywords(query))) if HARNESS["keyword_search"] else []
    return await db.q(user, _SEARCH, {**_params(user, clear, session_id, org), "vec": str(vec),
                                      "tsq": " | ".join(words) or None, "k": k})


async def attached(user: dict, clear: int, session_id, budget: int) -> list[dict] | None:
    """세션 첨부가 예산 안에 들어가면 전문을 문서 순서대로 돌려준다. 넘치면 None(검색으로 처리)."""
    sql = f"""select c.id, d.name, c.page, c.heading, c.text from chunks c join documents d on d.id = c.document_id
              where d.scope = 'session' and {_VISIBLE} order by d.created_at, c.id"""
    rows = await db.q(user, sql, _params(user, clear, session_id, False))
    return rows if sum(context.tokens(cite(r)) for r in rows) <= budget else None


async def memories(user: dict, clear: int) -> list[str]:
    """사용자가 저장한 장기 기억. 수가 적어 검색하지 않고 등급이 맞는 것을 모두 돌려준다."""
    rows = await db.q(user, """select c.text from chunks c join documents d on d.id = c.document_id
                               where d.scope = 'memory' and d.owner = %s and d.grade <= %s order by c.id""",
                      (user["id"], min(clear, user["clearance"])))
    return [r["text"] for r in rows]


def cite(row: dict) -> str:
    """컨텍스트에 넣는 모양. 답변은 [#번호]로 인용한다."""
    where = row["name"] + (f" {row['page']}쪽" if row["page"] else "") + (f" · {row['heading']}" if row["heading"] else "")
    return f"[#{row['id']}] ({where})\n{row['text']}"
