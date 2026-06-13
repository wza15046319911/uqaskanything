"""
answer_eval.py — 端到端答案正确性评测(student-facing 红线 1/2/3 的确定性校验)

逐题跑 qa.run(conn, q) 拿最终答案,做确定性断言(不靠 LLM-judge):
  - source_has:答案必须带上期望的官方来源(URL 片段或「来源」块,红线 2)
  - answer_has:确定性答案必须含期望的课程码/专业/关键事实(program 等)
  - refuse=true:超纲高风险问题必须拒答(KB_REFUSE)或 empty,绝不编造(红线 3)
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
from app.services import qa, answer

_CODE_RE = re.compile(r"\b[A-Z]{4}\d{4}\b")


def _leaked_codes(ans: str, courses: list[dict]) -> set[str]:
    """答案正文(剔除护栏警告行)里出现、但不在检索结果集内的课程码——绝不该泄漏给用户。"""
    allowed = {c.get("code") for c in courses if c.get("code")}
    body = ans.split("[警告]")[0]              # guard_citations 的警告行会列出被剔除的越界码,排除之
    return {m for m in _CODE_RE.findall(body) if m not in allowed}


def _check(exp: dict, res: dict) -> list[str]:
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
            fails = _check(exp, res)
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
