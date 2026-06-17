"""
program_scraper.py — phase six: fetch a UQ program's [full rule tree] + recursively expand plan branches
(matches plan.md section 6 program dimension, supports course planning simulation)

Two layers + recursion:
  1. program search page -> each program's acad_prog id + name
  2. program_list.html?acad_prog=X -> window.AppData (JSON) rule tree
  3. plan items in the rules (major/minor/...) recursively fetch plan_display.html?acad_plan=CODE
     (plan page has the same AppData structure, parse logic reused). with cache + cycle guard + depth limit.

Each course group = {header, body} node:
  - header.selectionRule.text/params -> rule sentence + min/max units (N=min, M=max)
  - header.partReference (A / C.1) -> hierarchy path
  - body: CurriculumReference (course or plan reference) / EquivalenceGroup / WildCardItem

Output programs.jsonl, one program per line:
  {program_id, title, total_units, rules:[
     {ref, title, part_type, rule_text, units_min, units_max, select_type,
      items:[ {kind:course,code,name,units}
            | {kind:equivalence,options:[...]}
            | {kind:wildcard,org_code,...}
            | {kind:plan,code,name,subtype,units_min,units_max, rules:[...recursively expanded...]} ]} ]}

Usage:
    python program_scraper.py --acad-prog 2559           # single test (with plan expand)
    python program_scraper.py --faculty eait --limit 10  # 10 EAIT programs
    python program_scraper.py --acad-prog 2559 --no-expand   # do not expand plan
"""
from __future__ import annotations
import re
import json
import time
import argparse

import requests
from bs4 import BeautifulSoup

SEARCH = ("https://programs-courses.uq.edu.au/search.html?keywords=&searchType=program"
          "{archived}&CourseParameters%5Bsemester%5D=2026%3A1&year=2026&faculty={faculty}")
LIST = "https://programs-courses.uq.edu.au/program_list.html?acad_prog={pid}&year={year}"
PLAN = "https://programs-courses.uq.edu.au/plan_display.html?acad_plan={code}&year={year}"
HEADERS = {"User-Agent": "Mozilla/5.0 (course-kb-scraper)"}
CODE = re.compile(r"^[A-Z]{4}\d{4}$")          # real course code (exclude 6-letter plan references)
DELAY = 1.0                                     # polite interval before fetching a plan (main overrides via --delay)
_plan_cache: dict[str, list] = {}              # plan code -> expanded rules (shared across the whole run, avoid refetch)


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


def _txt(s) -> str:
    return re.sub(r"\s+", " ", s).strip() if s else ""


def list_programs(search_url: str) -> dict[str, str]:
    soup = BeautifulSoup(_get(search_url), "html.parser")
    progs: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"program\.html\?acad_prog=(\d+)", a["href"])
        if m and m.group(1) not in progs:
            title = a.get_text(strip=True)
            if title:
                progs[m.group(1)] = title
    return progs


def _appdata(html: str):
    m = re.search(r"window\.\w+\s*=\s*", html)
    if not m:
        return None
    return json.JSONDecoder().raw_decode(html, html.index("{", m.end()))[0]


def _course(cr: dict) -> dict:
    return {"kind": "course", "code": cr.get("code"), "name": _txt(cr.get("name")),
            "units": cr.get("unitsMaximum")}


def _items(body: list) -> list[dict]:
    out = []
    for it in body:
        if not isinstance(it, dict):
            continue
        rt = it.get("rowType")
        if rt == "CurriculumReference":
            cr = it.get("curriculumReference") or {}
            code = cr.get("code") or ""
            if CODE.match(code):
                out.append(_course(cr))
            elif code:                          # plan reference (major/minor/...); rules filled by the expand stage
                out.append({"kind": "plan", "code": code, "name": _txt(cr.get("name")),
                            "subtype": cr.get("subtype"), "units_min": cr.get("unitsMinimum"),
                            "units_max": cr.get("unitsMaximum"), "rules": None})
        elif rt == "EquivalenceGroup":
            opts = [_course(e["curriculumReference"]) for e in it.get("equivalenceGroup", [])
                    if isinstance(e, dict) and (e.get("curriculumReference") or {}).get("code")]
            if opts:
                out.append({"kind": "equivalence", "options": opts})
        elif rt == "WildCardItem":
            w = it.get("wildCardItem") or {}
            out.append({"kind": "wildcard", "org_code": w.get("orgCode"),
                        "org_name": _txt(w.get("orgName")),
                        "include_child_orgs": w.get("includeChildOrgs")})
    return out


def _level_aux(aux_list) -> list[dict]:
    """level constraints inside auxiliaryRules -> [{kind:level_min|level_max, units, level, or_higher, text}].
    Take only "at least / at most [N] units at level [LEVEL]" types (so the simulator can check the lower/upper bound by level);
    skip the rest (unconditional exclude, etc.) -- those go through parse_aux_rules into programs.aux_rules."""
    out = []
    for a in aux_list or []:
        if not isinstance(a, dict):
            continue
        text = a.get("text") or ""
        low = text.lower()
        pmap = {p.get("name"): p.get("value") for p in a.get("params", []) if isinstance(p, dict)}
        n, lvl = pmap.get("N"), pmap.get("LEVEL")
        if n is None or lvl is None:
            continue
        if "at least" in low:
            kind = "level_min"
        elif "at most" in low:
            kind = "level_max"
        else:
            continue
        try:
            units, level = float(n), int(lvl)
        except (TypeError, ValueError):
            continue
        or_higher = str(pmap.get("OR_HIGHER", "")).strip().lower() in ("yes", "true", "y")
        rendered = text
        for k, v in pmap.items():
            sub = (" or higher" if or_higher else "") if k == "OR_HIGHER" else str(v)
            rendered = rendered.replace(f"[{k}]", sub)
        out.append({"kind": kind, "units": units, "level": level,
                    "or_higher": or_higher, "text": " ".join(rendered.split())})
    return out


def plan_container_aux(data) -> list[dict]:
    """plan-level level constraints on a plan page: attached to the container header with no partReference (like SOFTWX5528's
    "at least 8 units at level 7"). group-level constraints (attached to a group header with partReference, like D's
    "at most 4 units at level 4") are picked up by _rule() with their rule, not repeated here."""
    raw: list = []

    def walk(o):
        if isinstance(o, dict):
            h = o.get("header")
            if isinstance(h, dict) and not h.get("partReference") and h.get("auxiliaryRules"):
                raw.extend(h["auxiliaryRules"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk((data or {}).get("programRequirements") or {})
    return _level_aux(raw)


def _rule(header: dict, body: list) -> dict:
    sr = header.get("selectionRule") or {}
    text = _txt(sr.get("text") or header.get("text"))
    params = sr.get("params") or header.get("params") or []
    pmap = {p.get("name"): p.get("value") for p in params if isinstance(p, dict)}
    rule_text = text
    for k, v in pmap.items():                   # fill the [N]/[M]/[PLANTYPE] placeholders
        rule_text = rule_text.replace(f"[{k}]", str(v))
    select_type = "all" if "ALL of the following" in (text or "") else "select"
    # the SubRule parent node (like 2559's C) min/max is not in selectionRule, but in header.unitsMin/Max
    r = {"ref": header.get("partReference"), "title": _txt(header.get("title")),
         "part_type": header.get("partType"), "rule_text": rule_text,
         "units_min": pmap["N"] if "N" in pmap else header.get("unitsMin", header.get("unitsMinimum")),
         "units_max": pmap["M"] if "M" in pmap else header.get("unitsMax", header.get("unitsMaximum")),
         "select_type": select_type, "items": _items(body)}
    if header.get("ruleLogic"):
        r["rule_logic"] = _txt(header["ruleLogic"])
    kids = [b["header"].get("partReference") for b in body
            if isinstance(b, dict) and isinstance(b.get("header"), dict)
            and b["header"].get("partReference")]
    if kids:
        r["children_refs"] = kids
    notes = header.get("notes")
    if notes and isinstance(notes, str):
        nt = _txt(BeautifulSoup(notes, "html.parser").get_text(" "))
        if nt:
            r["notes"] = nt
    aux = _level_aux(header.get("auxiliaryRules"))   # group-level level constraints (like D's ≤4@L4)
    if aux:
        r["aux_rules"] = aux
    return r


def parse_rules(data) -> list[dict]:
    """extract the rule tree from AppData (program and plan share the same structure)"""
    rules: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            header, body = node.get("header"), node.get("body")
            if isinstance(header, dict) and isinstance(body, list):
                r = _rule(header, body)
                # keep a ref part even when items is empty: SubRule parent (with rule_logic/children_refs,
                # like 2559's C "No Major Option") and empty-list select groups (like E, meaning = pick any in the program list)
                if r["items"] or (r.get("ref") and (r.get("rule_logic")
                                                    or header.get("selectionRule"))):
                    rules.append(r)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return rules


def expand_plans(rules: list, year: str, seen: set, depth: int):
    """fill each plan item's rules in place (recursively expand major/minor/...); plan-level level constraints attach to aux_rules."""
    for r in rules:
        for it in r["items"]:
            if it.get("kind") == "plan" and it.get("code"):
                sub_rules, plan_aux = fetch_plan_rules(it["code"], year, seen, depth)
                it["rules"] = sub_rules
                if plan_aux:
                    it["aux_rules"] = plan_aux


def fetch_plan_rules(code: str, year: str, seen: set, depth: int) -> tuple[list, list]:
    """return (plan sub-rules, plan-level level constraints)."""
    if code in _plan_cache:
        return _plan_cache[code]
    if depth <= 0 or code in seen:              # depth exhausted or cycle -> stop expanding
        return [], []
    time.sleep(DELAY)
    try:
        data = _appdata(_get(PLAN.format(code=code, year=year)))
    except Exception:
        data = None
    rules = parse_rules(data) if data else []
    plan_aux = plan_container_aux(data) if data else []
    expand_plans(rules, year, seen | {code}, depth - 1)
    _plan_cache[code] = (rules, plan_aux)
    return rules, plan_aux


def _collect_aux(o, out: list):
    """recursively collect all auxiliaryRules nodes in AppData."""
    if isinstance(o, dict):
        if isinstance(o.get("auxiliaryRules"), list):
            out.extend(o["auxiliaryRules"])
        for v in o.values():
            _collect_aux(v, out)
    elif isinstance(o, list):
        for v in o:
            _collect_aux(v, out)


def _render_param(val) -> tuple[str, list]:
    """param.value -> (display string, course code list). course uses code, plan uses name+subtype, otherwise take name if possible;
    a dict that cannot be rendered returns empty string, to avoid putting raw JSON into the text."""
    codes: list = []

    def one(v):
        if isinstance(v, dict):
            if v.get("code"):
                codes.append(v["code"])
                return v["code"]
            if isinstance(v.get("plan"), dict):
                pl = v["plan"]
                return (_txt(pl.get("name")) + " " + (pl.get("subtype") or "")).strip()
            if v.get("name"):
                return _txt(v.get("name"))
            return ""
        return str(v)

    if isinstance(val, list):
        return "、".join(s for s in (one(v) for v in val) if s), codes
    return one(val), codes


def parse_aux_rules(data: dict) -> list[dict]:
    """extract program-level additional rules (auxiliaryRules) from AppData. return [{type, text, exclude_codes}].
    type='exclude' = an unconditional "No credit will be given for [course]", its exclude_codes are the program-level banned courses
    (like BCompSc bans MATH1040); other types (conditional ban / level cap / plan conflict ...) only keep the text for reference."""
    raw: list = []
    _collect_aux((data or {}).get("programRequirements") or {}, raw)
    out = []
    for rule in raw:
        text = rule.get("text") or ""
        all_codes: list = []
        for p in rule.get("params", []):
            disp, codes = _render_param(p.get("value"))
            all_codes += codes
            text = text.replace(f"[{p.get('name')}]", disp)
        low = text.lower()
        conditional = any(k in low for k in
                          ("completing", "for a student", "for students",
                           "can only be counted", "can not be undertaken"))
        if low.startswith("no credit will be given for") and not conditional:
            typ = "exclude"
        elif "no credit" in low:
            typ = "exclude_conditional"
        elif "at most" in low or "at least" in low:
            typ = "level_cap"
        elif "can not be undertaken" in low:
            typ = "plan_conflict"
        elif "can only be counted" in low:
            typ = "plan_scoped"
        elif "discipline" in low:
            typ = "discipline_cap"
        else:
            typ = "other"
        out.append({"type": typ, "text": " ".join(text.split()),
                    "exclude_codes": sorted(set(all_codes)) if typ == "exclude" else []})
    return out


def program_rule_logic(data) -> str | None:
    """program-level boolean formula (like "Part A AND ( Part B OR Part C ) AND ...").
    take ruleLogic on nodes with no partReference (those with partReference are SubRule internal formulas);
    with multiple components, wrap each in brackets and join with AND."""
    found: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            rl = o.get("ruleLogic")
            if rl and not o.get("partReference") and "Part" in rl:
                found.append(_txt(rl))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk((data or {}).get("programRequirements") or {})
    if not found:
        return None
    return " AND ".join(f"( {f} )" for f in found) if len(found) > 1 else found[0]


def parse_program(pid: str, year: str = "2026", expand: bool = True, max_depth: int = 3) -> dict | None:
    data = _appdata(_get(LIST.format(pid=pid, year=year)))
    if not data:
        return None
    pr = data.get("programRequirements") or {}
    rules = parse_rules(data)
    if expand:
        expand_plans(rules, year, set(), max_depth)
    return {"program_id": pid, "title": _txt(pr.get("name")) or _txt(data.get("title")),
            "total_units": pr.get("unitsMinimum"), "rules": rules,
            "rule_logic": program_rule_logic(data),
            "aux_rules": parse_aux_rules(data)}


def _count(rules: list) -> tuple[int, int]:
    """recursively count (course count, plan count)"""
    nc = npl = 0
    for r in rules:
        for it in r["items"]:
            if it["kind"] == "course":
                nc += 1
            elif it["kind"] == "plan":
                npl += 1
                c2, p2 = _count(it.get("rules") or [])
                nc += c2
                npl += p2
    return nc, npl


def main():
    global DELAY
    ap = argparse.ArgumentParser()
    ap.add_argument("--faculty", default="eait")
    ap.add_argument("--faculties", help="逗号分隔多个学院代码(eait,hss,hmbs,bel,sci),合并去重一次抓,共享 plan 缓存")
    ap.add_argument("--search-url")
    ap.add_argument("--acad-prog", help="只抓单个 program(测试用)")
    ap.add_argument("--year", default="2026")
    ap.add_argument("--no-expand", action="store_true", help="不递归展开 plan 分支")
    ap.add_argument("--max-depth", type=int, default=3, help="plan 展开最大深度")
    ap.add_argument("--archived", action="store_true", help="包含已归档 program(默认只当前有效)")
    ap.add_argument("--out", default="data/programs.jsonl")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    DELAY = args.delay

    archived = "&archived=true" if args.archived else ""
    if args.acad_prog:
        progs = {args.acad_prog: None}
    elif args.faculties:
        progs = {}
        for fac in args.faculties.split(","):
            progs.update(list_programs(SEARCH.format(faculty=fac.strip(), archived=archived)))
    else:
        progs = list_programs(args.search_url or SEARCH.format(faculty=args.faculty, archived=archived))
    pids = list(progs)
    if args.limit:
        pids = pids[:args.limit]
    print(f"program 数: {len(pids)} | 展开 plan: {not args.no_expand} (深度 {args.max_depth})")

    no_rules: list[str] = []        # no course list (research degree / archived old version, not an error)
    fails: list[tuple[str, str]] = []
    with open(args.out, "w", encoding="utf-8") as f:
        for pid in pids:
            try:
                rec = parse_program(pid, args.year, expand=not args.no_expand,
                                    max_depth=args.max_depth)
                if not rec or not rec["rules"]:
                    no_rules.append(pid)
                    continue
                if not rec["title"]:
                    rec["title"] = progs.get(pid)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                nc, npl = _count(rec["rules"])
                print(f"[ok] {pid} {rec['title']!r}: {len(rec['rules'])}规则/{nc}课/"
                      f"{npl}个plan (总学分={rec['total_units']})")
            except Exception as e:
                fails.append((pid, str(e)))
            time.sleep(args.delay)
    print(f"\n写入 {args.out}: {len(pids) - len(no_rules) - len(fails)} 个有课表 "
          f"| 无课表(研究型/归档){len(no_rules)} | 异常 {len(fails)}")
    if fails:
        print(f"  异常明细: {fails[:10]}")


if __name__ == "__main__":
    main()
