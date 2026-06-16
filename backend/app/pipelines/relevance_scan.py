"""
relevance_scan.py — 课程主题相关性评测(P0「课程检索相关性天花板」的回归护栏)

逐题跑 qa.run 拿端到端答案,确定性断言两类主题:
  - expect=relevant:语料确有讲该主题的课。须 mode∈{semantic,hybrid}、答案非空且不带「无强相关」
    诚实声明,且返回课集合里至少命中一个期望课码(codes 任一即可,宽松防脆)。
  - expect=no_strong_match:语料没有真正讲该主题的课(如游戏开发/加密货币)。系统绝不能自信地
    当成「X 课」列出——通过条件:带「未找到…强相关」诚实声明,或 mode=empty / 空答 / 无召回。

同时打印每题 top sim:用于直观展示「绝对 sim 无法分离」——真实低分主题(统计 0.556)与空主题
(游戏设计 0.550)几乎同分,故拒绝纯阈值方案、改由 answer 的相关性诚实指令(LLM 分类)兜底。

注意:答案正文由 LLM 生成,相关性诚实声明是 LLM 分类结果,非确定性,通过率会小幅波动;
用于看趋势与定位回归,不是逐位回归(同 answer_eval / llm_judge_eval)。

用法(从 backend/ 跑,需 Postgres:5433 + LLM 后端就绪):
    python -m app.pipelines.relevance_scan
    python -m app.pipelines.relevance_scan --golden data/eval/course_relevance.jsonl --show-ok
"""
from __future__ import annotations
import json
import argparse
from pathlib import Path

import psycopg

from app.core.config import DSN, DATA_DIR
from app.services import qa, answer


def _has_no_match_caveat(ans: str) -> bool:
    """答案是否带「未找到与 X 强相关 / 请自行甄别 / 语义最接近」的诚实兜底声明。"""
    if "请自行甄别" in ans or "语义最接近" in ans:
        return True
    return "强相关" in ans and ("未找到" in ans or "未能找到" in ans or "没有找到" in ans)


def _top_sim(res: dict) -> float | None:
    """端到端结果里返回课的最高向量相似度(semantic/hybrid 行带 sim);取不到返回 None。"""
    for c in res.get("courses") or []:
        if "sim" in c:
            return float(c["sim"])
    return None


def _codes_in(res: dict, codes: list[str]) -> list[str]:
    """期望课码里实际出现在返回课集合中的(命中即算召回成功)。"""
    got = {c.get("code") for c in (res.get("courses") or [])}
    return [c for c in codes if c in got]


def _check(exp: dict, res: dict) -> list[str]:
    """对单题主题查询做确定性断言,返回失败原因列表(空=通过)。"""
    fails: list[str] = []
    ans = res.get("answer") or ""
    mode = res.get("mode")
    caveat = _has_no_match_caveat(ans)

    if exp["expect"] == "relevant":
        if mode not in ("semantic", "hybrid"):
            fails.append(f"真实主题路由到 {mode}(期望 semantic/hybrid)")
        if not ans or ans == answer.EMPTY_ANSWER or mode == "empty":
            fails.append("真实主题却空答/empty")
        if caveat:
            fails.append("真实主题被误判「无强相关」(诚实声明误触发)")
        hit = _codes_in(res, exp.get("codes", []))
        if exp.get("codes") and not hit:
            fails.append(f"期望课码一个都没召回:{exp['codes']}")
    else:  # no_strong_match
        listed = bool(res.get("courses"))
        confidently_listed = (mode in ("semantic", "hybrid")
                              and listed and not caveat
                              and ans and ans != answer.EMPTY_ANSWER)
        if confidently_listed:
            fails.append("空主题被自信当成「X 课」列出(无诚实声明)")
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(DATA_DIR / "eval" / "course_relevance.jsonl"),
                    help="评测集 JSONL(每行 {q, topic, expect, codes?})")
    ap.add_argument("--show-ok", action="store_true", help="同时打印通过的用例")
    args = ap.parse_args()

    path = Path(args.golden)
    if not path.exists():
        ap.error(f"找不到评测集 {path}")
    cases = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not cases:
        ap.error(f"评测集为空:{path}")

    rel_ok = rel_n = empty_ok = empty_n = 0
    failures: list[tuple[str, str]] = []
    rel_top_sims: list[float] = []      # 真实主题 top sim(过的)
    empty_top_sims: list[float] = []    # 空主题 top sim
    print(f"评测集:{path.name} | {len(cases)} 题(端到端,逐题跑 qa.run)\n")
    print(f"{'topic':14s} {'expect':16s} {'mode':9s} {'top_sim':8s} {'caveat':7s} 结果")
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        for exp in cases:
            res = qa.run(conn, exp["q"], generate=True)
            fails = _check(exp, res)
            ts = _top_sim(res)
            caveat = _has_no_match_caveat(res.get("answer") or "")
            if exp["expect"] == "relevant":
                rel_n += 1
                rel_ok += not fails
                if not fails and ts is not None:
                    rel_top_sims.append(ts)
            else:
                empty_n += 1
                empty_ok += not fails
                if ts is not None:
                    empty_top_sims.append(ts)
            mark = "✓" if not fails else "✗ " + "; ".join(fails)
            ts_str = f"{ts:.3f}" if ts is not None else "  -  "
            if fails or args.show_ok:
                print(f"{exp['topic']:14s} {exp['expect']:16s} {str(res['mode']):9s} "
                      f"{ts_str:8s} {'是' if caveat else '否':7s} {mark}")
            if fails:
                failures.append((exp["topic"], "; ".join(fails)))

    print(f"\n=== 课程主题相关性评测 ===")
    print(f"真实主题(应相关):     {rel_ok}/{rel_n} 通过")
    print(f"空主题(应无强相关):   {empty_ok}/{empty_n} 通过")
    # 直观展示「绝对 sim 无法分离」:真实主题最低 top sim vs 空主题最高 top sim 必然重叠
    if rel_top_sims and empty_top_sims:
        print(f"\nsim 重叠证据(故不用纯阈值):")
        print(f"  真实主题最低 top_sim = {min(rel_top_sims):.3f}")
        print(f"  空主题最高 top_sim   = {max(empty_top_sims):.3f}")
        if max(empty_top_sims) >= min(rel_top_sims):
            print(f"  → 空主题最高分 ≥ 真实主题最低分:任何纯 sim 阈值都会误伤,改由相关性诚实指令裁定。")
    if failures:
        print(f"\n失败({len(failures)}):")
        for topic, why in failures:
            print(f"  [✗] {topic}  | {why}")
    else:
        print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
