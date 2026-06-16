"""
answer_eval.py — 端到端答案正确性评测(student-facing 红线 1/2/3 的确定性校验)

逐题跑 qa.run(conn, q) 拿最终答案,做确定性断言(不靠 LLM-judge):
  - source_has:答案必须带上期望的官方来源(URL 片段或「来源」块,红线 2)
  - answer_has / answer_hasnt:答案必须含 / 必须不含期望的课程码/专业/关键事实
  - refuse=true:超纲高风险问题必须拒答(KB_REFUSE)或 empty,绝不编造(红线 3)
  - courses_satisfy:filter 命中的课必须逐行真满足结构化条件(如 {"has_exam": false}),
    不是只看路由对不对,而是核对返回的课程行本身正确(答案质量)
  - 专业范围(program_to_courses 自动校验):答案里的课码必须都属于该专业(独立拿
    program_course 核对),绝不串入别专业的课——问 BCS 专业课不能答其它专业的课(红线 1)
  - 引用安全(所有 course 模式自动校验,无需标注):答案正文出现的课程码必须都在
    检索结果集内,不得把越界(疑似虚构)课码泄漏给用户(红线 1,验证 guard_citations
    确实接进生产路径)

注意:答案正文由 LLM 生成,本评测只断言确定性属性(来源/课码集合/拒答),不判措辞质量。
LLM 后端非确定性,通过率会小幅波动;用于看趋势与定位回归,不是逐位回归。

用法(从 backend/ 跑,需 Postgres:5433 + KB 已灌库 + LLM 后端就绪):
    python -m app.pipelines.answer_eval
    python -m app.pipelines.answer_eval --golden data/eval/answers.jsonl --show-ok
"""
from __future__ import annotations
import json
import re
import argparse
from pathlib import Path

import psycopg

from app.core.config import DSN, DATA_DIR
from app.services import qa, answer, program_lookup

_CODE_RE = re.compile(r"\b[A-Z]{4}\d{4}\b")


def _leaked_codes(ans: str, courses: list[dict]) -> set[str]:
    """答案正文(剔除护栏警告行)里出现、但不在检索结果集内的课程码——绝不该泄漏给用户。"""
    allowed = {c.get("code") for c in courses if c.get("code")}
    body = ans.split("[警告]")[0]              # guard_citations 的警告行会列出被剔除的越界码,排除之
    return {m for m in _CODE_RE.findall(body) if m not in allowed}


def _program_scope_leak(conn, res: dict) -> set[str]:
    """program_to_courses 答案里出现、却不属于该专业(也非其禁修课)的课码——串了别专业的课。
    拿 program_course 表当 ground truth 独立核对,不信任答案构造逻辑(红线1:答非所专业即错)。"""
    plan = res.get("plan") or {}
    if plan.get("direction") != "program_to_courses":
        return set()
    progs = program_lookup.find_program(conn, plan.get("program_name") or "")
    if not progs:
        return set()
    pid = progs[0][0]
    member = {r[0] for r in conn.execute(
        "SELECT course_code FROM program_course WHERE program_id = %s", (pid,)).fetchall()}
    allowed = member | set(program_lookup.excluded_courses(conn, pid))
    body = (res.get("answer") or "").split("[警告]")[0]
    return {m for m in _CODE_RE.findall(body) if m not in allowed}


def _check(exp: dict, res: dict, conn) -> list[str]:
    """对单题端到端结果做确定性断言,返回失败原因列表(空=通过)。"""
    fails: list[str] = []
    ans = res.get("answer") or ""
    mode = res.get("mode")

    if "mode" in exp and mode != exp["mode"]:
        fails.append(f"mode {mode}≠{exp['mode']}")

    if exp.get("refuse"):
        if not (ans == answer.KB_REFUSE or mode == "empty"):
            fails.append(f"应拒答但给了答案(mode={mode}):{ans[:40]}…")

    if exp.get("source_has") and exp["source_has"] not in ans:
        fails.append(f"答案缺期望来源 '{exp['source_has']}'")

    for sub in exp.get("answer_has", []):
        if sub not in ans:
            fails.append(f"答案缺 '{sub}'")

    for sub in exp.get("answer_hasnt", []):
        if sub in ans:
            fails.append(f"答案含不应出现的 '{sub}'")

    # 答案质量:filter 命中的课必须真满足结构化条件(逐行核对返回的课程行,非只看 mode)。
    for field, val in (exp.get("courses_satisfy") or {}).items():
        bad = [c.get("code") for c in (res.get("courses") or []) if c.get(field) != val]
        if bad:
            fails.append(f"{len(bad)} 门课不满足 {field}={val}:{'、'.join(b for b in bad[:5] if b)}")

    # 答案质量:program_to_courses 答案绝不能串入别专业的课(独立拿 program_course 核对)。
    if mode == "program":
        leaked = _program_scope_leak(conn, res)
        if leaked:
            fails.append(f"答案含非本专业课码:{'、'.join(sorted(leaked))}")

    # 引用安全只对「LLM 生成、以 courses 为依据」的模式有意义;program 的确定性答案
    # 本就含用户问的课码(数据在 program_facts),course_detail/kb 也不以 courses 兜底。
    if mode in ("filter", "semantic", "hybrid"):
        leaked = _leaked_codes(ans, res.get("courses") or [])
        if leaked:
            fails.append(f"泄漏越界课码:{'、'.join(sorted(leaked))}")

    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(DATA_DIR / "eval" / "answers.jsonl"),
                    help="评测集 JSONL(每行 {q, mode?, source_has?, answer_has?, refuse?})")
    ap.add_argument("--show-ok", action="store_true", help="同时打印通过的用例")
    args = ap.parse_args()

    path = Path(args.golden)
    if not path.exists():
        ap.error(f"找不到评测集 {path}")
    cases = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not cases:
        ap.error(f"评测集为空:{path}")

    n = len(cases)
    ok = 0
    failures: list[tuple[str, str]] = []
    print(f"评测集:{path.name} | {n} 题(端到端,逐题跑 qa.run)\n")
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        for exp in cases:
            res = qa.run(conn, exp["q"], generate=True)
            fails = _check(exp, res, conn)
            if fails:
                failures.append((exp["q"], "; ".join(fails)))
            elif args.show_ok:
                print(f"[✓] {exp['q']}  -> {res['mode']}")
            ok += not fails

    print(f"\n=== 端到端答案评测({n} 题)===")
    print(f"通过:{ok}/{n} ({ok / n * 100:.0f}%)")
    if failures:
        print(f"\n失败({len(failures)}):")
        for q, why in failures:
            print(f"  [✗] {q}  | {why}")
    else:
        print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
