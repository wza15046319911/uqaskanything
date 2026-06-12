"""
kb_discover.py — 知识库爬取 阶段一:URL 发现(产出 urls.csv)
(对应 plan.md 第 1 节 / 里程碑 M1)

流程(每个域名依次):
  1. robots.txt -> 取声明的 Sitemap 地址 + Disallow 规则
  2. 无声明则退回 https://<domain>/sitemap.xml;sitemap index 递归展开成全量 URL
  3. 路径黑名单 + robots Disallow 过滤
  4. 无 sitemap 的子站降级:同域 BFS(深度/页数有上限)
  5. 写 data/kb/urls.csv,字段:url, domain, path_pattern, guessed_type, lastmod, source

验收(M1):人工扫一遍 urls.csv,确认各域名覆盖面与量级合理,再进入阶段二。

用法(从 backend/ 跑):
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

# 阶段一目标域名(plan 第 0 节)。优先级仅作记录,不影响发现逻辑。
# 这三个站有 XML sitemap,requests+lxml 即可发现 URL。
DOMAINS: list[tuple[str, str]] = [
    ("support.my.uq.edu.au", "P0"),
    ("my.uq.edu.au", "P0"),
    ("study.uq.edu.au", "P1"),
]

# 推迟到阶段四(Playwright)处理,不在阶段一发现:
#   - policies.uq.edu.au(P1,原 ppl.app.uq.edu.au 整域 301 迁来):JS 渲染 SPA,无 XML sitemap
#   - library.uq.edu.au(P2):同为 JS SPA,首页仅 1 个 <a>,无 sitemap
#   - graduate-school.uq.edu.au(P2):DNS 已不解析,域名下线

# robots Disallow 覆盖白名单:已获用户明确授权对其抓取的 UQ 站点。
# support.my.uq.edu.au 的 robots 对非主流搜索引擎 UA 整站 Disallow(Oracle Service Cloud
# 按浏览量计费所致),但它是 P0 核心 FAQ/KB,经授权纳入。
ROBOTS_OVERRIDE = {"support.my.uq.edu.au"}

# 路径段黑名单(所有域名通用,plan 第 0 节):任一路径段命中即排除。
# 按段精确匹配,不会误伤 /about-us、/news-feed 这类(段名是 about-us / news-feed)。
EXCLUDE_SEG = (
    "news", "events", "event", "contact", "about",
    "staff", "people", "profile", "media", "login", "search",
)

# 域名专属排除(同样按段匹配):study 的 study-options 是 program/course 详情页
# (DB 已有,plan 命令排除),stories 是营销文。
PER_DOMAIN_EXCLUDE: dict[str, tuple[str, ...]] = {
    "study.uq.edu.au": ("study-options", "stories"),
}

# guessed_type 按域名归类
FAQ_DOMAINS = {"support.my.uq.edu.au"}
POLICY_DOMAINS = {"policies.uq.edu.au"}


def _get(url: str, retries: int = 3, timeout: int = 30) -> requests.Response | None:
    """带重试的 GET;最终失败返回 None(由调用方计入跳过/失败,不静默吞)。"""
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
    """取路径首段做粗分组,如 /managing-my-program/...-> '/managing-my-program/'。"""
    segs = [s for s in urlparse(url).path.split("/") if s]
    return f"/{segs[0]}/" if segs else "/"


def _excluded(url: str, domain: str) -> bool:
    segs = {s.lower() for s in urlparse(url).path.split("/") if s}
    blocked = set(EXCLUDE_SEG) | set(PER_DOMAIN_EXCLUDE.get(domain, ()))
    return bool(segs & blocked)


def load_robots(domain: str) -> RobotFileParser | None:
    """读 robots.txt 解析成 RobotFileParser;取不到返回 None。"""
    resp = _get(f"https://{domain}/robots.txt")
    if resp is None or resp.status_code == 404:
        return None
    rp = RobotFileParser()
    rp.parse(resp.text.splitlines())
    return rp


def _xml_root(resp: requests.Response):
    """sitemap 响应 -> lxml root;支持 .gz。解析失败返回 None。"""
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
    """递归展开 sitemap / sitemapindex,返回 [(url, lastmod), ...]。"""
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
    """同域 BFS 降级:无 sitemap 时用。lastmod 留空。"""
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
    """单域发现。返回 (records, stats)。records 已过滤。"""
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
