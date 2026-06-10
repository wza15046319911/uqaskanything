"""scrape_aux_rules.py — 轻量重抓:只取各 program 的 auxiliaryRules(程序级附加规则,含禁课)。
不展开 plan(比全量 program_scraper 轻得多,每程序 1 次请求),限速防把 UQ 抓崩。
跳过的程序会计数并打印原因,不静默。

用法:
    python scrape_aux_rules.py --out aux_rules.jsonl --delay 1
    python scrape_aux_rules.py --limit 3        # 抽样测试
"""
from __future__ import annotations
import os
import json
import time
import argparse

import psycopg

from app.scrapers import program_scraper as ps

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:uqrag@localhost:5433/uq_courses")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="aux_rules.jsonl")
    ap.add_argument("--delay", type=float, default=1.0, help="每次请求间隔秒")
    ap.add_argument("--year", default="2026")
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 个(抽样)")
    args = ap.parse_args()

    with psycopg.connect(DSN) as conn:
        pids = [r[0] for r in conn.execute(
            "SELECT program_id FROM programs ORDER BY program_id").fetchall()]
    if args.limit:
        pids = pids[:args.limit]

    done = ok = skip = 0
    with open(args.out, "w", encoding="utf-8") as f:
        for pid in pids:
            try:
                data = ps._appdata(ps._get(ps.LIST.format(pid=pid, year=args.year)))
                aux = ps.parse_aux_rules(data) if data else []
                f.write(json.dumps({"program_id": pid, "aux_rules": aux}, ensure_ascii=False) + "\n")
                f.flush()
                ok += 1
            except Exception as e:
                skip += 1
                print(f"  [skip] {pid}: {type(e).__name__}: {e}")
            done += 1
            if done % 25 == 0:
                print(f"  进度 {done}/{len(pids)} (ok={ok} skip={skip})")
            time.sleep(args.delay)
    print(f"完成 {done}/{len(pids)}:ok={ok} skip={skip} -> {args.out}")


if __name__ == "__main__":
    main()
