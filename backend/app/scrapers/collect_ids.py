"""
collect_ids.py — 阶段二:某学期全部课程的 offering id 清单采集
(对应 plan.md 第 6 节 / Roadmap 阶段二)

流程:
  1. programs-courses 搜索页 -> 该学期所有课程码(静态 HTML)
  2. 逐门 course.html -> 解析 "Course offerings" 表,挑出目标学期的行,
     取 course-profiles 的 offering id(形如 CSSE1001-21206-7620)
  3. 输出 offering id 清单,直接喂给 scraper.py --file

用法:
    python collect_ids.py --semester 2026:1 --out course_ids.txt
    python collect_ids.py --semester 2026:1 --limit 12        # 抽样测试
"""
from __future__ import annotations
import re
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

SEARCH = ("https://programs-courses.uq.edu.au/search.html"
          "?searchType=course&keywords=&CourseParameters%5Bsemester%5D={sem}&year={year}")
COURSE = "https://programs-courses.uq.edu.au/course.html?course_code={code}"
HEADERS = {"User-Agent": "Mozilla/5.0 (course-kb-scraper)"}


def _get(url: str, retries: int = 3) -> str:
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))


def _target_period(semester: str) -> tuple[str, re.Pattern]:
    """'2026:1' -> ('Semester 1, 2026', 匹配开课表 col0 的正则)"""
    year, sem = semester.split(":")
    if sem == "3":
        return f"Summer Semester, {year}", re.compile(rf"Summer\s+Semester,\s*{year}", re.I)
    return f"Semester {sem}, {year}", re.compile(rf"Semester\s*{sem},\s*{year}", re.I)


def list_course_codes(semester: str) -> list[str]:
    """搜索页 -> 该学期去重后的课程码列表"""
    year = semester.split(":")[0]
    soup = BeautifulSoup(_get(SEARCH.format(sem=semester, year=year)), "html.parser")
    codes = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"course_code=([A-Z]{3,4}\d{4}[A-Z]?)&offer=", a["href"])
        if m:
            codes.add(m.group(1))
    return sorted(codes)


def offerings_for(code: str, pat: re.Pattern, location: str, mode: str) -> list[dict]:
    """该课匹配 目标学期 + 校区 + 授课模式 的开课列表(排除 archive 旧页)"""
    soup = BeautifulSoup(_get(COURSE.format(code=code)), "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        if "course-profiles.uq.edu.au/course-profiles/" not in a["href"]:
            continue
        row = a.find_parent("tr")
        if not row:
            continue
        cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True))
                 for td in row.find_all(["td", "th"])]
        if len(cells) < 3 or not pat.search(cells[0]):
            continue
        if location and cells[1].strip().lower() != location.lower():
            continue
        if mode and cells[2].strip().lower() != mode.lower():
            continue
        out.append({
            "offering_id": a["href"].rstrip("/").split("/")[-1],
            "location": cells[1],
            "mode": cells[2],
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--semester", default="2026:1", help="如 2026:1 / 2026:2 / 2026:3(summer)")
    ap.add_argument("--location", default="St Lucia", help="校区精确匹配,空串=不限")
    ap.add_argument("--mode", default="In Person", help="授课模式精确匹配,空串=不限")
    ap.add_argument("--out", default="data/course_ids.txt", help="offering id 输出清单")
    ap.add_argument("--workers", type=int, default=6, help="并发数")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 门(抽样测试)")
    args = ap.parse_args()

    label, pat = _target_period(args.semester)
    filt = " / ".join(filter(None, [label, args.location, args.mode]))
    print(f"过滤条件: {filt}")

    codes = list_course_codes(args.semester)
    if args.limit:
        codes = codes[:args.limit]
    print(f"课程码: {len(codes)} 门,开始抓 course.html 取 offering id ...")

    results: dict[str, list[dict]] = {}
    no_offering: list[str] = []      # 搜到课但目标学期没有已发布的 course-profiles
    failures: list[tuple[str, str]] = []   # 请求/解析异常
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(offerings_for, c, pat, args.location, args.mode): c for c in codes}
        for fut in as_completed(futs):
            code = futs[fut]
            done += 1
            try:
                offs = fut.result()
                results[code] = offs
                if not offs:
                    no_offering.append(code)
            except Exception as e:
                failures.append((code, str(e)))
            if done % 50 == 0 or done == len(codes):
                print(f"  {done}/{len(codes)}")

    # 去重(按 offering_id),并统计多开课的课程
    all_ids: list[str] = []
    seen: set[str] = set()
    multi = 0
    for offs in results.values():
        if len(offs) > 1:
            multi += 1
        for o in offs:
            if o["offering_id"] not in seen:
                seen.add(o["offering_id"])
                all_ids.append(o["offering_id"])

    with open(args.out, "w", encoding="utf-8") as f:
        for oid in all_ids:
            f.write(oid + "\n")

    print(f"\n写入 {args.out}: {len(all_ids)} 个 offering id")
    print(f"  课程码 {len(codes)} 门 | 命中 >1 offering {multi} 门 "
          f"| 无匹配开课 {len(no_offering)} 门 | 异常 {len(failures)} 门")
    if no_offering:
        print(f"  [无匹配开课] {', '.join(no_offering[:30])}"
              + (" ..." if len(no_offering) > 30 else ""))
    if failures:
        print("  [异常] 前 20:")
        for code, why in failures[:20]:
            print(f"    {code}: {why}")


if __name__ == "__main__":
    main()
