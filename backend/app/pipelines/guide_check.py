"""
guide_check.py — pre-load reconciliation gate for guides (deterministic, no LLM)

Compare every claim in a `<code>_<year>.md` frontmatter.claims against course_detail (the
authoritative fact source): prereq, each assessment weight (multiset), and Hurdle marks. Any
mismatch -> print a conflict table + non-zero exit (acts as a load gate, rule 19: do not pass
silently). It checks the **value**, not "does the field exist" (rule 16): weight numbers must be
equal, prereq strings must be equal after normalisation, and hurdle sets must be equal; a weak
check is no gate at all.

A suspected bare date in the body ("month N" with no year nearby) only warns, pushing the author
to add the year (it does not block).

Usage (run from backend/, needs local :5433):
    python -m app.pipelines.guide_check data/guides/INFS7410_2025.md
    python -m app.pipelines.guide_check data/guides/COMP4500_2025.md data/guides/COMP7500_2025.md
"""
from __future__ import annotations
import re
import sys
import glob

import os
import yaml
import psycopg

from app.services import retrieval
from app.core.config import DSN

# Guide file name convention: <CODE>_<YEAR>.md; used during glob expansion to filter out non-guide files like README.md
_GUIDE_FILE_RE = re.compile(r"^[A-Za-z]{4}\d{4}_\d{4}\.md$")

# frontmatter separator: the file starts with `---\n<yaml>\n---\n<body>`
_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)
# Suspected concrete date in the body: "month N" (Chinese), used to push the author to add the year; passes if a 4-digit year is within 6 chars nearby
_BARE_DATE_RE = re.compile(r"\d{1,2}\s*月")
_YEAR_NEAR_RE = re.compile(r"\d{4}")


def parse_guide(path: str) -> tuple[dict, str]:
    """Read one guide md -> (frontmatter dict, body). Raise if there is no valid frontmatter (the load gate does not tolerate format errors)."""
    raw = open(path, encoding="utf-8").read()
    m = _FRONT_RE.match(raw)
    if not m:
        raise ValueError(f"{path}: 缺少 frontmatter(必须以 --- 包裹的 YAML 开头)")
    fm = yaml.safe_load(m.group(1)) or {}
    if not isinstance(fm, dict):
        raise ValueError(f"{path}: frontmatter 不是合法 YAML 映射")
    return fm, m.group(2)


def _norm_prereq(s) -> str:
    """Normalise prereq: uppercase + collapse whitespace + strip leading/trailing punctuation. After this, compare strings for equality (rule 16: compare values, not field existence)."""
    return re.sub(r"\s+", " ", str(s or "").strip().upper()).strip(" 。.;;")


def _weights(items) -> list[int]:
    """Assessment items -> weight multiset (rounded to integers, ascending). claim uses item['weight'], course uses a['weight']."""
    out: list[int] = []
    for a in items or []:
        if not isinstance(a, dict):
            continue
        w = a.get("weight")
        if w is not None:
            out.append(round(float(w)))
    return sorted(out)


def _hurdle_weights(items) -> list[int]:
    """Weight set of the assessment items marked as hurdle (ascending). Missing a hurdle mark makes the two sets unequal -> report a conflict."""
    out: list[int] = []
    for a in items or []:
        if isinstance(a, dict) and a.get("hurdle"):
            w = a.get("weight")
            out.append(round(float(w)) if w is not None else -1)
    return sorted(out)


def check_claims(claims: dict, course: dict) -> list[str]:
    """Compare claims against course_detail item by item, return a list of conflict notes (empty list = all pass)."""
    conflicts: list[str] = []
    claims = claims or {}

    cl_pre = _norm_prereq(claims.get("prereq"))
    db_pre = _norm_prereq(course.get("prerequisite_raw"))
    if cl_pre != db_pre:
        conflicts.append(f"先修不一致:攻略 claims={cl_pre!r} ≠ 官方 prerequisite_raw={db_pre!r}")

    cl_items = claims.get("assessment") or []
    db_items = course.get("assessments") or []
    cl_w, db_w = _weights(cl_items), _weights(db_items)
    if cl_w != db_w:
        conflicts.append(f"考核占比不一致:攻略权重={cl_w} ≠ 官方权重={db_w}")

    cl_h, db_h = _hurdle_weights(cl_items), _hurdle_weights(db_items)
    if cl_h != db_h:
        conflicts.append(
            f"Hurdle 标注不一致:攻略 hurdle 项权重={cl_h} ≠ 官方 hurdle 项权重={db_h}"
            "(漏标或多标 hurdle)")
    return conflicts


def _suspect_dates(body: str) -> list[str]:
    """Suspected bare date in the body ("month N" with no year nearby), only warn without blocking, pushing the author to add the year to the date (Risk 2)."""
    warns: list[str] = []
    for m in _BARE_DATE_RE.finditer(body):
        window = body[max(0, m.start() - 16): m.end() + 16]
        if not _YEAR_NEAR_RE.search(window):
            warns.append(window.strip())
    return warns


def check_file(conn, path: str) -> dict:
    """Reconcile a single file, return {code, ok, conflicts, warnings, frontmatter, body}. A missing course counts as a conflict."""
    fm, body = parse_guide(path)
    code = str(fm.get("course_code") or "").strip().upper()
    if not code:
        return {"code": "", "ok": False, "conflicts": ["frontmatter 缺 course_code"],
                "warnings": [], "frontmatter": fm, "body": body}
    course = retrieval.course_detail(conn, code)
    if not course:
        return {"code": code, "ok": False,
                "conflicts": [f"course_detail 未找到 {code}(无法对账,拒绝入库)"],
                "warnings": [], "frontmatter": fm, "body": body}
    conflicts = check_claims(fm.get("claims") or {}, course)
    return {"code": code, "ok": not conflicts, "conflicts": conflicts,
            "warnings": _suspect_dates(body), "frontmatter": fm, "body": body}


def _expand(paths: list[str]) -> list[str]:
    """Expand paths: glob patterns keep only guide files matching <CODE>_<YEAR>.md (filter out README etc.); explicit paths are kept as-is (so errors can be reported)."""
    out: list[str] = []
    for p in paths:
        if any(c in p for c in "*?["):
            out.extend(g for g in sorted(glob.glob(p)) if _GUIDE_FILE_RE.match(os.path.basename(g)))
        else:
            out.append(p)
    return out


def main() -> int:
    paths = _expand(sys.argv[1:])
    if not paths:
        print("用法:python -m app.pipelines.guide_check data/guides/<code>_<year>.md ...")
        return 2
    failed = skipped = 0
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        for path in paths:
            if not _GUIDE_FILE_RE.match(os.path.basename(path)):
                print(f"[SKIP] {path}  (非 <CODE>_<YEAR>.md 攻略文件)")
                skipped += 1
                continue
            res = check_file(conn, path)
            tag = "OK " if res["ok"] else "FAIL"
            print(f"[{tag}] {path}  ({res['code']})")
            for c in res["conflicts"]:
                print(f"    ✗ {c}")
            for w in res["warnings"]:
                print(f"    ⚠ 疑似裸日期,建议标年:…{w}…")
            if not res["ok"]:
                failed += 1
    checked = len(paths) - skipped
    print(f"\n对账完成:{checked} 篇,通过 {checked - failed},冲突 {failed}"
          + (f",跳过 {skipped}" if skipped else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
