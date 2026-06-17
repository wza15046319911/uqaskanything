"""
kb_fetch.py — knowledge base phase two fetch (M1.5 pilot small-batch version)
(matches plan.md section 2 / section 1.5)

Select a batch of URLs from urls.csv, fetch raw HTML to data/kb/raw/<domain>/<sha1>.html,
and write data/kb/fetched.jsonl recording the mapping:
  url, url_hash, domain, http_status, content_hash, final_url, html_path,
  fetched_at, lastmod, redirected_home

Pilot shortcut: sync requests + polite interval, enough to fetch a few dozen pages; the full phase two
moves to httpx+asyncio + SQLite pages table per plan. Store HTML first without parsing -- changing the
chunking strategy only reruns local parse, no re-crawl.

Sampling: round-robin stratify by path_pattern, to cover several sections (test parser robustness).

Usage (run from backend/):
    python -m app.scrapers.kb_fetch --type article --per-pattern 4 --max 40
    python -m app.scrapers.kb_fetch --urls "https://a,https://b" \
        --manifest data/kb/fetched_article.jsonl   # directly fetch a few extra pages (append+dedup into manifest)
"""
from __future__ import annotations
import csv
import json
import time
import hashlib
import argparse
from datetime import datetime, timezone
from collections import defaultdict, OrderedDict
from pathlib import Path
from urllib.parse import urlparse

import requests

from app.core.config import DATA_DIR

HEADERS = {"User-Agent": "Mozilla/5.0 (uq-kb-crawler; pilot fetch)"}


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def select_urls(csv_path: Path, gtype: str, per_pattern: int, max_n: int) -> list[dict]:
    """read urls.csv, filter by guessed_type, then sample round-robin by path_pattern."""
    by_pat: "OrderedDict[str, list[dict]]" = OrderedDict()
    with open(csv_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if gtype and r["guessed_type"] != gtype:
                continue
            by_pat.setdefault(r["path_pattern"], []).append(r)
    picked: list[dict] = []
    round_i = 0
    while True:
        progressed = False
        for lst in by_pat.values():
            if round_i < len(lst) and (per_pattern == 0 or round_i < per_pattern):
                picked.append(lst[round_i])
                progressed = True
                if max_n and len(picked) >= max_n:
                    return picked
        if not progressed:
            break
        round_i += 1
    return picked


def fetch_one(url: str, out_dir: Path, retries: int = 3) -> dict:
    """fetch a single page to disk, return the record. on failure raise, caller counts it in failures."""
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
            if r.status_code >= 400:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            domain = urlparse(url).netloc
            dom_dir = out_dir / domain
            dom_dir.mkdir(parents=True, exist_ok=True)
            html_path = dom_dir / f"{_sha1(url)}.html"
            html_path.write_text(r.text, encoding="utf-8")
            final_path = urlparse(r.url).path.rstrip("/")
            redirected_home = final_path == "" and urlparse(url).path.rstrip("/") != ""
            return {
                "url": url,
                "url_hash": _sha1(url),
                "domain": domain,
                "http_status": r.status_code,
                "content_hash": _sha1(r.text),
                "final_url": r.url,
                "html_path": str(html_path.relative_to(DATA_DIR.parent)),
                "fetched_at": _now(),
                "redirected_home": redirected_home,
            }
        except Exception as e:
            last = e
            if i == retries - 1:
                raise
            time.sleep(1.5 * (i + 1))
    raise last  # never reached, just a safeguard


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(DATA_DIR / "kb" / "urls.csv"),
                    help="URL 清单(阶段一产出)")
    ap.add_argument("--type", default="article",
                    help="只抓该 guessed_type(article/faq/policy),空=不限")
    ap.add_argument("--per-pattern", type=int, default=4,
                    help="每个 path_pattern 最多取几条(round-robin 分层)")
    ap.add_argument("--max", type=int, default=40, help="总抓取上限")
    ap.add_argument("--out-dir", default=str(DATA_DIR / "kb" / "raw"),
                    help="原始 HTML 落盘根目录")
    ap.add_argument("--manifest", default=str(DATA_DIR / "kb" / "fetched.jsonl"),
                    help="抓取记录 JSONL")
    ap.add_argument("--delay", type=float, default=0.8, help="请求间隔秒")
    ap.add_argument("--urls", default="",
                    help="逗号分隔 URL 直接补抓(跳过 csv 选样);追加去重写入 --manifest")
    args = ap.parse_args()

    if args.urls:
        targets = [u.strip() for u in args.urls.split(",") if u.strip()]
        existing: set[str] = set()
        mpath = Path(args.manifest)
        if mpath.exists():
            for ln in mpath.read_text(encoding="utf-8").splitlines():
                if ln.strip():
                    existing.add(json.loads(ln)["url"])
        dup = [u for u in targets if u in existing]
        picked = [{"url": u, "lastmod": ""} for u in targets if u not in existing]
        print(f"直接补抓 {len(picked)} 个 URL(--urls),追加写入 {args.manifest}")
        if dup:
            print(f"  跳过已在 manifest 的 {len(dup)} 个:{dup}")
    else:
        csv_path = Path(args.csv)
        if not csv_path.exists():
            ap.error(f"找不到 {csv_path};先跑 kb_discover 产出 urls.csv")
        picked = select_urls(csv_path, args.type, args.per_pattern, args.max)
        patterns = sorted({r["path_pattern"] for r in picked})
        print(f"选中 {len(picked)} 个 URL,覆盖 {len(patterns)} 个 path_pattern:")
        print("  " + ", ".join(patterns))

    out_dir = Path(args.out_dir)
    records: list[dict] = []
    failures: list[tuple[str, str]] = []
    redirected: list[str] = []
    for i, r in enumerate(picked, 1):
        url = r["url"]
        try:
            rec = fetch_one(url, out_dir)
            rec["lastmod"] = r.get("lastmod", "")
            records.append(rec)
            if rec["redirected_home"]:
                redirected.append(url)
            flag = " [重定向首页/疑似下线]" if rec["redirected_home"] else ""
            print(f"  [{i}/{len(picked)}] ok {url}{flag}")
        except Exception as e:
            failures.append((url, str(e)[:100]))
            print(f"  [{i}/{len(picked)}] FAIL {url}: {str(e)[:80]}")
        time.sleep(args.delay)

    with open(args.manifest, "a" if args.urls else "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"\n写入 {args.manifest}: {len(records)} 条记录(原始 HTML 在 {out_dir})")
    print(f"  成功 {len(records)} | 失败 {len(failures)} | 重定向首页 {len(redirected)}")
    if redirected:
        print("  [重定向首页] " + ", ".join(redirected[:10]))
    if failures:
        print("  [失败]")
        for url, why in failures[:15]:
            print(f"    {url}: {why}")


if __name__ == "__main__":
    main()
