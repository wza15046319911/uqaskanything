"""
kb_discover.py — knowledge base crawl phase one: URL discovery (produces urls.csv)
(matches plan.md section 1 / milestone M1)

Flow (one domain after another):
  1. robots.txt -> take the declared Sitemap address + Disallow rules
  2. if none declared, fall back to https://<domain>/sitemap.xml; expand sitemap index into all URLs
  3. path blacklist + robots Disallow filter
  4. fallback for subsites without sitemap: same-domain BFS (depth/page count capped)
  5. write data/kb/urls.csv, fields: url, domain, path_pattern, guessed_type, lastmod, source

Acceptance (M1): manually scan urls.csv, confirm each domain's coverage and scale are reasonable, then move to phase two.

Usage (run from backend/):
    python -m app.scrapers.kb_discover
    python -m app.scrapers.kb_discover --domains support.my.uq.edu.au
    python -m app.scrapers.kb_discover --no-bfs --out data/kb/urls.csv
"""
from __future__ import annotations
import csv
import gzip
import time
import argparse
from collections import deque
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser

import requests
from lxml import etree, html as lxml_html

from app.core.config import DATA_DIR

HEADERS = {"User-Agent": "Mozilla/5.0 (uq-kb-crawler; discovery)"}

# phase one target domains (plan section 0). priority is record only, it does not affect discovery logic.
# these three sites have an XML sitemap, requests+lxml can discover URLs.
DOMAINS: list[tuple[str, str]] = [
    ("support.my.uq.edu.au", "P0"),
    ("my.uq.edu.au", "P0"),
    ("study.uq.edu.au", "P1"),
]

# deferred to phase four (Playwright), not discovered in phase one:
#   - policies.uq.edu.au (P1, the old ppl.app.uq.edu.au whole domain 301 moved here): JS-rendered SPA, no XML sitemap
#   - library.uq.edu.au (P2): also a JS SPA, home page has only 1 <a>, no sitemap
#   - graduate-school.uq.edu.au (P2): DNS no longer resolves, domain offline

# robots Disallow override whitelist: UQ sites with explicit user authorization to crawl.
# support.my.uq.edu.au robots Disallow the whole site for non-major search engine UA (Oracle Service Cloud
# charges by page views), but it is the P0 core FAQ/KB, included with authorization.
ROBOTS_OVERRIDE = {"support.my.uq.edu.au"}

# path-segment blacklist (shared by all domains, plan section 0): exclude if any path segment hits.
# exact match by segment, will not wrongly hit /about-us, /news-feed (segment names are about-us / news-feed).
EXCLUDE_SEG = (
    "news", "events", "event", "contact", "about",
    "staff", "people", "profile", "media", "login", "search",
)

# per-domain exclude (also matched by segment): study's study-options are program/course detail pages
# (already in DB, plan tells to exclude), stories are marketing text.
PER_DOMAIN_EXCLUDE: dict[str, tuple[str, ...]] = {
    "study.uq.edu.au": ("study-options", "stories"),
}

# guessed_type grouped by domain
FAQ_DOMAINS = {"support.my.uq.edu.au"}
POLICY_DOMAINS = {"policies.uq.edu.au"}


def _get(url: str, retries: int = 3, timeout: int = 30) -> requests.Response | None:
    """GET with retry; returns None on final failure (caller counts it as skip/fail, not silently swallowed)."""
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 404:
                return r
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            if i == retries - 1:
                print(f"  [warn] GET 失败 {url}: {last}")
                return None
            time.sleep(1.5 * (i + 1))
    return None


def _guessed_type(domain: str) -> str:
    if domain in FAQ_DOMAINS:
        return "faq"
    if domain in POLICY_DOMAINS:
        return "policy"
    return "article"


def _path_pattern(url: str) -> str:
    """take the first path segment as a rough group, like /managing-my-program/... -> '/managing-my-program/'."""
    segs = [s for s in urlparse(url).path.split("/") if s]
    return f"/{segs[0]}/" if segs else "/"


def _excluded(url: str, domain: str) -> bool:
    segs = {s.lower() for s in urlparse(url).path.split("/") if s}
    blocked = set(EXCLUDE_SEG) | set(PER_DOMAIN_EXCLUDE.get(domain, ()))
    return bool(segs & blocked)


def load_robots(domain: str) -> RobotFileParser | None:
    """read robots.txt and parse into RobotFileParser; return None if not available."""
    resp = _get(f"https://{domain}/robots.txt")
    if resp is None or resp.status_code == 404:
        return None
    rp = RobotFileParser()
    rp.parse(resp.text.splitlines())
    return rp


def _xml_root(resp: requests.Response):
    """sitemap response -> lxml root; supports .gz. return None on parse failure."""
    content = resp.content
    if resp.url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except Exception as e:
            print(f"  [warn] gunzip 失败 {resp.url}: {e}")
            return None
    try:
        return etree.fromstring(content)
    except Exception as e:
        print(f"  [warn] XML 解析失败 {resp.url}: {e}")
        return None


def collect_from_sitemaps(seeds: list[str], delay: float) -> list[tuple[str, str]]:
    """recursively expand sitemap / sitemapindex, return [(url, lastmod), ...]."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    queue = deque(seeds)
    while queue:
        sm = queue.popleft()
        if sm in seen:
            continue
        seen.add(sm)
        resp = _get(sm)
        if resp is None or resp.status_code == 404:
            continue
        root = _xml_root(resp)
        if root is None:
            continue
        tag = etree.QName(root).localname
        for child in root:
            loc = child.find("{*}loc")
            if loc is None or not (loc.text or "").strip():
                continue
            loc_url = loc.text.strip()
            if tag == "sitemapindex":
                queue.append(loc_url)
            else:  # urlset
                lm = child.find("{*}lastmod")
                lastmod = (lm.text or "").strip() if lm is not None else ""
                out.append((loc_url, lastmod))
        time.sleep(delay)
    return out


def bfs(domain: str, rp: RobotFileParser | None,
        max_depth: int, max_pages: int, delay: float) -> list[tuple[str, str]]:
    """same-domain BFS fallback: used when there is no sitemap. lastmod left empty."""
    start = f"https://{domain}/"
    seen: set[str] = {start}
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    out: list[tuple[str, str]] = []
    while queue and len(out) < max_pages:
        url, depth = queue.popleft()
        resp = _get(url)
        if resp is None or resp.status_code == 404:
            continue
        out.append((url, ""))
        if depth >= max_depth:
            continue
        try:
            doc = lxml_html.fromstring(resp.content)
        except Exception:
            continue
        for href in doc.xpath("//a/@href"):
            nxt = urljoin(url, href.split("#")[0])
            p = urlparse(nxt)
            if p.scheme not in ("http", "https") or p.netloc != domain:
                continue
            nxt = nxt.rstrip("/") or nxt
            if nxt in seen:
                continue
            if rp is not None and not rp.can_fetch(HEADERS["User-Agent"], nxt):
                continue
            seen.add(nxt)
            queue.append((nxt, depth + 1))
        time.sleep(delay)
    return out


def discover_domain(domain: str, use_bfs: bool, max_depth: int,
                    max_pages: int, delay: float) -> tuple[list[dict], dict]:
    """single-domain discovery. return (records, stats). records already filtered."""
    rp = load_robots(domain)
    seeds = list(rp.site_maps() or []) if rp else []
    source = "sitemap"
    if seeds:
        raw = collect_from_sitemaps(seeds, delay)
    else:
        raw = collect_from_sitemaps([f"https://{domain}/sitemap.xml"], delay)
    if not raw and use_bfs:
        source = "bfs"
        raw = bfs(domain, rp, max_depth, max_pages, delay)

    records: list[dict] = []
    seen: set[str] = set()
    n_dup = n_excl = n_robots = 0
    gtype = _guessed_type(domain)
    obey_robots = rp is not None and domain not in ROBOTS_OVERRIDE
    for url, lastmod in raw:
        if url in seen:
            n_dup += 1
            continue
        seen.add(url)
        if urlparse(url).netloc != domain:
            continue
        if _excluded(url, domain):
            n_excl += 1
            continue
        if obey_robots and not rp.can_fetch(HEADERS["User-Agent"], url):
            n_robots += 1
            continue
        records.append({
            "url": url,
            "domain": domain,
            "path_pattern": _path_pattern(url),
            "guessed_type": gtype,
            "lastmod": lastmod,
            "source": source,
        })
    stats = {
        "domain": domain, "source": source, "raw": len(raw),
        "kept": len(records), "dup": n_dup,
        "excluded": n_excl, "robots_blocked": n_robots,
        "has_sitemap": bool(seeds),
        "robots_override": domain in ROBOTS_OVERRIDE,
    }
    return records, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domains", nargs="*",
                    help="只跑这些域名(默认跑 DOMAINS 全部)")
    ap.add_argument("--out", default=str(DATA_DIR / "kb" / "urls.csv"),
                    help="输出 CSV")
    ap.add_argument("--delay", type=float, default=0.5, help="请求间隔秒")
    ap.add_argument("--no-bfs", action="store_true",
                    help="禁用 BFS 降级(只信 sitemap)")
    ap.add_argument("--bfs-max-depth", type=int, default=4, help="BFS 最大深度")
    ap.add_argument("--bfs-max-pages", type=int, default=500, help="BFS 单域页数上限")
    args = ap.parse_args()

    targets = [(d, p) for d, p in DOMAINS
               if not args.domains or d in args.domains]
    if not targets:
        ap.error(f"--domains 没匹配到任何已知域名;已知:{[d for d, _ in DOMAINS]}")

    all_records: list[dict] = []
    all_stats: list[dict] = []
    for domain, priority in targets:
        print(f"\n=== {priority} {domain} ===")
        records, stats = discover_domain(
            domain, use_bfs=not args.no_bfs,
            max_depth=args.bfs_max_depth, max_pages=args.bfs_max_pages,
            delay=args.delay)
        all_records.extend(records)
        all_stats.append(stats)
        print(f"  source={stats['source']} sitemap={stats['has_sitemap']} "
              f"raw={stats['raw']} kept={stats['kept']} "
              f"dup={stats['dup']} excluded={stats['excluded']} "
              f"robots_blocked={stats['robots_blocked']}")

    fields = ["url", "domain", "path_pattern", "guessed_type", "lastmod", "source"]
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for rec in all_records:
            w.writerow(rec)

    print(f"\n写入 {args.out}: {len(all_records)} 个 URL")
    print("  按域名:")
    for s in all_stats:
        note = " [robots 已授权覆盖]" if s["robots_override"] else ""
        print(f"    {s['domain']:<28} kept={s['kept']:<5} "
              f"source={s['source']} "
              f"(raw={s['raw']}, excluded={s['excluded']}, "
              f"robots_blocked={s['robots_blocked']}){note}")
    empty = [s["domain"] for s in all_stats if s["kept"] == 0]
    if empty:
        print(f"  [注意] 0 URL 的域名(需人工查 sitemap/robots):{', '.join(empty)}")


if __name__ == "__main__":
    main()
