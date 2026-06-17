"""
retrieval.py — 统一检索层。
四种检索 + 安全网,全部接收 psycopg 连接 conn,返回 list[dict]。

分工(沿用项目「确定性决策用代码,语言任务交模型」):
  - 本模块全是确定性活:结构化槽位拼装(参数化)、向量检索、全文检索、RRF 融合。
  - 不调 LLM;filters 槽位由 planner 校验后给来,query_en/semantic_en 是给好的英文主题词。

公开函数:
  - build_where(filters) -> (sql, params)  # 校验后的槽位 -> 参数化 WHERE 片段(注入安全是结构性的)
  - ensure_fts_index(conn) -> None  # 启动时一次性建 FTS 索引,读路径不再建
  - filter_search(conn, filters, order_by='code', coord_units=None, exclude_title=None) -> list[dict]
  - semantic_search(conn, query_en, k=8, min_sim=SEMANTIC_MIN_SIM=0.50) -> list[dict]
  - keyword_search(conn, query_en, k=20) -> list[dict]
  - hybrid_search(conn, filters, semantic_en, k=8) -> list[dict]
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
    coordinating_unit 是文本列(不是 build_where 的结构化筛选列),只走参数化 IN,值来自代码侧受控映射。"""
    units = [u for u in (coord_units or []) if u]
    if not units:
        return "", []
    return f"coordinating_unit IN ({','.join(['%s'] * len(units))})", units


def _title_exclude_clause(exclude_title) -> tuple[str, list]:
    """课程性质标题排除 -> (SQL 片段, 参数列表)。title 是文本列(不是 build_where 的结构化筛选列),
    只走参数化 NOT ILIKE,值来自 planner 受控词表(capstone/review/project…),注入安全。
    coalesce 避免 NULL 标题被 NOT ILIKE 静默排掉(NULL NOT ILIKE 结果为 NULL→不命中)。"""
    kws = [k for k in (exclude_title or []) if k]
    if not kws:
        return "", []
    parts = " AND ".join(["coalesce(title,'') NOT ILIKE %s"] * len(kws))
    return parts, [f"%{k}%" for k in kws]


# filters 槽位 -> WHERE 片段的确定性拼装表(单一真相源:加一种结构化筛选 = 在此加一行)。
# 列表顺序即 %s 在片段里的出现顺序;列名来自代码侧闭集,值全进 params——没有自由字符串进 SQL,
# 注入安全是结构性的(取代旧 guard_where 的串净化)。
_WHERE_BUILDERS = (
    ("has_exam",        "has_exam = %s"),
    ("has_hurdle",      "has_hurdle = %s"),
    ("midterm_status",  "midterm_status = %s"),
    ("group_status",    "group_status = %s"),
    ("level",           "level = %s"),
    ("units",           "units = %s"),
    ("location",        "location = %s"),
    ("attendance_mode", "attendance_mode = %s"),
    ("semester",        "semester = %s"),
)


def build_where(filters: dict | None) -> tuple[str, list]:
    """校验后的 filters 槽位 -> 参数化 (where_sql, params)。

    只拼代码侧闭集里的列名,值全进 params(psycopg %s);没有自由字符串进 SQL,
    注入安全是结构性的(取代旧 guard_where 的串净化)。空 filters 返回 ("", [])——
    纯函数不抛;是否容忍空 WHERE 由调用方决定(filter_search 抛、both_semesters 容忍)。
    course_type_only/exclude 用 = ANY / <> ALL 数组参数,等价旧的 IN / NOT IN
    (course_type 是 NOT NULL 列,<> ALL 无三值逻辑漏排)。"""
    f = filters or {}
    clauses: list[str] = []
    params: list = []
    for key, tmpl in _WHERE_BUILDERS:
        v = f.get(key)
        if v is None:
            continue
        clauses.append(tmpl)
        params.append(v)
    only = f.get("course_type_only")
    if only:
        clauses.append("course_type = ANY(%s)")
        params.append(list(only))
    exclude = f.get("course_type_exclude")
    if exclude:
        clauses.append("course_type <> ALL(%s)")
        params.append(list(exclude))
    # code 首位数字 = 课程级别号(1xxx 入门本科…7/8/9 研究生)。code 文本列绝不做学科 LIKE,
    # 但确定性抽出的首位数字是结构化筛选:取 code 里第一个数字字符(POSIX 子串,等价 _first_digit),
    # 值(数字列表)进 params,注入安全。
    levels = f.get("code_level")
    if levels:
        clauses.append("substring(code from '[0-9]') = ANY(%s)")
        params.append(list(levels))
    return " AND ".join(clauses), params


def describe_where(filters: dict | None) -> str:
    """build_where 的可读对偶:filters 槽位 -> 人读 WHERE 描述串(仅供 meta 展示 / 前端
    program_facts.filter 字段 / route_eval 断言,绝不进 SQL)。course_type_exclude/only 渲染成
    NOT IN / IN;bool 渲染 true/false;字符串加引号。"""
    if not filters:
        return ""
    parts: list[str] = []
    for k, v in filters.items():
        if k == "course_type_exclude":
            parts.append("course_type NOT IN (" + ",".join(f"'{t}'" for t in v) + ")")
        elif k == "course_type_only":
            parts.append("course_type IN (" + ",".join(f"'{t}'" for t in v) + ")")
        elif k == "code_level":
            parts.append("code首位∈{" + ",".join(map(str, v)) + "}")
        elif isinstance(v, bool):
            parts.append(f"{k}={'true' if v else 'false'}")
        elif isinstance(v, str):
            parts.append(f"{k}='{v}'")
        else:
            parts.append(f"{k}={v}")
    return " AND ".join(parts)


def filter_search(conn, filters: dict, order_by: str = "code", coord_units=None,
                  exclude_title=None) -> list[dict]:
    """纯结构化过滤:SELECT 固定列 FROM courses WHERE {build_where(filters)} [+coord +title] ORDER BY {白名单子句}。

    filters 经 build_where 拼成参数化片段。空 filters -> raise ValueError:空 WHERE 会退化成全表扫,
    把全库当「符合条件」返回(踩 student-facing 红线 5);上游 qa 接 ValueError 优雅降级 empty。
    order_by 走 _ORDER_BY 白名单(默认 code);非法键回退 code。
    coord_units 追加参数化 coordinating_unit IN (...);exclude_title 追加参数化 title NOT ILIKE。
    去重与排序无关(seen_codes 集)。"""
    where, where_params = build_where(filters)
    if not where:
        raise ValueError("filters 不能为空(空 WHERE 会全表扫,踩红线)")
    order_clause = _ORDER_BY.get(order_by, _ORDER_BY["code"])
    coord_sql, coord_params = _coord_clause(coord_units)
    title_sql, title_params = _title_exclude_clause(exclude_title)
    full_where = where + (f" AND {coord_sql}" if coord_sql else "") + (f" AND {title_sql}" if title_sql else "")
    sql = f"SELECT {SELECT_COLS} FROM courses WHERE {full_where} ORDER BY {order_clause}"
    out: list[dict] = []
    seen_codes: set = set()         # 同课跨学期多 offering 按课去重(seen_codes 集,与排序无关)
    for r in conn.execute(sql, where_params + coord_params + title_params).fetchall():
        d = _row_to_dict(r)
        if d["code"] in seen_codes:
            continue
        seen_codes.add(d["code"])
        out.append(d)
    return out


def filter_search_both_semesters(conn, filters: dict | None = None, coord_units=None,
                                 exclude_title=None) -> list[dict]:
    """「S1 和 S2 都满足」:同一课码在 S1、S2 各有一个满足 filters 的 offering 才算命中。

    「都」是跨学期合取,扁平 WHERE 的 semester IN ('S1','S2') 只能表达并集(任一学期满足),
    数量会虚高;此路径用 GROUP BY code HAVING count(DISTINCT semester)=2 取真合取。
    filters 为附加结构化条件(不含 semester,本函数固定补 IN('S1','S2'));可为空(只问两学期都开)——
    故 build_where 返回空片段时不抛(永不真全表扫,semester IN 已限定范围),与 filter_search 不同。
    coord_units / exclude_title 同 filter_search 走参数化追加。"""
    where, where_params = build_where(filters)
    cond = (where + " AND ") if where else ""
    coord_sql, coord_params = _coord_clause(coord_units)
    title_sql, title_params = _title_exclude_clause(exclude_title)
    base = (cond + "semester IN ('S1','S2')"
            + (f" AND {coord_sql}" if coord_sql else "")
            + (f" AND {title_sql}" if title_sql else ""))
    # base 在外层 WHERE 与子查询各出现一次;其 %s 顺序为 where -> coord -> title(semester IN 是字面量)。
    base_params = where_params + coord_params + title_params
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
    return _fused_search(conn, filters=None, query_en=query_en, k=k, min_sim=min_sim,
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


def hybrid_search(conn, filters: dict | None, semantic_en: str, k: int = 8,
                  coord_units=None) -> list[dict]:
    """
    结构化 filters(可空,空=不按结构化收窄,只主题召回)过滤后,RRF 融合「向量排序」与「全文检索排序」,取前 k。
    语义维度同样卡 SEMANTIC_MIN_SIM:结构化 filters(如 has_exam=false)不收窄主题,
    若不卡下限,topical 召回的尾部会混入 off-topic 课(如 AI 查询混入 Marketing/心理课)。
    coord_units 非空时把候选限定在指定 coordinating_unit(学科→学院,排除跨学院噪声)。
    """
    if not semantic_en or not semantic_en.strip():
        raise ValueError("semantic_en 不能为空")
    return _fused_search(conn, filters=filters, query_en=semantic_en, k=k, min_sim=SEMANTIC_MIN_SIM,
                         coord_units=coord_units)


def _fused_search(conn, filters, query_en, k, min_sim, coord_units=None) -> list[dict]:
    """
    RRF 融合核心:在同一个候选集合(可被 filters + coord_units 过滤)上分别取向量排序和
    全文排序,用 score=Σ 1/(RRF_K+rank) 融合,返回带 sim 的 top-k。
    filters 经 build_where 拼成参数化片段(可空=不按结构化收窄),coordinating_unit 走参数化 IN,
    合并成 filt。穿参铁律:where_params 在 filt 里位于 coord_params 之前,且 vec 的 %s 在 filt 之前。
    """
    where, where_params = build_where(filters)
    coord_sql, coord_params = _coord_clause(coord_units)
    conds = [c for c in (where, coord_sql) if c]
    filt = ("WHERE " + " AND ".join(conds)) if conds else ""

    vec = _embed(query_en)
    pool = k * 3  # 每路候选量,取大些保证融合后还够 k 个

    # 向量路:offering_id -> (rank, sim);参数顺序须与 SQL 内 %s 文本出现顺序严格一致:
    # SELECT 的 vec -> filt 里 where_params 再 coord_params -> ORDER BY 的 vec -> LIMIT。
    vec_sql = (
        f"SELECT offering_id, {SELECT_COLS}, 1-(embedding<=>%s::vector) AS sim "
        f"FROM courses {filt} "
        f"ORDER BY embedding<=>%s::vector LIMIT %s"
    )
    vec_rows = conn.execute(vec_sql, (vec, *where_params, *coord_params, vec, pool)).fetchall()

    # 全文路:offering_id -> rank(命中即入,没命中不入)
    kw_filt = filt + (" AND " if filt else "WHERE ") + \
        f"{TSV_EXPR} @@ websearch_to_tsquery('english', %s)"
    kw_sql = (
        f"SELECT offering_id FROM courses {kw_filt} "
        f"ORDER BY ts_rank({TSV_EXPR}, websearch_to_tsquery('english', %s)) DESC, code "
        f"LIMIT %s"
    )
    kw_rows = conn.execute(kw_sql, (*where_params, *coord_params, query_en, query_en, pool)).fetchall()

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

    # build_where 拼装验证:槽位 -> 参数化 (sql, params),值全进 params(注入安全是结构性的)
    print("== build_where 参数化验证 ==")
    for f in [
        {"has_exam": False},
        {"level": "Postgraduate Coursework", "units": 2},
        {"location": "St Lucia"},
        {"has_exam": False, "course_type_exclude": ["placement", "thesis", "research"]},
        {"course_type_only": ["thesis"]},
        {},  # 空 -> ("", []),由调用方决定是否容忍
    ]:
        print(f"  {f}  ->  {build_where(f)}")

    # 启动时一次性建索引(写连接)
    with psycopg.connect(DSN) as conn:
        ensure_fts_index(conn)

    # 读路径在只读连接下也要能跑(只 SELECT,不建索引/不 commit)
    with psycopg.connect(DSN) as conn:
        conn.read_only = True

        f_rows = filter_search(conn, {"has_exam": False})
        print(f"\n== filter has_exam=false 计数: {len(f_rows)} ==")
        show("filter has_exam=false 前3条", f_rows)

        s_rows = semantic_search(conn, "machine learning")
        show("semantic 'machine learning' top3", s_rows, sim=True)

        k_rows = keyword_search(conn, "machine learning")
        show("keyword 'machine learning' top3", k_rows)

        h_rows = hybrid_search(conn, {"level": "Postgraduate Coursework"}, "data science")
        show("hybrid level=Postgraduate Coursework + 'data science' top3", h_rows, sim=True)

    print("\n自测完成。")
