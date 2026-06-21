"""
guide_check.py — 攻略入库前置对账闸门(确定性,无 LLM)

把一篇 `<code>_<year>.md` 的 frontmatter.claims 逐项比对 course_detail(事实权威源):
先修 prereq、各考核占比 weight(多重集)、Hurdle 标注。任一不一致 -> 打印冲突表 + 非零退出
(当入库闸门,规则 19:不静默放过)。比的是**值**不是「字段存不存在」(规则 16):权重数值相等、
prereq 归一后字符串相等、hurdle 集合相等;弱比对等于没闸门。

正文里疑似裸日期(「N 月」且附近无年份)只 warn,逼作者标年(不拦)。

用法(从 backend/ 跑,需本地 :5433):
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

# 攻略文件名约定:<CODE>_<YEAR>.md;glob 展开时据此过滤掉 README.md 等非攻略文件
_GUIDE_FILE_RE = re.compile(r"^[A-Za-z]{4}\d{4}_\d{4}\.md$")

# frontmatter 分隔:文件以 `---\n<yaml>\n---\n<body>` 开头
_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)
# 正文疑似具体日期:N月(中文),用于催作者标年;附近 6 字符内有 4 位年份则放过
_BARE_DATE_RE = re.compile(r"\d{1,2}\s*月")
_YEAR_NEAR_RE = re.compile(r"\d{4}")


def parse_guide(path: str) -> tuple[dict, str]:
    """读一篇攻略 md -> (frontmatter dict, body)。无合法 frontmatter 直接抛错(入库闸门不容忍格式错)。"""
    raw = open(path, encoding="utf-8").read()
    m = _FRONT_RE.match(raw)
    if not m:
        raise ValueError(f"{path}: 缺少 frontmatter(必须以 --- 包裹的 YAML 开头)")
    fm = yaml.safe_load(m.group(1)) or {}
    if not isinstance(fm, dict):
        raise ValueError(f"{path}: frontmatter 不是合法 YAML 映射")
    return fm, m.group(2)


def _norm_prereq(s) -> str:
    """先修归一:大写 + 折叠空白 + 去首尾标点。归一后做字符串相等(规则 16:比值不比字段存在)。"""
    return re.sub(r"\s+", " ", str(s or "").strip().upper()).strip(" 。.;;")


def _weights(items) -> list[int]:
    """考核项 -> 权重多重集(四舍五入取整,升序)。claim 用 item['weight'],course 用 a['weight']。"""
    out: list[int] = []
    for a in items or []:
        if not isinstance(a, dict):
            continue
        w = a.get("weight")
        if w is not None:
            out.append(round(float(w)))
    return sorted(out)


def _hurdle_weights(items) -> list[int]:
    """被标 hurdle 的考核项的权重集合(升序)。漏标 hurdle 会让两侧集合不等 -> 报冲突。"""
    out: list[int] = []
    for a in items or []:
        if isinstance(a, dict) and a.get("hurdle"):
            w = a.get("weight")
            out.append(round(float(w)) if w is not None else -1)
    return sorted(out)


def check_claims(claims: dict, course: dict) -> list[str]:
    """逐项比 claims vs course_detail,返回冲突说明列表(空列表 = 全过)。"""
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
    """正文里疑似裸日期(N月 且附近无年份),只告警不拦,催作者把日期标年(Risk 2)。"""
    warns: list[str] = []
    for m in _BARE_DATE_RE.finditer(body):
        window = body[max(0, m.start() - 16): m.end() + 16]
        if not _YEAR_NEAR_RE.search(window):
            warns.append(window.strip())
    return warns


def check_file(conn, path: str) -> dict:
    """单篇对账,返回 {code, ok, conflicts, warnings, frontmatter, body}。course 缺失即视为冲突。"""
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
    """展开路径:glob 模式只保留符合 <CODE>_<YEAR>.md 的攻略文件(滤掉 README 等);显式路径原样保留(便于报错)。"""
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
