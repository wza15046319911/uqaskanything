"""
kb_fetch_selenium.py — fetch support FAQ with headed real Chrome (pass the Akamai JS sensor anti-bot)
(matches plan.md phase two / docs/kb_progress.md "support")

support detail is blocked by an Akamai JS sensor: requests / Playwright-headless / backend domain /
REST all return 403. But **headed real Chrome (not chromium) + undetected-chromedriver** can pass
(tested 5/5). Live fetch, better than old Wayback snapshots (coverage also rose from 38% to the full 846).

Key points:
  - must be headed (headless gets detected by the sensor, do not set headless=True). needs local Google Chrome.
  - reuse one driver and fetch in order; each page has a quality gate (not Access Denied and body >100c), retry once if it fails.
  - write manifest incrementally and flush, no loss on interrupt; `--resume` skips already-fetched a_id and continues.
  - store only rendered HTML (raw), parsing still goes through kb_parse / later faq parser.

Usage (run from backend/, a Chrome window pops up, do not close it):
    python -m app.scrapers.kb_fetch_selenium --limit 15      # small batch try
    python -m app.scrapers.kb_fetch_selenium                 # full run (about 1 hour)
    python -m app.scrapers.kb_fetch_selenium --resume        # continue fetching
    each page's render wait is random within [--delay-min, --delay-max] (default 3-6s), to avoid a fixed
    interval being detected by Akamai;
    when rate-limited, widen the range and split into small batches: --resume --limit 30 --delay-min 5 --delay-max 10
"""
from __future__ import annotations
import re
import csv
import json
import time
import random
import argparse
from pathlib import Path

import trafilatura
import undetected_chromedriver as uc

from app.core.config import DATA_DIR
from app.scrapers.kb_fetch import _sha1, _now

HOME = "https://support.my.uq.edu.au/"
CANON = "https://support.my.uq.edu.au/app/answers/detail/a_id/{aid}"


def _support_aids(csv_path: Path) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["domain"] == "support.my.uq.edu.au":
                m = re.search(r"a_id/(\d+)", row["url"])
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    out.append(m.group(1))
    return out


def _done_aids(manifest: Path) -> set[str]:
    done: set[str] = set()
    if manifest.exists():
        for ln in manifest.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                a = json.loads(ln).get("a_id")
                if a:
                    done.add(a)
    return done


def _grab(driver, canonical: str, delay: float) -> str:
    """get once, return rendered HTML; empty/blocked is judged by the caller."""
    driver.get(canonical)
    time.sleep(delay)
    return driver.page_source


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(DATA_DIR / "kb" / "urls.csv"))
    ap.add_argument("--out-dir", default=str(DATA_DIR / "kb" / "raw" / "support.my.uq.edu.au"))
    ap.add_argument("--manifest", default=str(DATA_DIR / "kb" / "fetched_faq.jsonl"))
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 个(试)")
    ap.add_argument("--delay-min", type=float, default=3.0, help="每篇渲染等待秒下限")
    ap.add_argument("--delay-max", type=float, default=6.0, help="每篇渲染等待秒上限(在 [min,max] 间随机)")
    ap.add_argument("--resume", action="store_true", help="跳过 manifest 已抓的 a_id 续抓")
    ap.add_argument("--stop-after", type=int, default=12,
                    help="连续失败达此数即停(疑似撞 Akamai 限速,省得空跑)")
    args = ap.parse_args()

    aids = _support_aids(Path(args.csv))
    manifest = Path(args.manifest)
    done = _done_aids(manifest) if args.resume else set()
    todo = [a for a in aids if a not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"support a_id:{len(aids)} | 已抓 {len(done)} | 本次 {len(todo)}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    mf = open(manifest, "a" if args.resume else "w", encoding="utf-8")

    driver = uc.Chrome(headless=False)
    ok = fail = 0
    consec_fail = 0
    failed: list[tuple[str, str]] = []
    stopped = False
    try:
        driver.get(HOME)
        time.sleep(4)
        for i, aid in enumerate(todo, 1):
            canonical = CANON.format(aid=aid)
            try:
                html = _grab(driver, canonical, random.uniform(args.delay_min, args.delay_max))
                body = trafilatura.extract(html) or ""
                if "access denied" in html.lower() or len(body) < 100:
                    # retry with a longer wait (add 2s to the upper bound), give the rate-limited page more cooldown
                    html = _grab(driver, canonical,
                                 random.uniform(args.delay_max, args.delay_max + 2.0))
                    body = trafilatura.extract(html) or ""
                if "access denied" in html.lower() or len(body) < 100:
                    raise RuntimeError(f"blocked/empty (body={len(body)}c)")
                path = out_dir / f"{_sha1(canonical)}.html"
                path.write_text(html, encoding="utf-8")
                rec = {
                    "url": canonical, "url_hash": _sha1(canonical),
                    "domain": "support.my.uq.edu.au", "http_status": 200,
                    "content_hash": _sha1(html), "final_url": canonical,
                    "html_path": str(path.relative_to(DATA_DIR.parent)),
                    "fetched_at": _now(), "source": "selenium", "a_id": aid,
                }
                mf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                mf.flush()
                ok += 1
                consec_fail = 0
            except Exception as e:
                fail += 1
                consec_fail += 1
                failed.append((aid, str(e)[:60]))
                if consec_fail >= args.stop_after:        # consecutive failures -> likely Akamai rate limit, stop early to save time
                    stopped = True
                    print(f"\n连续 {consec_fail} 篇失败,疑似撞 Akamai 限速,提前停止"
                          f"(已到 {i}/{len(todo)})。等冷却后再 --resume 续抓。")
                    break
            if i % 20 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)}  ok={ok} fail={fail}")
            time.sleep(random.uniform(0.5, 1.5))
    finally:
        driver.quit()
        mf.close()

    print(f"\n完成:ok={ok} fail={fail} / 本次 {len(todo)}(累计已抓 {len(done) + ok})")
    if failed:
        print("  [失败 a_id]")
        for aid, why in failed[:20]:
            print(f"    {aid}: {why}")


if __name__ == "__main__":
    main()
