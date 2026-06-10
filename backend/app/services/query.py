"""
query.py — 阶段四:查询层
自然语言问题 -> 本地 qwen2.5-coder 出「查询计划」-> 结构化过滤 / 向量检索 / 混合。

分工(对应「确定性决策用代码,语言任务交模型」):
  - LLM 只做语言活:判类型(filter/semantic/hybrid)、写 WHERE 表达式、给语义关键词
  - 代码做确定性活:SELECT-only 拦截 + 只读连接、SQL 拼装、向量检索、结果输出

用法:
    python query.py "哪些课没有考试"
    python query.py "找跟机器学习相关的课"
    python query.py "研究生阶段跟会计相关的课"
"""
from __future__ import annotations
import os
import re
import json
import argparse

import requests
import psycopg

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:uqrag@localhost:5433/uq_courses")
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
LLM = os.environ.get("LLM_MODEL", "qwen2.5-coder:7b")
EMBED_MODEL = "bge-m3"

SELECT_COLS = "code, title, semester, level, units, location, has_exam"

LOWCARD = ["semester", "location", "attendance_mode", "level"]  # 低基数列,枚举值实时取

PROMPT = """你是课程库查询规划器。把用户问题转成 JSON 查询计划,只输出 JSON,不要解释。
{schema}
规则:
- mode="filter":只有结构化条件(学期/有无考试/hurdle/本研/学分等)。给 where,semantic_query 留空。
- mode="semantic":只有模糊主题/学科(如"跟机器学习相关")。给 semantic_query,where 留空。
- mode="hybrid":既有结构化条件又有模糊主题/学科。两者都给。
- 【关键】学科/专业/主题(计算机/人工智能/金融/网络安全/心理学…)一律走 semantic_query,用**英文**表达;**绝不能**用 title/code/description 做 LIKE 匹配(课名是英文,学科还横跨多个课程码)。
- 缩写也算学科,必须翻成英文放进 semantic_query,**绝不能因为不认识就丢弃**:CS=computer science、AI=artificial intelligence、ML=machine learning、IT=information technology、EE=electrical engineering。
- where 只能用这些列:semester, year, location, attendance_mode, level, units, has_exam, has_hurdle。字符串单引号,布尔 true/false,不写分号/SELECT,不碰 title/description 等文本列。
- 严格输出:{{"mode":"...","where":"...","semantic_query":"..."}}

例子:
- "没有考试的课" -> {{"mode":"filter","where":"has_exam=false","semantic_query":""}}
- "找跟机器学习相关的课" -> {{"mode":"semantic","where":"","semantic_query":"machine learning"}}
- "计算机相关、没有hurdle的课" -> {{"mode":"hybrid","where":"has_hurdle=false","semantic_query":"computer science"}}
- "CS有哪些没有考试的课" -> {{"mode":"hybrid","where":"has_exam=false","semantic_query":"computer science"}}

用户问题:{q}"""

BANNED = re.compile(r"(;|--|/\*|\b(insert|update|delete|drop|alter|create|truncate|grant|revoke)\b)", re.I)


def build_schema_doc(conn) -> str:
    enums = {c: [r[0] for r in conn.execute(
        f"SELECT DISTINCT {c} FROM courses WHERE {c} IS NOT NULL ORDER BY 1")]
        for c in LOWCARD}
    return f"""表 courses(每行一门课):
  code TEXT              课程码,如 CSSE1001
  title TEXT             课程名
  semester TEXT          实际值:{enums['semester']}
  year INT               年份,如 2026
  location TEXT          实际值:{enums['location']}
  attendance_mode TEXT   实际值:{enums['attendance_mode']}
  level TEXT             实际值:{enums['level']}
  units REAL             学分
  coordinating_unit TEXT 开课学院(自由文本)
  coordinator TEXT       协调人
  has_exam BOOLEAN       是否含考试(true/false,不加引号)
  has_hurdle BOOLEAN     是否含 hurdle
  (description / learning_outcomes / topics 等文本不在结构化列里,模糊主题要走语义检索)"""


def llm_plan(q: str, schema: str) -> dict:
    r = requests.post(f"{OLLAMA}/api/chat", json={
        "model": LLM,
        "messages": [{"role": "user", "content": PROMPT.format(schema=schema, q=q)}],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0},
    }, timeout=120)
    r.raise_for_status()
    plan = json.loads(r.json()["message"]["content"])
    if plan.get("mode") not in ("filter", "semantic", "hybrid"):
        raise ValueError(f"非法 mode: {plan!r}")
    return plan


TEXT_COLS = re.compile(r"\b(title|description|search_blob|learning_outcomes|topics)\b", re.I)


def guard_where(where: str) -> str:
    if not where or not where.strip():
        raise ValueError("filter/hybrid 模式缺少 where 条件")
    if BANNED.search(where):
        raise ValueError(f"where 含非法内容,已拦截:{where!r}")
    if TEXT_COLS.search(where):                  # 主题/学科不能用文本列过滤,应走 semantic
        raise ValueError(f"where 不应过滤文本列(主题走 semantic):{where!r}")
    return where.strip()


def embed(text: str) -> str:
    v = requests.post(f"{OLLAMA}/api/embeddings",
                      json={"model": EMBED_MODEL, "prompt": text}, timeout=120).json()["embedding"]
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def run(conn, q: str):
    plan = llm_plan(q, build_schema_doc(conn))
    mode = plan["mode"]
    where = plan.get("where", "")
    sq = plan.get("semantic_query", "")

    if mode == "filter":
        where = guard_where(where)
        sql = f"SELECT {SELECT_COLS} FROM courses WHERE {where} ORDER BY code"
        return mode, f"WHERE {where}", conn.execute(sql).fetchall(), False

    if mode == "semantic":
        if not sq:
            raise ValueError("semantic 模式缺少 semantic_query")
        vec = embed(sq)
        sql = (f"SELECT {SELECT_COLS}, 1-(embedding<=>%s::vector) sim FROM courses "
               f"ORDER BY embedding<=>%s::vector LIMIT 8")
        return mode, f"semantic='{sq}'", conn.execute(sql, (vec, vec)).fetchall(), True

    where = guard_where(where)
    vec = embed(sq)
    sql = (f"SELECT {SELECT_COLS}, 1-(embedding<=>%s::vector) sim FROM courses "
           f"WHERE {where} ORDER BY embedding<=>%s::vector LIMIT 8")
    return mode, f"WHERE {where} + semantic='{sq}'", conn.execute(sql, (vec, vec)).fetchall(), True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", help="自然语言问题")
    args = ap.parse_args()

    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        mode, how, rows, has_sim = run(conn, args.question)

    print(f"[mode={mode}] {how}")
    print(f"命中 {len(rows)} 门:")
    for row in rows:
        code, title, sem, level, units, loc, has_exam = row[:7]
        tail = f"  sim={row[7]:.3f}" if has_sim else ""
        print(f"  {code}  {title}  ({sem}, {level}, exam={has_exam}){tail}")


if __name__ == "__main__":
    main()
