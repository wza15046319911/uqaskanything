"""
kb_fetch_selenium.py — 用 headed 真实 Chrome 抓 support FAQ(过 Akamai JS sensor 反爬)
(对应 plan.md 阶段二 / docs/kb_progress.md「support」)

support detail 被 Akamai JS sensor 反爬:requests / Playwright-headless / 后端域 /
REST 全 403。但 **headed 真实 Chrome(非 chromium)+ undetected-chromedriver** 能过
(实测 5/5)。实时抓取,优于 Wayback 旧快照(覆盖率也从 38% 提到全量 846)。

要点:
  - 必须 headed(headless 会被 sensor 检测,勿改 headless=True)。需本机 Google Chrome。
  - 复用单个 driver 顺序抓;每篇质量门槛(非 Access Denied 且正文 >100c),不过则重试一次。
  - 增量写 manifest 并 flush,中断不丢;`--resume` 跳过已抓 a_id 续抓。
  - 只存渲染后 HTML(raw),解析仍走 kb_parse / 后续 faq 解析器。

用法(从 backend/ 跑,会弹 Chrome 窗口,期间勿关):
    python -m app.scrapers.kb_fetch_selenium --limit 15      # 小批试
    python -m app.scrapers.kb_fetch_selenium                 # 全量(约 1 小时)
    python -m app.scrapers.kb_fetch_selenium --resume        # 续抓
"""
from __future__ import annotations
import re
import csv
import json
import time
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
    """get 一次,返回渲染后 HTML;空/被拦由调用方判断。"""
    driver.get(canonical)
    time.sleep(delay)
    return driver.page_source


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(DATA_DIR / "kb" / "urls.csv"))
    ap.add_argument("--out-dir", default=str(DATA_DIR / "kb" / "raw" / "support.my.uq.edu.au"))
    ap.add_argument("--manifest", default=str(DATA_DIR / "kb" / "fetched_faq.jsonl"))
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 个(试)")
    ap.add_argument("--delay", type=float, default=2.5, help="每篇渲染等待秒")
    ap.add_argument("--resume", action="store_true", help="跳过 manifest 已抓的 a_id 续抓")
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
    failed: list[tuple[str, str]] = []
    try:
        driver.get(HOME)
        time.sleep(4)
        for i, aid in enumerate(todo, 1):
            canonical = CANON.format(aid=aid)
            try:
                html = _grab(driver, canonical, args.delay)
                body = trafilatura.extract(html) or ""
                if "access denied" in html.lower() or len(body) < 100:
                    html = _grab(driver, canonical, args.delay + 1.5)   # 重试一次
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
            except Exception as e:
                fail += 1
                failed.append((aid, str(e)[:60]))
            if i % 20 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)}  ok={ok} fail={fail}")
            time.sleep(0.5)
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
