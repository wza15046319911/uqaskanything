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
  - semantic_search(conn, query_en, k=8, min_sim=SEMANTIC_MIN_SIM=0.50) -> list[dict]
  - keyword_search(conn, query_en, k=20) -> list[dict]
  - hybrid_search(conn, where, semantic_en, k=8) -> list[dict]
  - course_detail(conn, code) -> dict | None  # 单门课完整详情(介绍/先修/考核/开课)

返回 dict 字段:code, title, semester, level, units, has_exam,语义/混合附带 sim。
"""
from __future__ import annotations
import os
import re

import requests
import psycopg

from app.services import reranker

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = "bge-m3"

# 固定输出列,顺序固定;dict 化时用这套 key
SELECT_COLS = "code, title, semester, level, units, has_exam, has_hurdle, midterm_status, group_status"
RESULT_KEYS = ("code", "title", "semester", "level", "units", "has_exam", "has_hurdle", "midterm_status", "group_status")

# 官方课程页(每个答案都带可跳转的官方来源,见 student-facing 红线 2)。course.html?course_code=
# 是课程总览页(列出各学期 offering),按课程码定位,稳定可跳。
COURSE_PROFILE_URL = "https://programs-courses.uq.edu.au/course.html?course_code={}"

# 全文检索表达式(必须和建索引时的表达式逐字一致,否则索引用不上)
TSV_EXPR = ("to_tsvector('english', coalesce(title,'') || ' ' || "
            "coalesce(code,'') || ' ' || coalesce(search_blob,''))")

RRF_K = 60  # RRF 常数,业界默认 60
# 语义/混合检索的向量召回下限:低于此的纯向量条目当噪声丢弃(全文命中豁免)。
# 0.50 由 sim 分布实测定:真实主题的相关课最低约 0.515(finance 0.528/AI 0.515/统计 0.531),
# 噪声多在 <0.50(AI 里的 Marketing 0.490/Law 0.499、纯虚构主题整组),0.50 是不误伤真命中、
# 又能砍明确噪声的分界。残留的 0.50~0.55 off-topic 是 bi-encoder 固有局限,归 reranker(P1)。
SEMANTIC_MIN_SIM = 0.50

# where 白名单:只允许这些结构化列(文本主题列不在内,主题走 semantic)
ALLOWED_COLS = (
    "code", "semester", "year", "location", "attendance_mode",
    "level", "units", "has_exam", "has_hurdle", "course_type", "midterm_status",
    "group_status",
)

# 单个比较条件的合法形态:{白名单列} {运算符} {字面量}
#   - 字面量:单引号字符串 / 数字 / true|false|null
#   - IN:所有白名单列;字面量列表(无子查询),如 IN ('A','B') 或 IN (1,2)
#   - NOT IN:仅 course_type(NOT NULL 列,无三值逻辑漏排风险);两种否定写法都收:
#     `course_type NOT IN (...)` 与 `NOT course_type IN (...)`。可空列禁 NOT IN(会静默漏掉 NULL 行)。
_COL = r"(?:" + "|".join(ALLOWED_COLS) + r")"
_CMP = r"(?:=|!=|<>|<=|>=|<|>)"
_LIT = r"(?:'[^']*'|-?\d+(?:\.\d+)?|true|false|null)"
_IN_LIST = r"\(\s*" + _LIT + r"(?:\s*,\s*" + _LIT + r")*\s*\)"
_COND = (
    r"\s*(?:"
    + r"not\s+course_type\s+in\s*" + _IN_LIST
    + r"|course_type\s+not\s+in\s*" + _IN_LIST
    + r"|" + _COL + r"\s*(?:" + _CMP + r"\s*" + _LIT + r"|in\s*" + _IN_LIST + r")"
    + r")\s*"
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
    # 剥离字面量后只该剩 ASCII(列名/运算符/数字/布尔)。非 ASCII(如 NBSP 等 unicode 空白)
    # 会被 \s 放过却让 Postgres 报错 -> 在此拦掉,避免绕过白名单后 500。
    if not stripped.isascii():
        raise ValueError(f"where 含非 ASCII 字符,已拦截:{where!r}")
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
    """把固定列顺序的元组转 dict;with_sim 时末列是 sim。每行带官方课程页 profile_url(红线 2)。"""
    d = dict(zip(RESULT_KEYS, row[: len(RESULT_KEYS)]))
    d["profile_url"] = COURSE_PROFILE_URL.format(d["code"]) if d.get("code") else None
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


# order_by 白名单:键 -> 固定 ORDER BY 子句(确定性查表,绝不拼用户串,防注入)。
_ORDER_BY = {
    "code": "code",
    # 客观考核负担:考核项数升序(非数组的排最后),同数按 code。供「低负担/躺平」查询用。
    "assessments_asc": ("(CASE WHEN jsonb_typeof(assessments)='array' "
                        "THEN jsonb_array_length(assessments) ELSE 999 END) ASC, code"),
}


def _coord_clause(coord_units) -> tuple[str, list]:
    """学院/学科 -> coordinating_unit 限定:返回 (SQL 片段, 参数列表)。
    coordinating_unit 是文本列(不进 guard_where 白名单),只走参数化 IN,值来自代码侧受控映射。"""
    units = [u for u in (coord_units or []) if u]
    if not units:
        return "", []
    return f"coordinating_unit IN ({','.join(['%s'] * len(units))})", units


def _title_exclude_clause(exclude_title) -> tuple[str, list]:
    """课程性质标题排除 -> (SQL 片段, 参数列表)。title 是文本列(不进 guard_where 白名单),
    只走参数化 NOT ILIKE,值来自 planner 受控词表(capstone/review/project…),注入安全。
    coalesce 避免 NULL 标题被 NOT ILIKE 静默排掉(NULL NOT ILIKE 结果为 NULL→不命中)。"""
    kws = [k for k in (exclude_title or []) if k]
    if not kws:
        return "", []
    parts = " AND ".join(["coalesce(title,'') NOT ILIKE %s"] * len(kws))
    return parts, [f"%{k}%" for k in kws]


def filter_search(conn, where: str, order_by: str = "code", coord_units=None,
                  exclude_title=None) -> list[dict]:
    """纯结构化过滤:SELECT 固定列 FROM courses WHERE {guard(where)} [+coord +title] ORDER BY {白名单子句}。

    order_by 走 _ORDER_BY 白名单(默认 code);非法键回退 code。
    coord_units 追加参数化 coordinating_unit IN (...);exclude_title 追加参数化 title NOT ILIKE。
    去重与排序无关(seen_codes 集)。"""
    where = guard_where(where)
    order_clause = _ORDER_BY.get(order_by, _ORDER_BY["code"])
    coord_sql, coord_params = _coord_clause(coord_units)
    title_sql, title_params = _title_exclude_clause(exclude_title)
    full_where = where + (f" AND {coord_sql}" if coord_sql else "") + (f" AND {title_sql}" if title_sql else "")
    sql = f"SELECT {SELECT_COLS} FROM courses WHERE {full_where} ORDER BY {order_clause}"
    out: list[dict] = []
    seen_codes: set = set()         # 同课跨学期多 offering 按课去重(seen_codes 集,与排序无关)
    for r in conn.execute(sql, coord_params + title_params).fetchall():
        d = _row_to_dict(r)
        if d["code"] in seen_codes:
            continue
        seen_codes.add(d["code"])
        out.append(d)
    return out


def filter_search_both_semesters(conn, where: str | None = None, coord_units=None,
                                 exclude_title=None) -> list[dict]:
    """「S1 和 S2 都满足」:同一课码在 S1、S2 各有一个满足 where 的 offering 才算命中。

    「都」是跨学期合取,扁平 WHERE 的 semester IN ('S1','S2') 只能表达并集(任一学期满足),
    数量会虚高;此路径用 GROUP BY code HAVING count(DISTINCT semester)=2 取真合取。
    where 为附加结构化条件(不含 semester,本函数固定补 IN('S1','S2'));可为空(只问两学期都开)。
    coord_units / exclude_title 同 filter_search 走参数化追加。
    semester 限定与子查询为代码侧受控构造,where 仍过 guard_where 安全网。"""
    cond = (guard_where(where) + " AND ") if (where and where.strip()) else ""
    coord_sql, coord_params = _coord_clause(coord_units)
    title_sql, title_params = _title_exclude_clause(exclude_title)
    base = (cond + "semester IN ('S1','S2')"
            + (f" AND {coord_sql}" if coord_sql else "")
            + (f" AND {title_sql}" if title_sql else ""))
    base_params = coord_params + title_params       # base 在外层与子查询各出现一次
    sql = (
        f"SELECT {SELECT_COLS} FROM courses WHERE {base} "
        f"AND code IN (SELECT code FROM courses WHERE {base} "
        f"GROUP BY code HAVING count(DISTINCT semester) = 2) ORDER BY code"
    )
    out: list[dict] = []
    seen_codes: set = set()
    for r in conn.execute(sql, base_params + base_params).fetchall():
        d = _row_to_dict(r)
        if d["code"] in seen_codes:
            continue
        seen_codes.add(d["code"])
        # 命中课按定义 S1、S2 两学期都满足,标 'S1+S2'(去重后单行只剩一个学期值,会误导)
        d["semester"] = "S1+S2"
        out.append(d)
    return out


def semantic_search(conn, query_en: str, k: int = 8, min_sim: float = SEMANTIC_MIN_SIM,
                    coord_units=None) -> list[dict]:
    """
    向量检索 + 关键词 RRF 融合。
    取候选(向量 k*3 + 全文 k*3),按 RRF 融合排序,只留向量 sim>=min_sim,取前 k。
    融合是为了让「课名就叫 Machine Learning」这种被全文命中的课能排上来。
    coord_units 非空时把候选限定在指定 coordinating_unit(学科→学院,排除跨学院噪声)。
    """
    if not query_en or not query_en.strip():
        raise ValueError("query_en 不能为空")
    return _fused_search(conn, where=None, query_en=query_en, k=k, min_sim=min_sim,
                         coord_units=coord_units)


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


def hybrid_search(conn, where: str | None, semantic_en: str, k: int = 8,
                  coord_units=None) -> list[dict]:
    """
    结构化 where(可空,空=全表)过滤后,RRF 融合「向量排序」与「全文检索排序」,取前 k。
    语义维度同样卡 SEMANTIC_MIN_SIM:结构化 where(如 has_exam=false)不收窄主题,
    若不卡下限,topical 召回的尾部会混入 off-topic 课(如 AI 查询混入 Marketing/心理课)。
    coord_units 非空时把候选限定在指定 coordinating_unit(学科→学院,排除跨学院噪声)。
    """
    if not semantic_en or not semantic_en.strip():
        raise ValueError("semantic_en 不能为空")
    return _fused_search(conn, where=where, query_en=semantic_en, k=k, min_sim=SEMANTIC_MIN_SIM,
                         coord_units=coord_units)


def _fused_search(conn, where, query_en, k, min_sim, coord_units=None) -> list[dict]:
    """
    RRF 融合核心:在同一个候选集合(可被 where + coord_units 过滤)上分别取向量排序和
    全文排序,用 score=Σ 1/(RRF_K+rank) 融合,返回带 sim 的 top-k。
    coordinating_unit 走参数化 IN(文本列不进 guard_where),与 where 合并成 filt。
    """
    where = guard_where(where) if where and where.strip() else None
    coord_sql, coord_params = _coord_clause(coord_units)
    conds = [c for c in (where, coord_sql) if c]
    filt = ("WHERE " + " AND ".join(conds)) if conds else ""

    vec = _embed(query_en)
    pool = k * 3  # 每路候选量,取大些保证融合后还够 k 个

    # 向量路:offering_id -> (rank, sim);参数顺序须与 SQL 内 %s 文本出现顺序一致
    vec_sql = (
        f"SELECT offering_id, {SELECT_COLS}, 1-(embedding<=>%s::vector) AS sim "
        f"FROM courses {filt} "
        f"ORDER BY embedding<=>%s::vector LIMIT %s"
    )
    vec_rows = conn.execute(vec_sql, (vec, *coord_params, vec, pool)).fetchall()

    # 全文路:offering_id -> rank(命中即入,没命中不入)
    kw_filt = filt + (" AND " if filt else "WHERE ") + \
        f"{TSV_EXPR} @@ websearch_to_tsquery('english', %s)"
    kw_sql = (
        f"SELECT offering_id FROM courses {kw_filt} "
        f"ORDER BY ts_rank({TSV_EXPR}, websearch_to_tsquery('english', %s)) DESC, code "
        f"LIMIT %s"
    )
    kw_rows = conn.execute(kw_sql, (*coord_params, query_en, query_en, pool)).fetchall()

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

    # 按融合分排序;min_sim 卡所有条目的向量相似度(含纯全文命中)——否则 off-topic 课只因
    # blob 里出现主题词被全文召回、绕过下限混入(如 AI 查询混入 Humanities/心理/教育课)。
    # 名副其实的课(标题就叫 Machine Learning)向量 sim 本就高,不受影响。取前 k。
    ranked = sorted(score.items(), key=lambda kv: kv[1], reverse=True)
    out: list[dict] = []
    seen_codes: set = set()         # 同一门课跨学期有多 offering,只保留融合分最高的一条,避免占满 top-k 槽
    for oid, _ in ranked:
        meta = info[oid]
        if meta["sim"] < min_sim:
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


# ---------- 知识库(FAQ / article)语义检索 ----------
KB_COLS = "id, url, source_type, page_title, breadcrumb, text"
KB_KEYS = ("id", "url", "source_type", "page_title", "breadcrumb", "text")


def kb_search(conn, query: str, k: int = 5, min_sim: float = 0.62,
              query_en: str | None = None) -> list[dict]:
    """知识库 chunk(support FAQ / study article)语义检索:bge-m3 向量近邻,返回 top-k。

    用于课程/专业结构化数据答不了的一般学生事务问题(how-to / 政策 / FAQ)。
    低于 min_sim 的一律滤掉——宁可不返回也不给弱相关结果(student-facing 红线 3:弱召回拒答)。
    min_sim=0.62 由 threshold_scan 在带负样本评测集上扫出(综合准确率 75%→83%,答全率不降);
    sim>=0.62 仍有少量编造问题(如"火星交换生" 0.70)挡不住,属阈值天花板,虚构实体拒答归
    answerability 门(P0),非本函数。同一官方页面可能切多个 chunk,这里不去重(answer 层按 url 去重列来源)。

    query_en(可选,planner 在 kb 模式产出的英文 KB query):语料是英文,中文 query 贴阈抖动。
    给了就对每个 chunk 取 max(sim_中, sim_英)(= 最近距离)召回,让真问题靠更准的英文匹配过
    min_sim,而非靠下调 0.62 硬阈值(跨语言根因修复)。为空时与原单语行为逐字节等价。

    可选重排(KB_RERANK 开,默认关):top-N 候选过 min_sim 后用 cross-encoder 重排再取 top-k;
    min_sim 仍卡 bi-encoder sim、reranker 分绝不参与拒答(拒答归 P0)。关闭时行为与无重排完全一致。
    """
    if not query or not query.strip():
        raise ValueError("query 不能为空")
    qen = (query_en or "").strip()
    if qen:
        # 跨语言:候选池按两路最近距离取 top-N,每个 chunk 的 sim 取两路最大(= least 距离)
        vec_zh, vec_en = _embed(query), _embed(qen)
        sql = (f"SELECT {KB_COLS}, "
               f"1-least(embedding<=>%s::vector, embedding<=>%s::vector) AS sim FROM kb_chunks "
               f"ORDER BY least(embedding<=>%s::vector, embedding<=>%s::vector) LIMIT %s")
        rows = conn.execute(sql, (vec_zh, vec_en, vec_zh, vec_en, k * 4)).fetchall()
        rerank_q = qen
    else:
        vec = _embed(query)
        sql = (f"SELECT {KB_COLS}, 1-(embedding<=>%s::vector) AS sim FROM kb_chunks "
               f"ORDER BY embedding<=>%s::vector LIMIT %s")
        rows = conn.execute(sql, (vec, vec, k * 4)).fetchall()  # top-N 候选
        rerank_q = query
    out: list[dict] = []
    for r in rows:
        sim = float(r[-1])
        if sim < min_sim:          # 拒答门槛只卡 bi-encoder sim(reranker 分不参与)
            continue
        d = dict(zip(KB_KEYS, r[:len(KB_KEYS)]))
        d["sim"] = sim
        out.append(d)
    out = reranker.rerank(rerank_q, out)   # 默认关=原样返回;开则只改顺序/取舍,不改拒答
    return out[:k]


# ---------- 单课详情(课程介绍 / 先修 / 考核) ----------
DETAIL_COLS = ("code, title, units, level, description, prerequisite_raw, "
               "incompatible, assessments, learning_outcomes, topics, "
               "coordinator, coordinating_unit, has_exam, has_hurdle")
DETAIL_KEYS = ("code", "title", "units", "level", "description", "prerequisite_raw",
               "incompatible", "assessments", "learning_outcomes", "topics",
               "coordinator", "coordinating_unit", "has_exam", "has_hurdle")


def course_detail(conn, code: str) -> dict | None:
    """单门课的完整详情(介绍/先修/考核/开课),聚合同课多 offering。

    课程内容字段(description/先修等)取最新一行;开课学期/校区汇总所有 offering。
    返回 None 表示课程码不在库(上层据此优雅提示,不静默成功)。
    """
    code = (code or "").strip().upper()
    if not code:
        raise ValueError("code 不能为空")
    row = conn.execute(
        f"SELECT {DETAIL_COLS} FROM courses WHERE code=%s "
        f"ORDER BY year DESC NULLS LAST, semester LIMIT 1", (code,)).fetchone()
    if not row:
        return None
    d = dict(zip(DETAIL_KEYS, row))
    offerings = conn.execute(
        "SELECT DISTINCT semester, location FROM courses "
        "WHERE code=%s AND semester IS NOT NULL", (code,)).fetchall()
    d["semesters"] = sorted({o[0] for o in offerings if o[0]})
    d["locations"] = sorted({o[1] for o in offerings if o[1]})
    d["profile_url"] = COURSE_PROFILE_URL.format(code)
    return d


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
        "requirement_type NOT IN ('thesis')",   # 非白名单列(原 bug)仍须拒
    ]
    must_pass = [
        "has_exam=false",
        "level='Postgraduate Coursework' AND units=2",
        "location='St Lucia'",
        "has_exam=false AND course_type NOT IN ('placement','thesis','research')",
        "course_type='thesis'",
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
