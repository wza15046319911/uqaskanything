"""
kb_fetch_wayback.py — 从 Wayback Machine 抓 support FAQ 存档(绕开 support 反爬,合规)
(对应 plan.md 阶段二 / docs/kb_progress.md「support」)

support detail 页被 Akamai 级边缘反爬拦死(普通 HTTP / Playwright / 后端域 / REST
全 403,见 kb_browser_probe.py)。archive.org 存了约 38% 的 200 快照——抓 archive
完全绕开 support 反爬且合规。本脚本:
  1. CDX API 列出 support detail 的全部 200 存档,按 a_id 取最新快照
  2. 抓 `/web/<ts>id_/<原始URL>` 的原始 HTML 落 raw/support.my.uq.edu.au/
  3. 规范 URL 仍指向官方 detail 页(答案链回官方),记录快照时间

覆盖率受限于 archive.org;无存档的 a_id(约 525 篇)需正式渠道(UQ IT 要 KB 数据)。

用法(从 backend/ 跑):
    python -m app.scrapers.kb_fetch_wayback --limit 5      # 小批试
    python -m app.scrapers.kb_fetch_wayback                # 全量
"""
from __future__ import annotations
import re
import csv
import json
import time
import argparse
from pathlib import Path

import requests

from app.core.config import DATA_DIR
from app.scrapers.kb_fetch import _sha1, _now

HEADERS = {"User-Agent": "Mozilla/5.0 (uq-kb-crawler; wayback)"}
CANONICAL = "https://support.my.uq.edu.au/app/answers/detail/a_id/{aid}"
CDX = "http://web.archive.org/cdx/search/cdx"


def _support_aids(csv_path: Path) -> set[str]:
    aids: set[str] = set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["domain"] == "support.my.uq.edu.au":
                m = re.search(r"a_id/(\d+)", row["url"])
                if m:
                    aids.add(m.group(1))
    return aids


def latest_snapshots(want: set[str]) -> dict[str, tuple[str, str]]:
    """CDX -> {a_id: (timestamp, original_url)},只保留每个 a_id 最新的 200 快照。"""
    r = requests.get(CDX, headers=HEADERS, timeout=120, params={
        "url": "support.my.uq.edu.au/app/answers/detail", "matchType": "prefix",
        "filter": "statuscode:200", "output": "json", "fl": "original,timestamp"})
    best: dict[str, tuple[str, str]] = {}
    for original, ts in r.json()[1:]:
        m = re.search(r"a_id/(\d+)", original)
        if not m:
            continue
        aid = m.group(1)
        if aid in want and (aid not in best or ts > best[aid][0]):
            best[aid] = (ts, original)
    return best


def fetch_snapshot(aid: str, ts: str, original: str, out_dir: Path,
                   retries: int = 3) -> dict:
    snap = f"https://web.archive.org/web/{ts}id_/{original}"
    canonical = CANONICAL.format(aid=aid)
    last = None
    for i in range(retries):
        try:
            r = requests.get(snap, headers=HEADERS, timeout=60)
            r.raise_for_status()
            out_dir.mkdir(parents=True, exist_ok=True)
            html_path = out_dir / f"{_sha1(canonical)}.html"
            html_path.write_text(r.text, encoding="utf-8")
            return {
                "url": canonical,
                "url_hash": _sha1(canonical),
                "domain": "support.my.uq.edu.au",
                "http_status": 200,
                "content_hash": _sha1(r.text),
                "final_url": snap,
                "html_path": str(html_path.relative_to(DATA_DIR.parent)),
                "fetched_at": _now(),
                "source": "wayback",
                "snapshot_ts": ts,
                "a_id": aid,
            }
        except Exception as e:
            last = e
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))
    raise last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(DATA_DIR / "kb" / "urls.csv"))
    ap.add_argument("--out-dir", default=str(DATA_DIR / "kb" / "raw" / "support.my.uq.edu.au"))
    ap.add_argument("--manifest", default=str(DATA_DIR / "kb" / "fetched_faq.jsonl"))
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 个(试)")
    ap.add_argument("--delay", type=float, default=1.5, help="archive.org 请求间隔秒")
    ap.add_argument("--resume", action="store_true",
                    help="跳过 manifest 已抓的 a_id、append 不覆盖(补 selenium 缺的)")
    args = ap.parse_args()

    want = _support_aids(Path(args.csv))
    print(f"support a_id 清单:{len(want)}")
    snaps = latest_snapshots(want)
    missing = sorted(want - set(snaps))
    print(f"Wayback 有存档:{len(snaps)} ({len(snaps)/len(want)*100:.0f}%) | "
          f"无存档:{len(missing)}(需正式渠道)")

    manifest = Path(args.manifest)
    done: set[str] = set()
    if args.resume and manifest.exists():
        for ln in manifest.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                a = json.loads(ln).get("a_id")
                if a:
                    done.add(a)
    items = sorted((a, v) for a, v in snaps.items() if a not in done)
    if args.limit:
        items = items[:args.limit]
    print(f"已抓 {len(done)} | 本次补 {len(items)}")

    out_dir = Path(args.out_dir)
    records, failures = [], []
    mf = open(manifest, "a" if args.resume else "w", encoding="utf-8")
    for i, (aid, (ts, original)) in enumerate(items, 1):
        try:
            rec = fetch_snapshot(aid, ts, original, out_dir)
            records.append(rec)
            mf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            mf.flush()
            print(f"  [{i}/{len(items)}] ok a_id/{aid} (snap {ts[:8]})")
        except Exception as e:
            failures.append((aid, str(e)[:90]))
            print(f"  [{i}/{len(items)}] FAIL a_id/{aid}: {str(e)[:70]}")
        time.sleep(args.delay)
    mf.close()

    print(f"\n写入 {args.manifest}: {len(records)} 条 | 失败 {len(failures)}")
    print(f"  无存档 a_id(前 20):{missing[:20]}")
    if failures:
        for aid, why in failures[:15]:
            print(f"    a_id/{aid}: {why}")


if __name__ == "__main__":
    main()
