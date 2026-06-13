"""
route_eval.py — planner 路由准确率评测(对应「提高问答准确率」第一杠杆:路由)

对一组标注了期望 mode/direction 的真实问题跑 planner.plan(),逐题比对路由是否正确,
按期望 mode 分组打印准确率、mode 误判混淆(期望→实际),并列出每条错例便于补规则。

评测对象只是 planner 的分类决策:mode、direction(program 类)、where 关键条件、
semantic_query 是否给出。不含 qa 层基于相似度的 KB 兜底转移(那是阈值调参,另行评测)。
只校验 golden 里给了的字段(meaningful 而非 brittle:where 不做全等匹配,只查关键子串)。

注意:LLM 后端非确定性,准确率可能随运行小幅波动;用它看趋势与定位错例,不是逐位回归。

用法(从 backend/ 跑,需 Postgres:5433 + LLM 后端就绪):
    python -m app.pipelines.route_eval
    python -m app.pipelines.route_eval --golden data/eval/routing.jsonl --show-ok
"""
from __future__ import annotations
import json
import argparse
from pathlib import Path

import psycopg

from app.core.config import DSN, DATA_DIR
from app.services import planner


def _route_of(question: str, schema: str, conn) -> tuple[str, str, str, bool]:
    """跑 planner.plan,返回 (mode, direction, where, 是否给了 semantic);
    plan 抛 ValueError(无法形成检索条件)记为 mode='empty'。"""
    try:
        p = planner.plan(question, schema_doc=schema, conn=conn)
    except ValueError:
        return ("empty", "", "", False)
    return (p["mode"], p.get("direction", ""), p.get("where", ""),
            bool(p.get("semantic_query")))


def _check(exp: dict, mode: str, direction: str, where: str, has_sem: bool) -> tuple[bool, list[str]]:
    """逐字段比对,只校验 golden 给了的字段;返回 (全对?, [失败原因...])。"""
    fails: list[str] = []
    if mode != exp["mode"]:
        fails.append(f"mode {mode}≠{exp['mode']}")
    if exp.get("direction") and direction != exp["direction"]:
        fails.append(f"direction {direction or '∅'}≠{exp['direction']}")
    for sub in exp.get("where_has", []):
        if sub.lower() not in where.lower():
            fails.append(f"where 缺 '{sub}'(实际 {where or '∅'})")
    if "semantic" in exp and has_sem != exp["semantic"]:
        fails.append(f"semantic={'有' if has_sem else '无'},期望{'有' if exp['semantic'] else '无'}")
    return (not fails, fails)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(DATA_DIR / "eval" / "routing.jsonl"),
                    help="评测集 JSONL(每行 {q, mode, direction?, where_has?, semantic?})")
    ap.add_argument("--show-ok", action="store_true", help="同时打印通过的用例")
    args = ap.parse_args()

    path = Path(args.golden)
    if not path.exists():
        ap.error(f"找不到评测集 {path}")
    cases = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not cases:
        ap.error(f"评测集为空:{path}")

    n = len(cases)
    full_ok = mode_ok = 0
    by_mode: dict[str, list[int]] = {}                 # 期望 mode -> [整体正确数, 总数]
    confusion: dict[tuple[str, str], int] = {}         # (期望 mode, 实际 mode) -> 次数
    failures: list[tuple[str, str, str]] = []

    print(f"[backend={planner.llm.backend_name()}] 评测集:{path.name} | {n} 题\n")
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        schema = planner.build_schema_doc(conn)
        for exp in cases:
            mode, direction, where, has_sem = _route_of(exp["q"], schema, conn)
            ok, fails = _check(exp, mode, direction, where, has_sem)
            full_ok += ok
            mode_ok += (mode == exp["mode"])
            slot = by_mode.setdefault(exp["mode"], [0, 0])
            slot[0] += ok
            slot[1] += 1
            if mode != exp["mode"]:
                confusion[(exp["mode"], mode)] = confusion.get((exp["mode"], mode), 0) + 1
            if not ok:
                failures.append((exp["q"], exp["mode"], "; ".join(fails)))
            elif args.show_ok:
                tail = f"/{direction}" if direction else ""
                print(f"[✓] {exp['q']}  -> {mode}{tail}")

    print(f"\n=== 路由评测({n} 题)===")
    print(f"整体正确(mode + 关键字段全对):{full_ok}/{n} ({full_ok / n * 100:.0f}%)")
    print(f"mode 正确率:                    {mode_ok}/{n} ({mode_ok / n * 100:.0f}%)")
    print("\n按期望 mode 分组(整体正确数/总数):")
    for m in sorted(by_mode):
        c, t = by_mode[m]
        print(f"  {m:<14} {c}/{t}")
    if confusion:
        print("\nmode 误判(期望 -> 实际):")
        for (exp_m, got_m), cnt in sorted(confusion.items(), key=lambda kv: -kv[1]):
            print(f"  {exp_m} -> {got_m} : {cnt}")
    if failures:
        print(f"\n错例({len(failures)}):")
        for q, em, why in failures:
            print(f"  [✗] {q}  | 期望 {em} | {why}")
    else:
        print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
