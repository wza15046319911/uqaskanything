"""
retrieval.py — unified retrieval layer.
Four retrieval modes + safety net, all take a psycopg connection conn and return list[dict].

Split of work (follows the project rule "deterministic decisions in code, language tasks to the model"):
  - This module is all deterministic work: structured slot assembly (parameterized), vector search, full-text search, RRF fusion.
  - No LLM calls; filters slots come pre-validated from planner, query_en/semantic_en are ready English topic terms.

Public functions:
  - build_where(filters) -> (sql, params)  # validated slots -> parameterized WHERE fragment (injection safety is structural)
  - ensure_fts_index(conn) -> None  # build FTS index once at startup, read path no longer builds it
  - filter_search(conn, filters, order_by='code', coord_units=None, exclude_title=None) -> list[dict]
  - semantic_search(conn, query_en, k=8, min_sim=SEMANTIC_MIN_SIM=0.50) -> list[dict]
  - keyword_search(conn, query_en, k=20) -> list[dict]
  - hybrid_search(conn, filters, semantic_en, k=8) -> list[dict]
  - course_detail(conn, code) -> dict | None  # full detail of a single course (intro/prereq/assessment/offering)

Returned dict fields: code, title, semester, level, units, has_exam; semantic/hybrid also carry sim.
"""
from __future__ import annotations
import os
import re

import requests
import psycopg

from app.services import reranker

EMBED_BASE = os.environ.get("EMBED_BASE", "https://api.deepinfra.com/v1/openai")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
# Local default embedding: Ollama (the same bge-m3 that built the DB vectors). DeepInfra is used only when EMBED_API_KEY is present.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "bge-m3")

# Fixed output columns, fixed order; this is the key set used when making dicts
SELECT_COLS = "code, title, semester, level, units, has_exam, has_hurdle, midterm_status, group_status"
RESULT_KEYS = ("code", "title", "semester", "level", "units", "has_exam", "has_hurdle", "midterm_status", "group_status")

# Official course page (every answer carries a clickable official source, see student-facing red line 2). course.html?course_code=
# is the course overview page (lists each semester's offering), located by course code, stable to link to.
COURSE_PROFILE_URL = "https://programs-courses.uq.edu.au/course.html?course_code={}"

# Full-text search expression (must match the index expression word for word, otherwise the index is not used)
TSV_EXPR = ("to_tsvector('english', coalesce(title,'') || ' ' || "
            "coalesce(code,'') || ' ' || coalesce(search_blob,''))")

RRF_K = 60  # RRF constant, industry default 60
# Vector recall floor for semantic/hybrid search: pure-vector items below this are dropped as noise (full-text hits exempt).
# 0.50 set from measured sim distribution: relevant courses of real topics bottom out around 0.515 (finance 0.528/AI 0.515/stats 0.531),
# noise is mostly <0.50 (Marketing 0.490/Law 0.499 inside AI, whole groups of purely made-up topics), 0.50 is the line that does not hurt real hits
# yet cuts clear noise. The remaining 0.50~0.55 off-topic is a built-in bi-encoder limit, handled by the reranker (P1).
SEMANTIC_MIN_SIM = 0.50

def _embed(text: str) -> str:
    """Get the bge-m3 vector and turn it into a pgvector literal. Same model, same 1024 dims, compatible with existing DB vectors; raise on failure, do not swallow.
    Local default: Ollama (bge-m3, the same model that built the DB vectors). When EMBED_API_KEY is set (cloud/production, injected by Terraform) it switches to the DeepInfra OpenAI-compatible API.
    The key is read live each call, so .env loaded after import (and runtime env changes) take effect."""
    text = text[:8000]
    key = os.environ.get("EMBED_API_KEY", "")
    if key:
        r = requests.post(
            f"{EMBED_BASE}/embeddings",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": EMBED_MODEL, "input": text, "encoding_format": "float"},
            timeout=60,
        )
        r.raise_for_status()
        v = r.json()["data"][0]["embedding"]
    else:
        r = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": OLLAMA_EMBED_MODEL, "prompt": text},
            timeout=60,
        )
        r.raise_for_status()
        v = r.json()["embedding"]
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def _row_to_dict(row, with_sim: bool = False) -> dict:
    """Turn a fixed-column-order tuple into a dict; with_sim means the last column is sim. Each row carries the official course page profile_url (red line 2)."""
    d = dict(zip(RESULT_KEYS, row[: len(RESULT_KEYS)]))
    d["profile_url"] = COURSE_PROFILE_URL.format(d["code"]) if d.get("code") else None
    if with_sim:
        d["sim"] = float(row[len(RESULT_KEYS)])
    return d


def ensure_fts_index(conn) -> None:
    """
    Build the GIN expression index (idempotent), called once by the integration layer at startup (write connection).
    Read paths (keyword/semantic/hybrid) no longer build the index, only SELECT, to stay compatible with read-only connections.
    """
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_courses_fts ON courses USING gin({TSV_EXPR})"
    )
    conn.commit()


# order_by allowlist: key -> fixed ORDER BY clause (deterministic lookup, never concatenate user strings, injection-safe).
_ORDER_BY = {
    "code": "code",
    # Objective assessment load: ascending by number of assessment items (non-arrays sort last), ties by code. For "low load / chill" queries.
    "assessments_asc": ("(CASE WHEN jsonb_typeof(assessments)='array' "
                        "THEN jsonb_array_length(assessments) ELSE 999 END) ASC, code"),
}


def _coord_clause(coord_units) -> tuple[str, list]:
    """Faculty/discipline -> coordinating_unit restriction: returns (SQL fragment, param list).
    coordinating_unit is a text column (not a build_where structured filter column), only goes through parameterized IN, values come from a code-side controlled mapping."""
    units = [u for u in (coord_units or []) if u]
    if not units:
        return "", []
    return f"coordinating_unit IN ({','.join(['%s'] * len(units))})", units


def _title_exclude_clause(exclude_title) -> tuple[str, list]:
    """Course-nature title exclusion -> (SQL fragment, param list). title is a text column (not a build_where structured filter column),
    only goes through parameterized NOT ILIKE, values come from planner's controlled word list (capstone/review/project...), injection-safe.
    coalesce stops NULL titles from being silently dropped by NOT ILIKE (NULL NOT ILIKE is NULL -> no match)."""
    kws = [k for k in (exclude_title or []) if k]
    if not kws:
        return "", []
    parts = " AND ".join(["coalesce(title,'') NOT ILIKE %s"] * len(kws))
    return parts, [f"%{k}%" for k in kws]


# Deterministic assembly table from filters slot -> WHERE fragment (single source of truth: adding one structured filter = adding one row here).
# List order is the order %s appears in the fragment; column names come from a code-side closed set, all values go into params -- no free string enters SQL,
# injection safety is structural (replaces the old guard_where string sanitizing).
_WHERE_BUILDERS = (
    ("has_exam",        "has_exam = %s"),
    ("has_hurdle",      "has_hurdle = %s"),
    ("midterm_status",  "midterm_status = %s"),
    ("group_status",    "group_status = %s"),
    ("level",           "level = %s"),
    ("units",           "units = %s"),
    ("location",        "location = %s"),
    ("attendance_mode", "attendance_mode = %s"),
)

# semester is NOT a plain column match: the `semester` text column is per-offering and unreliable for "currently offered"
# (S1 rows are 2026, S2 rows are mostly last year's 2025 proxy, and the authoritative S2 list lives in S2_CODES, not the column).
# So S1/S2 membership routes to the per-code derived flags offered_s1 / offered_s2 (populated by pipelines.backfill_offerings).
_SEMESTER_FLAG = {"S1": "offered_s1 = TRUE", "S2": "offered_s2 = TRUE"}


def build_where(filters: dict | None) -> tuple[str, list]:
    """Validated filters slots -> parameterized (where_sql, params).

    Only concatenates column names from a code-side closed set, all values go into params (psycopg %s); no free string enters SQL,
    injection safety is structural (replaces the old guard_where string sanitizing). Empty filters returns ("", []) --
    the pure function does not raise; whether to tolerate an empty WHERE is the caller's choice (filter_search raises, both_semesters tolerates).
    course_type_only/exclude use = ANY / <> ALL array params, equal to the old IN / NOT IN
    (course_type is a NOT NULL column, <> ALL has no three-valued-logic miss)."""
    f = filters or {}
    clauses: list[str] = []
    params: list = []
    for key, tmpl in _WHERE_BUILDERS:
        v = f.get(key)
        if v is None:
            continue
        clauses.append(tmpl)
        params.append(v)
    sem_flag = _SEMESTER_FLAG.get(f.get("semester"))
    if sem_flag:
        clauses.append(sem_flag)
    only = f.get("course_type_only")
    if only:
        clauses.append("course_type = ANY(%s)")
        params.append(list(only))
    exclude = f.get("course_type_exclude")
    if exclude:
        clauses.append("course_type <> ALL(%s)")
        params.append(list(exclude))
    # First digit of code = course level number (1xxx intro undergrad ... 7/8/9 postgrad). The code text column never does a subject LIKE,
    # but the deterministically extracted first digit is a structured filter: take the first digit character in code (POSIX substring, equal to _first_digit),
    # values (the digit list) go into params, injection-safe.
    levels = f.get("code_level")
    if levels:
        clauses.append("substring(code from '[0-9]') = ANY(%s)")
        params.append(list(levels))
    return " AND ".join(clauses), params


def describe_where(filters: dict | None) -> str:
    """Readable dual of build_where: filters slots -> human-readable WHERE description string (only for meta display / frontend
    program_facts.filter field / route_eval asserts, never enters SQL). course_type_exclude/only render as
    NOT IN / IN; bool renders true/false; strings get quotes."""
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
    """Pure structured filter: SELECT fixed cols FROM courses WHERE {build_where(filters)} [+coord +title] ORDER BY {allowlist clause}.

    filters become a parameterized fragment via build_where. Empty filters -> raise ValueError: an empty WHERE degrades to a full-table scan,
    returning the whole DB as "matching" (breaks student-facing red line 5); upstream qa catches ValueError and gracefully degrades to empty.
    order_by uses the _ORDER_BY allowlist (default code); illegal keys fall back to code.
    coord_units appends a parameterized coordinating_unit IN (...); exclude_title appends a parameterized title NOT ILIKE.
    Dedup is independent of sorting (seen_codes set)."""
    where, where_params = build_where(filters)
    if not where:
        raise ValueError("filters 不能为空(空 WHERE 会全表扫,踩红线)")
    order_clause = _ORDER_BY.get(order_by, _ORDER_BY["code"])
    coord_sql, coord_params = _coord_clause(coord_units)
    title_sql, title_params = _title_exclude_clause(exclude_title)
    full_where = where + (f" AND {coord_sql}" if coord_sql else "") + (f" AND {title_sql}" if title_sql else "")
    sql = f"SELECT {SELECT_COLS} FROM courses WHERE {full_where} ORDER BY {order_clause}"
    # Semester filtering matches by per-code flag (offered_s1/offered_s2), so a code with several offerings keeps several rows.
    # Keep the row whose `semester` value matches the asked semester (so the displayed semester/assessment is the right offering,
    # not e.g. a both-semester course's stale 2025 S2 row surfacing for an S1 query); order is otherwise the SQL order.
    pref = (filters or {}).get("semester")
    out: list[dict] = []
    pos: dict = {}                  # code -> index in out, to replace with the preferred-semester row
    for r in conn.execute(sql, where_params + coord_params + title_params).fetchall():
        d = _row_to_dict(r)
        code = d["code"]
        if code not in pos:
            pos[code] = len(out)
            out.append(d)
        elif pref and out[pos[code]]["semester"] != pref and d["semester"] == pref:
            out[pos[code]] = d
    return out


def filter_search_both_semesters(conn, filters: dict | None = None, coord_units=None,
                                 exclude_title=None) -> list[dict]:
    """"Satisfies both S1 and S2": a hit means the same course code is offered in both S1 and S2 and matches the extra filters.

    "Both" is a cross-semester conjunction. Membership in each semester comes from the per-code derived flags offered_s1 / offered_s2
    (same authoritative source as single-semester filtering), so both-semester membership is offered_s1 AND offered_s2 -- consistent with
    build_where's semester routing and not subject to the `semester` text column's stale-S2 / incomplete-S2 problems.
    filters are extra structured conditions (no semester; planner pops it for this path); may be empty (only asking which open in both semesters) --
    so build_where returning an empty fragment does not raise (never a real full-table scan, the two flags already bound the range), unlike filter_search.
    coord_units / exclude_title append in a parameterized way, same as filter_search."""
    where, where_params = build_where(filters)
    cond = (where + " AND ") if where else ""
    coord_sql, coord_params = _coord_clause(coord_units)
    title_sql, title_params = _title_exclude_clause(exclude_title)
    base = (cond + "offered_s1 = TRUE AND offered_s2 = TRUE"
            + (f" AND {coord_sql}" if coord_sql else "")
            + (f" AND {title_sql}" if title_sql else ""))
    base_params = where_params + coord_params + title_params
    sql = f"SELECT {SELECT_COLS} FROM courses WHERE {base} ORDER BY code"
    out: list[dict] = []
    seen_codes: set = set()
    for r in conn.execute(sql, base_params).fetchall():
        d = _row_to_dict(r)
        if d["code"] in seen_codes:
            continue
        seen_codes.add(d["code"])
        # A hit by definition satisfies both S1 and S2, mark 'S1+S2' (after dedup a single row keeps only one semester value, which would mislead)
        d["semester"] = "S1+S2"
        out.append(d)
    return out


def semantic_search(conn, query_en: str, k: int = 8, min_sim: float = SEMANTIC_MIN_SIM,
                    coord_units=None) -> list[dict]:
    """
    Vector search + keyword RRF fusion.
    Take candidates (vector k*3 + full-text k*3), rank by RRF fusion, keep only vector sim>=min_sim, take the top k.
    Fusion is so that a course full-text-hit like "the course is literally named Machine Learning" can rank up.
    When coord_units is non-empty, restrict candidates to the given coordinating_unit (discipline -> faculty, drops cross-faculty noise).
    """
    if not query_en or not query_en.strip():
        raise ValueError("query_en 不能为空")
    return _fused_search(conn, filters=None, query_en=query_en, k=k, min_sim=min_sim,
                         coord_units=coord_units)


def keyword_search(conn, query_en: str, k: int = 20) -> list[dict]:
    """Postgres full-text search: websearch_to_tsquery match, ts_rank order, take top k."""
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
    After filtering by structured filters (nullable, empty = no structured narrowing, topic recall only), RRF-fuse the "vector order" and "full-text order", take top k.
    The semantic dimension still enforces SEMANTIC_MIN_SIM: structured filters (e.g. has_exam=false) do not narrow the topic,
    and without a floor, the tail of topical recall mixes in off-topic courses (e.g. an AI query pulls in Marketing/psychology courses).
    When coord_units is non-empty, restrict candidates to the given coordinating_unit (discipline -> faculty, drops cross-faculty noise).
    """
    if not semantic_en or not semantic_en.strip():
        raise ValueError("semantic_en 不能为空")
    return _fused_search(conn, filters=filters, query_en=semantic_en, k=k, min_sim=SEMANTIC_MIN_SIM,
                         coord_units=coord_units)


def _fused_search(conn, filters, query_en, k, min_sim, coord_units=None) -> list[dict]:
    """
    RRF fusion core: on the same candidate set (which filters + coord_units can filter), take the vector order and
    full-text order separately, fuse with score=Σ 1/(RRF_K+rank), return the top-k with sim.
    filters become a parameterized fragment via build_where (nullable = no structured narrowing), coordinating_unit goes through parameterized IN,
    merged into filt. Param-passing rule: where_params come before coord_params inside filt, and vec's %s comes before filt.
    """
    where, where_params = build_where(filters)
    coord_sql, coord_params = _coord_clause(coord_units)
    conds = [c for c in (where, coord_sql) if c]
    filt = ("WHERE " + " AND ".join(conds)) if conds else ""

    vec = _embed(query_en)
    pool = k * 3  # per-path candidate count, taken larger to ensure at least k remain after fusion

    # Vector path: offering_id -> (rank, sim); param order must strictly match the order %s appears in the SQL text:
    # SELECT's vec -> where_params then coord_params inside filt -> ORDER BY's vec -> LIMIT.
    vec_sql = (
        f"SELECT offering_id, {SELECT_COLS}, 1-(embedding<=>%s::vector) AS sim "
        f"FROM courses {filt} "
        f"ORDER BY embedding<=>%s::vector LIMIT %s"
    )
    vec_rows = conn.execute(vec_sql, (vec, *where_params, *coord_params, vec, pool)).fetchall()

    # Full-text path: offering_id -> rank (enters on hit, stays out if no hit)
    kw_filt = filt + (" AND " if filt else "WHERE ") + \
        f"{TSV_EXPR} @@ websearch_to_tsquery('english', %s)"
    kw_sql = (
        f"SELECT offering_id FROM courses {kw_filt} "
        f"ORDER BY ts_rank({TSV_EXPR}, websearch_to_tsquery('english', %s)) DESC, code "
        f"LIMIT %s"
    )
    kw_rows = conn.execute(kw_sql, (*where_params, *coord_params, query_en, query_en, pool)).fetchall()

    # Cache row data + sim (use the vector path's, the row already carries sim); vec_oids marks items recalled by the vector path
    info: dict = {}
    vec_oids: set = set()
    for row in vec_rows:
        oid = row[0]
        vec_oids.add(oid)
        info[oid] = {"row": row[1:1 + len(RESULT_KEYS)], "sim": float(row[-1])}

    # RRF accumulation (rank starts at 1, matching the original RRF definition)
    score: dict = {}
    for rank, row in enumerate(vec_rows, start=1):
        oid = row[0]
        score[oid] = score.get(oid, 0.0) + 1.0 / (RRF_K + rank)
    for rank, row in enumerate(kw_rows, start=1):
        oid = row[0]
        score[oid] = score.get(oid, 0.0) + 1.0 / (RRF_K + rank)
        if oid not in info:
            # Full-text hit but not in the vector candidates: fetch that row + its real sim
            r = conn.execute(
                f"SELECT {SELECT_COLS}, 1-(embedding<=>%s::vector) AS sim "
                f"FROM courses WHERE offering_id=%s",
                (vec, oid),
            ).fetchone()
            info[oid] = {"row": r[:len(RESULT_KEYS)], "sim": float(r[-1])}

    # Sort by fusion score; min_sim enforces the vector similarity of all items (including pure full-text hits) -- otherwise off-topic courses
    # get full-text recalled just because a topic word appears in the blob and slip past the floor (e.g. an AI query pulls in Humanities/psychology/education courses).
    # A course that truly fits (title literally Machine Learning) has high vector sim anyway, so it is not affected. Take top k.
    ranked = sorted(score.items(), key=lambda kv: kv[1], reverse=True)
    out: list[dict] = []
    seen_codes: set = set()         # the same course has multiple cross-semester offerings, keep only the one with the highest fusion score, to avoid filling up top-k slots
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


# ---------- Knowledge base (FAQ / article) semantic search ----------
KB_COLS = "id, url, source_type, page_title, breadcrumb, text"
KB_KEYS = ("id", "url", "source_type", "page_title", "breadcrumb", "text")


def kb_search(conn, query: str, k: int = 5, min_sim: float = 0.62,
              query_en: str | None = None) -> list[dict]:
    """Knowledge base chunk (support FAQ / study article) semantic search: bge-m3 vector nearest neighbor, returns top-k.

    Used for general student-affairs questions that structured course/program data cannot answer (how-to / policy / FAQ).
    Anything below min_sim is filtered out -- rather return nothing than give weakly related results (student-facing red line 3: weak recall refuses).
    min_sim=0.62 was scanned out by threshold_scan on an eval set with negative samples (overall accuracy 75%->83%, recall not lowered);
    sim>=0.62 still cannot block a few made-up questions (e.g. "Mars exchange student" 0.70), which is the threshold ceiling, and fictional-entity refusal belongs to
    the answerability gate (P0), not this function. The same official page may be split into multiple chunks, no dedup here (the answer layer dedups sources by url).

    query_en (optional, the English KB query planner produces in kb mode): the corpus is English, a Chinese query jitters near the threshold.
    If given, recall takes max(sim_zh, sim_en) (= nearest distance) per chunk, letting a real question pass min_sim by a more accurate English match,
    instead of lowering the 0.62 hard threshold (cross-language root-cause fix). When empty, behavior is byte-for-byte equal to the original single-language path.

    Optional rerank (KB_RERANK on, off by default): after top-N candidates pass min_sim, rerank with a cross-encoder then take top-k;
    min_sim still enforces the bi-encoder sim, and the reranker score never takes part in refusal (refusal belongs to P0). When off, behavior is identical to no rerank.
    """
    if not query or not query.strip():
        raise ValueError("query 不能为空")
    qen = (query_en or "").strip()
    if qen:
        # Cross-language: the candidate pool takes top-N by the nearest distance of the two paths, each chunk's sim takes the max of the two paths (= least distance)
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
        rows = conn.execute(sql, (vec, vec, k * 4)).fetchall()  # top-N candidates
        rerank_q = query
    out: list[dict] = []
    for r in rows:
        sim = float(r[-1])
        if sim < min_sim:          # the refusal threshold only enforces the bi-encoder sim (the reranker score does not take part)
            continue
        d = dict(zip(KB_KEYS, r[:len(KB_KEYS)]))
        d["sim"] = sim
        out.append(d)
    out = reranker.rerank(rerank_q, out)   # off by default = return as is; when on it only changes order/selection, not refusal
    return out[:k]


# ---------- Course guides (subjective experience corpus) semantic search ----------
# Physical isolation (option A): guides live only in course_guides, so factual queries (kb_search / course_detail) physically cannot hit them (student-facing red line 1/3).
GUIDE_COLS = "id, course_code, year, semester, section, text, source, profile_url, checked_at"
GUIDE_KEYS = ("id", "course_code", "year", "semester", "section", "text", "source",
              "profile_url", "checked_at")


def guide_search(conn, course_code: str, query: str, k: int = 4,
                 min_sim: float = 0.55) -> list[dict]:
    """Course-guide experience chunk semantic search: bge-m3 vector nearest neighbor, forced WHERE course_code=%s (guides are within a single course's scope, cross-course is not recalled), returns top-k.

    Same vector space as kb_search (same _embed routing: local Ollama / DeepInfra when EMBED_API_KEY is set); anything below min_sim is filtered out
    -- rather return nothing than give weakly related results (red line 3: do not fob the student off with loosely related experience). **Queries only course_guides**, never touches courses / kb_chunks (option A physical isolation).
    min_sim starts at 0.55; scan it with real questions before going live to set it, do not just guess."""
    code = (course_code or "").strip().upper()
    if not code:
        raise ValueError("course_code 不能为空")
    if not query or not query.strip():
        raise ValueError("query 不能为空")
    vec = _embed(query)
    sql = (f"SELECT {GUIDE_COLS}, 1-(embedding<=>%s::vector) AS sim FROM course_guides "
           f"WHERE course_code=%s ORDER BY embedding<=>%s::vector LIMIT %s")
    rows = conn.execute(sql, (vec, code, vec, k * 2)).fetchall()
    out: list[dict] = []
    for r in rows:
        sim = float(r[-1])
        if sim < min_sim:
            continue
        d = dict(zip(GUIDE_KEYS, r[:len(GUIDE_KEYS)]))
        d["sim"] = sim
        out.append(d)
    return out[:k]


# ---------- Single-course detail (course intro / prereq / assessment) ----------
DETAIL_COLS = ("code, title, units, level, description, prerequisite_raw, "
               "incompatible, assessments, learning_outcomes, topics, "
               "coordinator, coordinating_unit, has_exam, has_hurdle")
DETAIL_KEYS = ("code", "title", "units", "level", "description", "prerequisite_raw",
               "incompatible", "assessments", "learning_outcomes", "topics",
               "coordinator", "coordinating_unit", "has_exam", "has_hurdle")


def course_detail(conn, code: str) -> dict | None:
    """Full detail of a single course (intro/prereq/assessment/offering), aggregating the course's multiple offerings.

    Course-content fields (description/prereq etc.) take the latest row; offering semester/campus sum over all offerings.
    Returns None when the course code is not in the DB (the upper layer gives a graceful hint, no silent success).
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

    # build_where assembly check: slots -> parameterized (sql, params), all values go into params (injection safety is structural)
    print("== build_where 参数化验证 ==")
    for f in [
        {"has_exam": False},
        {"level": "Postgraduate Coursework", "units": 2},
        {"location": "St Lucia"},
        {"has_exam": False, "course_type_exclude": ["placement", "thesis", "research"]},
        {"course_type_only": ["thesis"]},
        {},  # empty -> ("", []), the caller decides whether to tolerate it
    ]:
        print(f"  {f}  ->  {build_where(f)}")

    # Build the index once at startup (write connection)
    with psycopg.connect(DSN) as conn:
        ensure_fts_index(conn)

    # The read path must also run under a read-only connection (only SELECT, no index build / no commit)
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
