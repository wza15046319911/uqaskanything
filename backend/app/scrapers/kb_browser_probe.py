"""
kb_browser_probe.py — Playwright feasibility check (spike): can the support anti-bot be bypassed with a real browser
(matches plan.md phase four / docs/kb_progress.md "Playwright batch")

The support detail pages hard-403 plain HTTP (any UA). This script uses real chromium to visit the home page first to
get a session, then fetch a few detail pages, to see if it can get 200 + FAQ body. Only checks feasibility, no DB load.

Usage (run from backend/; needs playwright + chromium):
    python -m app.scrapers.kb_browser_probe
"""
from __future__ import annotations
import csv

from playwright.sync_api import sync_playwright
import trafilatura

from app.core.config import DATA_DIR

HOME = "https://support.my.uq.edu.au/"


def _support_urls(n: int = 4) -> list[str]:
    out: list[str] = []
    with open(DATA_DIR / "kb" / "urls.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["domain"] == "support.my.uq.edu.au":
                out.append(r["url"])
                if len(out) >= n:
                    break
    return out


def main():
    URLS = _support_urls()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="en-AU")
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = ctx.new_page()

        home = page.goto(HOME, wait_until="domcontentloaded", timeout=40000)
        print(f"首页:status={home.status if home else '?'}  (拿 session/cookie)")

        ok = 0
        for u in URLS:
            try:
                resp = page.goto(u, wait_until="domcontentloaded", timeout=40000, referer=HOME)
                status = resp.status if resp else None
                html = page.content()
                meta = trafilatura.extract_metadata(html)
                title = (meta.title if meta and meta.title else "").strip()
                body = trafilatura.extract(html) or ""
                good = status == 200 and len(body) > 200
                ok += good
                mark = "✓" if good else "✗"
                print(f"  {mark} status={status} body={len(body):>5}c  title={title[:70]!r}")
            except Exception as e:
                print(f"  ✗ {u}: {type(e).__name__}: {str(e)[:80]}")

        browser.close()
        print(f"\n成功 {ok}/{len(URLS)} —— "
              + ("Playwright 可绕过 403,可正式化抓 support" if ok
                 else "仍拿不到正文,需进一步(session/challenge 排查)"))


if __name__ == "__main__":
    main()
