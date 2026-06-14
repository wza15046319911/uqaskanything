"""
program_scraper.py — 阶段六:抓 UQ program 的【完整规则树】+ 递归展开 plan 分支
(对应 plan.md 第 6 节 program 维度,支持选课模拟)

两层 + 递归:
  1. program 搜索页 -> 各 program 的 acad_prog id + 名称
  2. program_list.html?acad_prog=X -> window.AppData(JSON)规则树
  3. 规则里的 plan item(major/minor/...)递归抓 plan_display.html?acad_plan=CODE
     (plan 页同款 AppData 结构,解析逻辑复用)。带缓存 + 防环 + 深度限制。

每个课程组 = {header, body} 节点:
  - header.selectionRule.text/params -> 规则句 + min/max 学分(N=min, M=max)
  - header.partReference (A / C.1) -> 层级路径
  - body: CurriculumReference(课程或 plan 引用) / EquivalenceGroup / WildCardItem

输出 programs.jsonl,每行一个 program:
  {program_id, title, total_units, rules:[
     {ref, title, part_type, rule_text, units_min, units_max, select_type,
      items:[ {kind:course,code,name,units}
            | {kind:equivalence,options:[...]}
            | {kind:wildcard,org_code,...}
            | {kind:plan,code,name,subtype,units_min,units_max, rules:[...递归展开...]} ]} ]}

用法:
    python program_scraper.py --acad-prog 2559           # 单个测试(含 plan 展开)
    python program_scraper.py --faculty eait --limit 10  # 10 个 EAIT program
    python program_scraper.py --acad-prog 2559 --no-expand   # 不展开 plan
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
CODE = re.compile(r"^[A-Z]{4}\d{4}$")          # 真课程码(排除 6 字母的 plan 引用)
DELAY = 1.0                                     # plan 抓取前的礼貌间隔(main 按 --delay 覆盖)
_plan_cache: dict[str, list] = {}              # plan code -> 已展开规则(整轮共享,避免重复抓)


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
            elif code:                          # plan 引用(major/minor/...);rules 由展开阶段补
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
    """auxiliaryRules 里的 level 约束 -> [{kind:level_min|level_max, units, level, or_higher, text}]。
    只取「at least / at most [N] units at level [LEVEL]」这类(供模拟器按 level 校验下限/上限);
    其余(无条件 exclude 等)跳过——那些走 parse_aux_rules 入 programs.aux_rules。"""
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
    """plan 页里 plan 级 level 约束:挂在无 partReference 的容器 header 上(如 SOFTWX5528 的
    「at least 8 units at level 7」)。group 级约束(挂带 partReference 的 group header,如 D 的
    「at most 4 units at level 4」)由 _rule() 收到对应规则,不在此重复。"""
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
    for k, v in pmap.items():                   # 填 [N]/[M]/[PLANTYPE] 占位符
        rule_text = rule_text.replace(f"[{k}]", str(v))
    select_type = "all" if "ALL of the following" in (text or "") else "select"
    # SubRule 父节点(如 2559 的 C)的 min/max 不在 selectionRule 里,而在 header.unitsMin/Max
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
    aux = _level_aux(header.get("auxiliaryRules"))   # group 级 level 约束(如 D 的 ≤4@L4)
    if aux:
        r["aux_rules"] = aux
    return r


def parse_rules(data) -> list[dict]:
    """从 AppData 抽规则树(program 与 plan 同款结构)"""
    rules: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            header, body = node.get("header"), node.get("body")
            if isinstance(header, dict) and isinstance(body, list):
                r = _rule(header, body)
                # ref 部件 items 空也保留:SubRule 父(带 rule_logic/children_refs,
                # 如 2559 的 C「No Major Option」)和空表选择组(如 E,语义=程序课表内任选)
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
    """就地把每个 plan item 的 rules 填上(递归展开 major/minor/...);plan 级 level 约束挂 aux_rules。"""
    for r in rules:
        for it in r["items"]:
            if it.get("kind") == "plan" and it.get("code"):
                sub_rules, plan_aux = fetch_plan_rules(it["code"], year, seen, depth)
                it["rules"] = sub_rules
                if plan_aux:
                    it["aux_rules"] = plan_aux


def fetch_plan_rules(code: str, year: str, seen: set, depth: int) -> tuple[list, list]:
    """返回 (plan 子规则, plan 级 level 约束)。"""
    if code in _plan_cache:
        return _plan_cache[code]
    if depth <= 0 or code in seen:              # 深度耗尽或成环 -> 不再展开
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
    """递归收集 AppData 里所有 auxiliaryRules 节点。"""
    if isinstance(o, dict):
        if isinstance(o.get("auxiliaryRules"), list):
            out.extend(o["auxiliaryRules"])
        for v in o.values():
            _collect_aux(v, out)
    elif isinstance(o, list):
        for v in o:
            _collect_aux(v, out)


def _render_param(val) -> tuple[str, list]:
    """param.value -> (显示串, 课码list)。课用 code,plan 用 名+subtype,其余尽量取 name;
    无法渲染的 dict 返回空串,避免把原始 JSON 塞进文本。"""
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
    """从 AppData 抽程序级附加规则(auxiliaryRules)。返回 [{type, text, exclude_codes}]。
    type='exclude' = 无条件"No credit will be given for [课]",其 exclude_codes 即程序级禁课
    (如 BCompSc 禁 MATH1040);其余类型(条件禁课/level 上限/plan 冲突…)只存文本备查。"""
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
    """程序级布尔公式(如 "Part A AND ( Part B OR Part C ) AND ...")。
    取无 partReference 节点上的 ruleLogic(有 partReference 的是 SubRule 内部公式);
    多 component 时各自括号后 AND 连接。"""
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
    """递归统计 (课程数, plan数)"""
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

    no_rules: list[str] = []        # 无课表(研究型学位 / 已归档旧版,非错误)
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
