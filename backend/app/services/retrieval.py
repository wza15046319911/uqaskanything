"""
retrieval.py — 统一检索层。
四种检索 + 安全网,全部接收 psycopg 连接 conn,返回 list[dict]。

分工(沿用项目「确定性决策用代码,语言任务交模型」):
  - 本模块全是确定性活:SELECT-only 拦截、SQL 拼装、向量检索、全文检索、RRF 融合。
  - 不调 LLM;query_en/semantic_en 由上层(query.py 的 LLM 规划)给好的英文主题词。

公开函数:
  - guard_where(where) -> str
  - ensure_fts_index(conn) -> None  # 启动时一次性建 FTS 索引,读路径不再建
  - filter_search(conn, where) -> list[dict]
  - semantic_search(conn, query_en, k=8, min_sim=0.45) -> list[dict]
  - keyword_search(conn, query_en, k=20) -> list[dict]
  - hybrid_search(conn, where, semantic_en, k=8) -> list[dict]

返回 dict 字段:code, title, semester, level, units, has_exam,语义/混合附带 sim。
"""
from __future__ import annotations
import os
import re

import requests
import psycopg

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = "bge-m3"

# 固定输出列,顺序固定;dict 化时用这套 key
SELECT_COLS = "code, title, semester, level, units, has_exam"
RESULT_KEYS = ("code", "title", "semester", "level", "units", "has_exam")

# 全文检索表达式(必须和建索引时的表达式逐字一致,否则索引用不上)
TSV_EXPR = ("to_tsvector('english', coalesce(title,'') || ' ' || "
            "coalesce(code,'') || ' ' || coalesce(search_blob,''))")

RRF_K = 60  # RRF 常数,业界默认 60

# where 白名单:只允许这些结构化列(文本主题列不在内,主题走 semantic)
ALLOWED_COLS = (
    "code", "semester", "year", "location", "attendance_mode",
    "level", "units", "has_exam", "has_hurdle",
)

# 单个比较条件的合法形态:{白名单列} {运算符} {字面量}
#   - 字面量:单引号字符串 / 数字 / true|false|null
#   - IN:仅字面量列表(无子查询),如 IN ('A','B') 或 IN (1,2)
_COL = r"(?:" + "|".join(ALLOWED_COLS) + r")"
_CMP = r"(?:=|!=|<>|<=|>=|<|>)"
_LIT = r"(?:'[^']*'|-?\d+(?:\.\d+)?|true|false|null)"
_IN_LIST = r"\(\s*" + _LIT + r"(?:\s*,\s*" + _LIT + r")*\s*\)"
_COND = (
    r"\s*" + _COL + r"\s*"
    + r"(?:" + _CMP + r"\s*" + _LIT
    + r"|in\s*" + _IN_LIST + r")\s*"
)
# 整体:条件用 AND/OR 连接,禁止括号/函数/逗号/子查询/SELECT
WHERE_WHITELIST = re.compile(
    r"^" + _COND + r"(?:(?:and|or)" + _COND + r")*$", re.I
)
# 校验前再兜底拦一层危险词(字符串字面量已被剥离,不会误杀值里的 select)
DANGEROUS = re.compile(r"(;|--|/\*|\bselect\b)", re.I)


def guard_where(where: str) -> str:
    """
    SELECT-only 安全网(白名单结构)。
    WHERE 只允许「{白名单列} {比较运算符} {字面量}」用 AND/OR 连接;
    禁止 括号(IN 列表除外)、函数调用、逗号、子查询、SELECT。
    任何不符合的整体 raise ValueError;通过则返回去空白后的原始 where。
    """
    if not where or not where.strip():
        raise ValueError("where 不能为空")
    w = where.strip()
    # 先把单引号字符串字面量替成空串,避免值里含 select/and 等被白名单/危险词误判
    stripped = re.sub(r"'[^']*'", "''", w)
    if DANGEROUS.search(stripped):
        raise ValueError(f"where 含非法内容(分号/注释/SELECT),已拦截:{where!r}")
    if not WHERE_WHITELIST.fullmatch(stripped):
        raise ValueError(f"where 不符合白名单(仅允许 列 运算符 字面量 经 AND/OR 连接):{where!r}")
    return w


def _embed(text: str) -> str:
    """取 bge-m3 向量并转成 pgvector 字面量。"""
    v = requests.post(
        f"{OLLAMA}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=120,
    ).json()["embedding"]
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def _row_to_dict(row, with_sim: bool = False) -> dict:
    """把固定列顺序的元组转 dict;with_sim 时末列是 sim。"""
    d = dict(zip(RESULT_KEYS, row[: len(RESULT_KEYS)]))
    if with_sim:
        d["sim"] = float(row[len(RESULT_KEYS)])
    return d


def ensure_fts_index(conn) -> None:
    """
    建 GIN 表达式索引(幂等),供集成层启动时一次性调用(写连接)。
    读路径(keyword/semantic/hybrid)不再建索引,只 SELECT,以兼容只读连接。
    """
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_courses_fts ON courses USING gin({TSV_EXPR})"
    )
    conn.commit()


def filter_search(conn, where: str) -> list[dict]:
    """纯结构化过滤:SELECT 固定列 FROM courses WHERE {guard(where)} ORDER BY code。"""
    where = guard_where(where)
    sql = f"SELECT {SELECT_COLS} FROM courses WHERE {where} ORDER BY code"
    out: list[dict] = []
    seen_codes: set = set()         # 同课跨学期多 offering 按课去重(ORDER BY code 使重复相邻,保留首条)
    for r in conn.execute(sql).fetchall():
        d = _row_to_dict(r)
        if d["code"] in seen_codes:
            continue
        seen_codes.add(d["code"])
        out.append(d)
    return out


def semantic_search(conn, query_en: str, k: int = 8, min_sim: float = 0.45) -> list[dict]:
    """
    向量检索 + 关键词 RRF 融合。
    取候选(向量 k*3 + 全文 k*3),按 RRF 融合排序,只留向量 sim>=min_sim,取前 k。
    融合是为了让「课名就叫 Machine Learning」这种被全文命中的课能排上来。
    """
    if not query_en or not query_en.strip():
        raise ValueError("query_en 不能为空")
    return _fused_search(conn, where=None, query_en=query_en, k=k, min_sim=min_sim)


def keyword_search(conn, query_en: str, k: int = 20) -> list[dict]:
    """Postgres 全文检索:websearch_to_tsquery 匹配,ts_rank 排序,取前 k。"""
    if not query_en or not query_en.strip():
        raise ValueError("query_en 不能为空")
    sql = (
        f"SELECT {SELECT_COLS} FROM courses "
        f"WHERE {TSV_EXPR} @@ websearch_to_tsquery('english', %s) "
        f"ORDER BY ts_rank({TSV_EXPR}, websearch_to_tsquery('english', %s)) DESC, code "
        f"LIMIT %s"
    )
    rows = conn.execute(sql, (query_en, query_en, k)).fetchall()
    return [_row_to_dict(r) for r in rows]


def hybrid_search(conn, where: str | None, semantic_en: str, k: int = 8) -> list[dict]:
    """
    结构化 where(可空,空=全表)过滤后,RRF 融合「向量排序」与「全文检索排序」,取前 k。
    where 为空时等价于 semantic_search 但不卡 min_sim(混合通常已有结构条件收窄)。
    """
    if not semantic_en or not semantic_en.strip():
        raise ValueError("semantic_en 不能为空")
    return _fused_search(conn, where=where, query_en=semantic_en, k=k, min_sim=0.0)


def _fused_search(conn, where, query_en, k, min_sim) -> list[dict]:
    """
    RRF 融合核心:在同一个候选集合(可被 where 过滤)上分别取向量排序和全文排序,
    用 score=Σ 1/(RRF_K+rank) 融合,返回带 sim 的 top-k。
    """
    where = guard_where(where) if where and where.strip() else None
    filt = f"WHERE {where}" if where else ""

    vec = _embed(query_en)
    pool = k * 3  # 每路候选量,取大些保证融合后还够 k 个

    # 向量路:offering_id -> (rank, sim)
    vec_sql = (
        f"SELECT offering_id, {SELECT_COLS}, 1-(embedding<=>%s::vector) AS sim "
        f"FROM courses {filt} "
        f"ORDER BY embedding<=>%s::vector LIMIT %s"
    )
    vec_rows = conn.execute(vec_sql, (vec, vec, pool)).fetchall()

    # 全文路:offering_id -> rank(命中即入,没命中不入)
    kw_filt = filt + (" AND " if filt else "WHERE ") + \
        f"{TSV_EXPR} @@ websearch_to_tsquery('english', %s)"
    kw_sql = (
        f"SELECT offering_id FROM courses {kw_filt} "
        f"ORDER BY ts_rank({TSV_EXPR}, websearch_to_tsquery('english', %s)) DESC, code "
        f"LIMIT %s"
    )
    kw_rows = conn.execute(kw_sql, (query_en, query_en, pool)).fetchall()

    # 缓存行数据 + sim(用向量路那份,行内已带 sim);vec_oids 标记向量召回过的条目
    info: dict = {}
    vec_oids: set = set()
    for row in vec_rows:
        oid = row[0]
        vec_oids.add(oid)
        info[oid] = {"row": row[1:1 + len(RESULT_KEYS)], "sim": float(row[-1])}

    # RRF 累加(rank 从 1 起,符合 RRF 原始定义)
    score: dict = {}
    for rank, row in enumerate(vec_rows, start=1):
        oid = row[0]
        score[oid] = score.get(oid, 0.0) + 1.0 / (RRF_K + rank)
    for rank, row in enumerate(kw_rows, start=1):
        oid = row[0]
        score[oid] = score.get(oid, 0.0) + 1.0 / (RRF_K + rank)
        if oid not in info:
            # 全文命中但不在向量候选里:补查该行 + 真实 sim
            r = conn.execute(
                f"SELECT {SELECT_COLS}, 1-(embedding<=>%s::vector) AS sim "
                f"FROM courses WHERE offering_id=%s",
                (vec, oid),
            ).fetchone()
            info[oid] = {"row": r[:len(RESULT_KEYS)], "sim": float(r[-1])}

    # 按融合分排序;min_sim 只卡「纯向量召回」条目,纯全文命中豁免阈值,取前 k
    ranked = sorted(score.items(), key=lambda kv: kv[1], reverse=True)
    out: list[dict] = []
    seen_codes: set = set()         # 同一门课跨学期有多 offering,只保留融合分最高的一条,避免占满 top-k 槽
    for oid, _ in ranked:
        meta = info[oid]
        if oid in vec_oids and meta["sim"] < min_sim:
            continue
        d = dict(zip(RESULT_KEYS, meta["row"]))
        if d["code"] in seen_codes:
            continue
        seen_codes.add(d["code"])
        d["sim"] = meta["sim"]
        out.append(d)
        if len(out) >= k:
            break
    return out


if __name__ == "__main__":
    from app.core.config import DSN

    def show(title, rows, n=3, sim=False):
        print(f"\n== {title}  (命中 {len(rows)}) ==")
        for d in rows[:n]:
            tail = f"  sim={d['sim']:.3f}" if sim and "sim" in d else ""
            print(f"  {d['code']}  {d['title']}  ({d['semester']}, {d['level']}, "
                  f"units={d['units']}, exam={d['has_exam']}){tail}")

    # guard_where 白名单验证:必须拒的 + 必须放行的
    must_reject = [
        "", "pg_sleep(0.4) IS NULL", "code IN (SELECT code FROM programs)",
        "true", "1=1", "has_exam=false; drop table courses",
        "title ilike '%ml%'", "1=1 -- x",
    ]
    must_pass = [
        "has_exam=false",
        "level='Postgraduate Coursework' AND units=2",
        "location='St Lucia'",
    ]
    print("== guard_where 白名单验证 ==")
    for bad in must_reject:
        try:
            guard_where(bad)
            print(f"  !! 未拦截(应拦):{bad!r}")
        except ValueError:
            print(f"  OK 已拦截 {bad!r}")
    for good in must_pass:
        try:
            guard_where(good)
            print(f"  OK 已放行 {good!r}")
        except ValueError as e:
            print(f"  !! 误杀(应放行):{good!r} -> {e}")

    # 启动时一次性建索引(写连接)
    with psycopg.connect(DSN) as conn:
        ensure_fts_index(conn)

    # 读路径在只读连接下也要能跑(只 SELECT,不建索引/不 commit)
    with psycopg.connect(DSN) as conn:
        conn.read_only = True

        f_rows = filter_search(conn, "has_exam=false")
        print(f"\n== filter has_exam=false 计数: {len(f_rows)} ==")
        show("filter has_exam=false 前3条", f_rows)

        s_rows = semantic_search(conn, "machine learning")
        show("semantic 'machine learning' top3", s_rows, sim=True)

        k_rows = keyword_search(conn, "machine learning")
        show("keyword 'machine learning' top3", k_rows)

        h_rows = hybrid_search(conn, "level='Postgraduate Coursework'", "data science")
        show("hybrid level=Postgraduate Coursework + 'data science' top3", h_rows, sim=True)

    print("\n自测完成。")
