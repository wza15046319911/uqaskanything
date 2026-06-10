"""
simulator.py — 阶段七:选课模拟器(确定性状态机)

从 programs.rules 规则树驱动:用户增删已选课、选定某 major/minor 分支后,
每条顶层规则的进度(status)与「还能选什么」(available)随之实时变化。
先修课暂不考虑;所有进度判断都是纯确定性代码,不调用 LLM。

规则树节点(见库内 programs.rules):
  顶层规则 {ref,title,select_type:'all'|'select',units_min,units_max,items:[...]}
  item.kind:
    - course      {code,name,units}
    - equivalence {options:[{code,name,units},...]}   选其中任一即满足该「项」学分
    - plan        {code,name,subtype,units_min,units_max,rules:[...递归...]}  major/minor 分支
    - wildcard    任意课(不绑定具体码,available 不枚举它)

进度口径:
  - select_type='all' :必修组,units_required = units_min,凑够即 done。
  - select_type='select':选修组,units_required = units_min(可为 0/None=0),凑够即 done。
  - units_max(若有):选修组学分上限。超出 units_max 的学分不算有效进度,
    计入进度按 min(done_units, units_max) 封顶;done_units 原值仍透出,且打 over_max 标记。
  - equivalence 组算「一个可填项」:选了其中任一选项,只按那一门的 units 计一次(不重复累加)。
  - plan 规则:status 里列出可选 plan 分支。本模块按 **select-one(择一)** 语义实现:
    同一条 plan 规则内的多个分支互斥,再 choose_plan 会替换同规则内的旧选;
    规则必需学分 = 「修满 1 个分支」(已选分支的 units_min,未选则取各分支最小 units_min)。
    注:若某 program 的 plan 规则实为 select-many(可同时修多个 major),需另行确认后调整。
  - units 默认每门取 item.units,缺失则取 2。
  - 自引用:rules 树里偶尔嵌入「整学位」节点(code == program_id 或 subtype 含 'Program'),
    它不是可选 major/minor 分支,索引与递归一律跳过,避免无限递归与误把整学位当分支选。

用法:
    python simulator.py            # 用真实 DB 跑 program_id=2559 自测
"""
from __future__ import annotations
import os
import re

import psycopg

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:uqrag@localhost:5433/uq_courses")

DEFAULT_UNITS = 2.0


def _units(item: dict) -> float:
    """取某 item(course 或 equivalence option)的学分,缺失给默认值。"""
    u = item.get("units")
    return float(u) if u is not None else DEFAULT_UNITS


def satisfied(tree: dict | None, selected: set) -> tuple[bool, str | None]:
    """先修树是否被 selected 满足。返回 (ok, reason);reason 非空表示锁定或警告。

    软门口径:tree=None(无先修)-> 满足;op=raw(无法解析)-> 满足但带警告(绝不硬挡)。
    """
    if tree is None:
        return True, None
    op = tree.get("op")
    if op == "course":
        code = tree.get("code")
        return (code in selected, None if code in selected else f"缺先修 {code}")
    if op == "or":
        sub = [satisfied(c, selected) for c in tree.get("children", [])]
        if any(ok for ok, _ in sub):
            return True, None
        return False, " 或 ".join(r for _, r in sub if r)
    if op == "and":
        miss = [r for c in tree.get("children", []) for ok, r in [satisfied(c, selected)] if not ok]
        return (not miss, " 且 ".join(miss) if miss else None)
    if op == "raw":
        return True, f"先修无法解析:{tree.get('unparsed', '')}"
    return True, None


def parse_rule_logic(s: str | None) -> dict | None:
    """程序级布尔公式 -> 树。"Part A AND ( Part B OR Part C )" ->
    {"op":"and"|"or","children":[...]} | {"op":"part","ref":"A"}。
    任何无法识别的残留字符或归约失败 -> None(调用方回退 AND-all,绝不臆造结构)。
    优先级:AND 比 OR 紧,括号最高(UQ 公式实际都带括号)。"""
    if not s:
        return None
    s = re.sub(r"^\s*(AND|OR)\s+", "", s, flags=re.I)   # 容错:官方数据偶见开头多挂连接词
    toks = re.findall(r"\(|\)|\bAND\b|\bOR\b|Part\s+[\w.\-]+", s, re.I)
    toks = [t.upper() if t.upper() in ("AND", "OR") else t for t in toks]
    if re.sub(r"\(|\)|\bAND\b|\bOR\b|Part\s+[\w.\-]+|\s+", "", s, flags=re.I):
        return None                                  # 有 token 之外的残留,拒绝解析
    pos = 0

    def expr():                                      # OR 层
        nonlocal pos
        node = term()
        if node is None:
            return None
        children = [node]
        while pos < len(toks) and toks[pos] == "OR":
            pos += 1
            nxt = term()
            if nxt is None:
                return None
            children.append(nxt)
        return children[0] if len(children) == 1 else {"op": "or", "children": children}

    def term():                                      # AND 层
        nonlocal pos
        node = atom()
        if node is None:
            return None
        children = [node]
        while pos < len(toks) and toks[pos] == "AND":
            pos += 1
            nxt = atom()
            if nxt is None:
                return None
            children.append(nxt)
        return children[0] if len(children) == 1 else {"op": "and", "children": children}

    def atom():
        nonlocal pos
        if pos >= len(toks):
            return None
        t = toks[pos]
        if t == "(":
            pos += 1
            node = expr()
            if node is None or pos >= len(toks) or toks[pos] != ")":
                return None
            pos += 1
            return node
        if t[:4].lower() == "part":
            pos += 1
            return {"op": "part", "ref": t.split(None, 1)[1]}
        return None

    tree = expr()
    return tree if tree is not None and pos == len(toks) else None


def _logic_refs(tree: dict | None) -> set:
    """公式树引用的全部 part ref。"""
    out: set = set()
    stack = [tree] if tree else []
    while stack:
        n = stack.pop()
        if n.get("op") == "part":
            out.add(n["ref"])
        else:
            stack += n.get("children", [])
    return out


class PlanSimulator:
    """单个 program 的选课进度状态机。

    公开方法:
      select(code) / deselect(code)  —— 增删已选课
      choose_plan(plan_code)         —— 选定某 major/minor 分支
      status() -> list               —— 每条顶层规则的进度
      available() -> list            —— 未选且属于未完成规则的课程码
    """

    def __init__(self, conn, program_id: str):
        row = conn.execute(
            "SELECT title, total_units, rules FROM programs WHERE program_id = %s",
            (program_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"program 不存在: {program_id!r}")
        self.program_id = program_id
        self.title = row[0]
        self.total_units = row[1]
        self.rules = row[2] or []          # 顶层规则列表(JSONB 已反序列化为 list[dict])
        self.selected: set[str] = set()     # 已选课程码
        self.chosen_plans: set[str] = set()  # 已选定的 plan 分支码

        # 程序级禁课(No credit will be given for…):从可选列表剔除。表未建时为空集(不报错)。
        self.excluded: set[str] = set()
        if conn.execute("SELECT to_regclass('program_exclude')").fetchone()[0]:
            self.excluded = {
                r[0] for r in conn.execute(
                    "SELECT course_code FROM program_exclude WHERE program_id = %s",
                    (program_id,)).fetchall()
            }

        # 先修(阶段三b):code -> 解析树(只装真树/raw)。列未迁移/未回填时为空(软门退化全解锁)。
        # 排除 jsonb 'null'(确无先修)与 SQL NULL(未回填):两者都「不在 _prereq」即按解锁处理。
        self._prereq: dict[str, dict] = {}
        if conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='courses' AND column_name='prerequisite_parsed'"
        ).fetchone():
            self._prereq = {
                r[0]: r[1] for r in conn.execute(
                    "SELECT DISTINCT ON (code) code, prerequisite_parsed FROM courses "
                    "WHERE prerequisite_parsed IS NOT NULL AND prerequisite_parsed <> 'null'::jsonb "
                    "ORDER BY code"
                ).fetchall()
            }

        # 程序级 level 学分上限(aux_rules 的 level_cap,如「at most 24 units at level 1」)。
        # 数据驱动:从 programs.aux_rules 解析;列/数据缺则为空(不报错)。
        self.level_caps: list[dict] = []
        if conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='programs' AND column_name='aux_rules'"
        ).fetchone():
            aux = conn.execute(
                "SELECT aux_rules FROM programs WHERE program_id = %s", (program_id,)
            ).fetchone()[0] or []
            for a in aux:
                if a.get("type") == "level_cap":
                    m = re.search(r"at most\s+(\d+)\s+units?\s+at\s+level\s+(\d+)",
                                  a.get("text", ""), re.I)
                    if m:
                        self.level_caps.append(
                            {"level": int(m.group(2)), "max_units": int(m.group(1)),
                             "text": a["text"]})

        # 程序级布尔公式(如 "Part A AND ( Part B OR Part C ) AND ...")。
        # 列缺/值空 -> 无公式(维持 AND-all 原语义);有公式但解析失败或引用了
        # 不存在的 ref -> 同样回退 AND-all,但 logic_fallback=True 显式透出(不静默)。
        self.rule_logic: str | None = None
        self.logic_tree: dict | None = None
        self.logic_fallback = False
        if conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='programs' AND column_name='rule_logic'"
        ).fetchone():
            self.rule_logic = conn.execute(
                "SELECT rule_logic FROM programs WHERE program_id = %s", (program_id,)
            ).fetchone()[0]
        if self.rule_logic:
            tree = parse_rule_logic(self.rule_logic)
            if tree is None or not _logic_refs(tree) <= {r.get("ref") for r in self.rules}:
                self.logic_fallback = True
            else:
                self.logic_tree = tree
        # OR 组分支选择:group_key("B|C") -> 选定 ref;未选默认组内第一个(如 B=Major)
        self.branch: dict[str, str] = {}
        # SubRule 父子索引(如 C -> [C.1, C.2])
        self._child_of: dict[str, str] = {
            ch: r["ref"] for r in self.rules
            for ch in (r.get("children_refs") or []) if r.get("ref")
        }

        # 全库课程学分:开放规则(E/F)可计入树外码,归属与校验都要查它
        self._course_units: dict[str, float] = {
            r[0]: (float(r[1]) if r[1] is not None else DEFAULT_UNITS)
            for r in conn.execute(
                "SELECT DISTINCT ON (code) code, units FROM courses ORDER BY code"
            ).fetchall()
        }
        self._all_codes = set(self._course_units)

        # 选修范围 level cap(规则 notes:"no more than N units at level L",如 2559 选修 L1≤14)
        self.elective_caps: list[dict] = []
        _seen_caps: set = set()
        for r in self.rules:
            m = re.search(r"no more than\s+(\d+)\s+units?\s+at\s+level\s+(\d+)",
                          r.get("notes") or "", re.I)
            if m and m.groups() not in _seen_caps:
                _seen_caps.add(m.groups())
                self.elective_caps.append(
                    {"level": int(m.group(2)), "max_units": int(m.group(1)),
                     "scope": "electives", "text": r["notes"]})

        # 预建索引:plan_code -> plan 节点,方便 choose_plan 后展开
        self._plans: dict[str, dict] = {}
        # plan_code -> group key:同一条 plan 规则下的分支共享同一 key,用于「择一」互斥
        self._plan_group: dict[str, int] = {}
        self._index_plans(self.rules)

    # ---------- 内部:自引用判断 ----------
    def _is_self_program(self, node: dict) -> bool:
        """判断某 plan 节点是不是「整学位」自引用节点(非可选分支)。

        命中条件(任一):code 等于本 program_id;或 subtype 文本含 'Program'。
        这类节点(如 2559 的 {code='2559', subtype='Undergraduate Program'})只是把
        整学位再嵌一层,既非可选 major/minor,也会让递归自引用,统一跳过。
        """
        if node.get("code") == self.program_id:
            return True
        subtype = node.get("subtype") or ""
        return "Program" in subtype

    # ---------- 内部:索引 ----------
    def _index_plans(self, rules: list, visited: set | None = None) -> None:
        """递归收集所有可选 plan 分支(含嵌套子 plan);跳过自引用整学位节点。"""
        if visited is None:
            visited = set()
        for r in rules:
            # 同一条规则 r 下的所有 plan 分支共享一个 group key(用规则对象 id 标识)
            group = id(r)
            for it in r.get("items", []):
                if it.get("kind") != "plan":
                    continue
                if self._is_self_program(it):  # 自引用整学位节点不收进可选分支
                    continue
                code = it.get("code")
                if code:
                    if code in visited:  # 防同码节点重复展开导致的自引用递归
                        continue
                    visited.add(code)
                    self._plans[code] = it
                    self._plan_group[code] = group
                self._index_plans(it.get("rules", []), visited)

    # ---------- 增删选择 ----------
    def select(self, code: str) -> None:
        self.selected.add(code)

    def deselect(self, code: str) -> None:
        self.selected.discard(code)

    def choose_plan(self, plan_code: str) -> None:
        # 自引用整学位节点(如 program_id 本身)不是可选分支,直接拒绝
        if plan_code == self.program_id or plan_code not in self._plans:
            raise ValueError(f"plan 分支不存在: {plan_code!r}(可选: {sorted(self._plans)})")
        # 择一语义:同一条 plan 规则内互斥,先清掉同 group 已选分支,再选新的
        group = self._plan_group.get(plan_code)
        if group is not None:
            same_group = {c for c in self.chosen_plans if self._plan_group.get(c) == group}
            self.chosen_plans -= same_group
        self.chosen_plans.add(plan_code)

    # ---------- OR 分支(程序级公式,如 2559 的 Major(B) / No-Major(C) 二选一) ----------
    def branch_groups(self) -> list[list[str]]:
        """公式里可切换的 OR 组(子节点全是 part 的 or 节点)。
        混有复合子式的 OR 不提供切换(全部视为活跃,sweep 另行报告)。"""
        groups: list[list[str]] = []

        def walk(n):
            if not isinstance(n, dict):
                return
            if n.get("op") == "or" and all(
                    c.get("op") == "part" for c in n.get("children", [])):
                groups.append([c["ref"] for c in n["children"]])
            else:
                for c in n.get("children", []):
                    walk(c)

        walk(self.logic_tree)
        return groups

    def choose_branch(self, ref: str) -> None:
        for g in self.branch_groups():
            if ref in g:
                self.branch["|".join(g)] = ref
                return
        raise ValueError(f"分支不存在: {ref!r}(可选 OR 组: {self.branch_groups()})")

    def branch_state(self) -> dict:
        """各 OR 组当前选定分支(未显式选则默认组内第一个,2559 即 B=Major)。"""
        return {"|".join(g): self.branch.get("|".join(g), g[0])
                for g in self.branch_groups()}

    def _inactive_refs(self) -> set:
        """未选中的 OR 分支及其全部子规则(失活:不计进度、不供选,课程流入开放规则)。"""
        out: set = set()
        for key, sel in self.branch_state().items():
            out |= {ref for ref in key.split("|") if ref != sel}
        grew = True
        while grew:                                  # 级联子规则(C 失活则 C.1/C.2 失活)
            grew = False
            for r in self.rules:
                if r.get("ref") in out:
                    for ch in r.get("children_refs") or []:
                        if ch not in out:
                            out.add(ch)
                            grew = True
        return out

    # ---------- 开放规则与计划外课归属 ----------
    def _open_rule(self, rule: dict) -> bool:
        """开放规则:select 型、有 units_max、无可枚举项(空 items=程序课表内任选,
        如 E;仅 wildcard=任意课,如 F)。进度来自 attribution(),不来自 items。"""
        if rule.get("select_type") != "select" or rule.get("units_max") is None:
            return False
        if rule.get("children_refs"):
            return False
        return all(it.get("kind") == "wildcard" for it in rule.get("items", []))

    def _enum_codes(self, rule: dict) -> set:
        """规则枚举到的全部课程码(course+equivalence 全选项;含已选定 plan 分支递归)。"""
        out: set = set()
        for it in rule.get("items", []):
            out |= set(self._item_codes(it))
            if (it.get("kind") == "plan" and not self._is_self_program(it)
                    and it.get("code") in self.chosen_plans):
                for sr in it.get("rules", []):
                    out |= self._enum_codes(sr)
        return out

    def _open_level_cap(self, rule: dict) -> int | None:
        """开放规则的课程级别上限:notes 写明 undergraduate 课表的(如 F),限 level<=6。"""
        return 6 if "undergraduate" in (rule.get("notes") or "").lower() else None

    def attribution(self) -> dict:
        """计划外已选码的确定性归属:{"assigned": {code: ref}, "unattributed": [code,...]}。

        活跃枚举规则吃掉的码不参与;剩余码(排序后,确定性)按规则树顺序试开放规则:
        空表规则(E)限「程序课表内」(树内全部枚举码),wildcard 规则(F)任意有效码
        (notes 标 undergraduate 的限 level<=6);单规则填到 units_max 即止。
        被程序禁修 / 不在 courses 库 / 无处可归 -> unattributed。"""
        inactive = self._inactive_refs()
        enum_codes: set = set()
        for rule in self.rules:
            if rule.get("ref") in inactive or rule.get("children_refs"):
                continue
            enum_codes |= self._enum_codes(rule)
        open_rules = [r for r in self.rules
                      if self._open_rule(r) and r.get("ref") not in inactive]
        prog_list = self._all_referenced_codes()
        fill = {r["ref"]: 0.0 for r in open_rules}
        assigned: dict[str, str] = {}
        unattributed: list[str] = []
        for code in sorted(self.selected - enum_codes):
            if code in self.excluded or code not in self._all_codes:
                unattributed.append(code)
                continue
            u = self._course_units.get(code, DEFAULT_UNITS)
            m = re.search(r"\d", code)
            lvl = int(m.group()) if m else None
            for r in open_rules:
                ref = r["ref"]
                wild = any(it.get("kind") == "wildcard" for it in r.get("items", []))
                if not wild and code not in prog_list:
                    continue
                cap_lv = self._open_level_cap(r)
                if cap_lv is not None and lvl is not None and lvl > cap_lv:
                    continue
                if fill[ref] + u > float(r["units_max"]):
                    continue
                assigned[code] = ref
                fill[ref] += u
                break
            else:
                unattributed.append(code)
        return {"assigned": assigned, "unattributed": unattributed}

    # ---------- 进度计算 ----------
    def _item_codes(self, item: dict) -> list[str]:
        """某 item 涉及的全部课程码(course=自身;equivalence=所有选项;plan/wildcard=空)。"""
        k = item.get("kind")
        if k == "course":
            return [item["code"]] if item.get("code") else []
        if k == "equivalence":
            return [o["code"] for o in item.get("options", []) if o.get("code")]
        return []

    def _claims(self) -> dict[str, str]:
        """已选码 -> 计数归属的顶层规则 ref(树序先到先得,防一码计两组,
        如 DECO2801 同时在 major 课表与 D 枚举表)。失活分支不参与认领。"""
        inactive = self._inactive_refs()
        claims: dict[str, str] = {}

        def claim(rule, owner):
            for it in rule.get("items", []):
                k = it.get("kind")
                if k in ("course", "equivalence"):
                    for c in self._item_codes(it):
                        if c in self.selected and c not in claims:
                            claims[c] = owner
                elif (k == "plan" and not self._is_self_program(it)
                        and it.get("code") in self.chosen_plans):
                    for sr in it.get("rules", []):
                        claim(sr, owner)

        for rule in self.rules:
            ref = rule.get("ref")
            if ref in inactive or rule.get("children_refs"):
                continue
            claim(rule, ref)
        return claims

    def _item_done_units(self, item: dict, claims: dict | None = None,
                         owner: str | None = None) -> float:
        """某 item 已贡献的学分。

        course:选了就计该门 units。
        equivalence:选了任一选项,只按那一门计一次(取已选选项里 units 最大的一门,
                    通常组内同分,口径是「满足该项即得该项学分」)。
        传入 claims/owner 时,只计认领归属本规则的码(防跨规则重复计数)。
        """
        def mine(code):
            return code in self.selected and (claims is None or claims.get(code) == owner)

        k = item.get("kind")
        if k == "course":
            return _units(item) if mine(item.get("code")) else 0.0
        if k == "equivalence":
            picked = [o for o in item.get("options", []) if mine(o.get("code"))]
            return _units(max(picked, key=_units)) if picked else 0.0
        return 0.0

    def _rule_units_done(self, rule: dict, claims: dict | None = None,
                         owner: str | None = None) -> float:
        """一条规则内,所有 course/equivalence 项已贡献学分之和。"""
        return sum(self._item_done_units(it, claims, owner) for it in rule.get("items", []))

    def _plan_units_done(self, plan: dict, claims: dict | None = None,
                         owner: str | None = None) -> float:
        """一个已选定 plan 分支:其子规则全部 course/equivalence 已贡献学分之和。

        每条子规则按自身 units_max 封顶后再累加;超额学分不计入分支进度。
        子规则里再嵌的 plan,只有同样被 choose_plan 时才递归计入。
        """
        total = 0.0
        for sr in plan.get("rules", []):
            sr_done, _ = self._capped(self._rule_units_done(sr, claims, owner),
                                      self._units_max(sr))
            total += sr_done
            for it in sr.get("items", []):
                if (
                    it.get("kind") == "plan"
                    and not self._is_self_program(it)
                    and it.get("code") in self.chosen_plans
                ):
                    total += self._plan_units_done(it, claims, owner)
        return total

    def _required(self, rule_or_plan: dict) -> float:
        """规则/分支的必需学分 = units_min(None 视为 0)。"""
        m = rule_or_plan.get("units_min")
        return float(m) if m is not None else 0.0

    def _units_max(self, rule_or_plan: dict) -> float | None:
        """规则/分支的学分上限 = units_max(None 表示不封顶)。"""
        m = rule_or_plan.get("units_max")
        return float(m) if m is not None else None

    def _capped(self, done: float, cap: float | None) -> tuple[float, bool]:
        """按上限封顶:返回 (计入进度的学分, 是否超额)。cap=None 不封顶。"""
        if cap is not None and done > cap:
            return cap, True
        return done, False

    def _eval_logic(self, tree: dict | None, done_map: dict) -> bool:
        """公式求值:leaf=该规则 done;and=全真;or=按 branch_state 选定分支的 done
        (可切换组),复合 OR 退化为 any。tree=None -> 全部非子规则 done(AND-all)。"""
        if tree is None:
            return all(v for k, v in done_map.items() if k not in self._child_of)
        op = tree.get("op")
        if op == "part":
            return bool(done_map.get(tree["ref"], False))
        kids = tree.get("children", [])
        if op == "and":
            return all(self._eval_logic(c, done_map) for c in kids)
        if op == "or":
            if all(c.get("op") == "part" for c in kids):
                chosen = self.branch_state().get("|".join(c["ref"] for c in kids))
                return bool(done_map.get(chosen, False))
            return any(self._eval_logic(c, done_map) for c in kids)
        return False

    def status(self) -> list:
        """每条顶层规则的进度。

        返回 list[dict]:
          {ref, title, select_type, units_required, units_done, units_counted, over_max,
           done, remaining, plan_options/chosen_plans(仅含 plan 的规则有),
           child_of(SubRule 子规则), inactive(未选中 OR 分支)}
        语义:开放规则(E/F)的进度来自 attribution();SubRule 父规则(C)= 子规则
        counted 之和按自身 min/max 判定;失活分支(未选中的 OR 支)counted 置 0。
        """
        att = self.attribution()
        inactive = self._inactive_refs()
        claims = self._claims()
        entries: dict[str, dict] = {}
        for rule in self.rules:
            if rule.get("children_refs"):
                continue                              # 父规则后算(依赖子 entry)
            entries[rule.get("ref")] = self._base_entry(rule, att, inactive, claims)
        for rule in self.rules:
            if not rule.get("children_refs"):
                continue
            entries[rule.get("ref")] = self._parent_entry(rule, entries, inactive)
        return [entries[r.get("ref")] for r in self.rules]

    def _parent_entry(self, rule: dict, entries: dict, inactive: set) -> dict:
        """SubRule 父规则(如 C「No Major Option」8–24):counted=子规则 counted 之和
        按自身 max 封顶;done=达自身 min 且子公式(如 Part C.1 AND Part C.2)满足。"""
        ref = rule.get("ref")
        required = self._required(rule)
        units_max = self._units_max(rule)
        kids = [entries[ch] for ch in rule.get("children_refs", []) if ch in entries]
        raw = sum(k["units_done"] for k in kids)
        effective, over_max = self._capped(sum(k["units_counted"] for k in kids), units_max)
        sub = parse_rule_logic(rule.get("rule_logic"))
        kids_ok = (self._eval_logic(sub, {k["ref"]: k["done"] for k in kids})
                   if sub else all(k["done"] for k in kids))
        if ref in inactive:
            raw = effective = 0.0
            over_max = kids_ok = False
        return {"ref": ref, "title": rule.get("title") or "",
                "select_type": rule.get("select_type"),
                "part_type": rule.get("part_type"),
                "children_refs": rule.get("children_refs"),
                "units_required": required, "units_max": units_max,
                "units_done": raw, "units_counted": effective, "over_max": over_max,
                "done": effective >= required and kids_ok,
                "remaining": max(required - effective, 0.0),
                "inactive": ref in inactive,
                "child_of": self._child_of.get(ref)}

    def _base_entry(self, rule: dict, att: dict, inactive: set, claims: dict) -> dict:
        ref = rule.get("ref")
        title = rule.get("title") or ""
        select_type = rule.get("select_type")
        required = self._required(rule)
        units_max = self._units_max(rule)
        if self._open_rule(rule):                     # E/F:进度=归属到本规则的计划外课
            done_units = sum(self._course_units.get(c, DEFAULT_UNITS)
                             for c, r2 in att["assigned"].items() if r2 == ref)
        else:
            done_units = self._rule_units_done(rule, claims, ref)

        entry: dict = {
            "ref": ref,
            "title": title,
            "select_type": select_type,
        }

        # 该规则若含 plan 项:列出可选分支(跳过自引用整学位节点),并按择一并入进度
        plan_items = [
            it
            for it in rule.get("items", [])
            if it.get("kind") == "plan" and not self._is_self_program(it)
        ]
        if plan_items:
            entry["plan_options"] = [
                {
                    "code": p.get("code"),
                    "name": p.get("name") or "",
                    "subtype": p.get("subtype"),
                    "units_min": self._required(p),
                    "units_max": self._units_max(p),
                }
                for p in plan_items
            ]
            chosen_here = [p for p in plan_items if p.get("code") in self.chosen_plans]
            entry["chosen_plans"] = [p.get("code") for p in chosen_here]
            # 择一语义:必需学分 = 修满 1 个分支。
            # 已选分支 -> 取该分支(同 group 互斥,最多一个)的 units_min;
            # 未选 -> 取各分支里最小的 units_min(至少修满一个分支)。
            if chosen_here:
                required = max(self._required(p) for p in chosen_here)
                done_units += sum(self._plan_units_done(p, claims, ref) for p in chosen_here)
            else:
                required = min((self._required(p) for p in plan_items), default=0.0)
            # plan 分支自身的封顶已在 _plan_units_done 内逐子规则处理,这里不再对整组封顶
            effective_done = done_units
            over_max = False
        else:
            # 普通规则:done 按 units_max 封顶,超额学分不算有效进度
            effective_done, over_max = self._capped(done_units, units_max)

        if ref in inactive:                       # 失活分支:不计进度(课程流入开放规则)
            done_units = effective_done = 0.0
            over_max = False
        entry["units_required"] = required
        entry["units_max"] = units_max
        entry["units_done"] = done_units          # 原始已修学分(可超 units_max)
        entry["units_counted"] = effective_done   # 计入进度的学分(已按 units_max 封顶)
        entry["over_max"] = over_max
        entry["done"] = effective_done >= required and ref not in inactive
        entry["remaining"] = max(required - effective_done, 0.0)
        entry["inactive"] = ref in inactive
        entry["child_of"] = self._child_of.get(ref)
        if self._open_rule(rule):                 # 开放规则:UI 据此挂课程搜索框
            entry["open"] = True
            entry["open_scope"] = "any" if rule.get("items") else "program"
            entry["open_max_level"] = self._open_level_cap(rule)
        return entry

    # ---------- 可选列表 ----------
    def _collect_codes(self, rule: dict, include_chosen_plans: bool) -> list[str]:
        """一条规则下可枚举的课程码(course + equivalence;plan 视参数决定是否下钻)。

        已满足的 equivalence 项(已选其中任一 / 已得该项学分)不再枚举其余 options。
        """
        codes: list[str] = []
        for it in rule.get("items", []):
            k = it.get("kind")
            if k == "course":
                codes += self._item_codes(it)
            elif k == "equivalence":
                if self._item_done_units(it) > 0:  # 该等价项已满足,不再列出其余备选
                    continue
                codes += self._item_codes(it)
            elif (
                k == "plan"
                and include_chosen_plans
                and not self._is_self_program(it)
                and it.get("code") in self.chosen_plans
            ):
                for sr in it.get("rules", []):
                    codes += self._collect_codes(sr, include_chosen_plans)
        return codes

    def _closed(self, entry: dict) -> bool:
        """规则是否不再供选:有 units_max 的组到顶(counted>=max)才收敛;
        无上限的组维持「满 min 即收敛」。done(满 min)≠ 不能继续选。"""
        mx = entry.get("units_max")
        if mx is not None:
            return entry["units_counted"] >= mx
        return entry["done"]

    def available(self) -> list:
        """当前未选、且属于「未收敛规则」的课程码(含已选定 plan 分支的课)。

        去重并保持稳定顺序;wildcard 不枚举(它不绑定具体课程码)。
        """
        seen: set[str] = set()
        out: list[str] = []
        st = {e["ref"]: e for e in self.status()}
        for rule in self.rules:
            e = st[rule.get("ref")]
            if e.get("inactive") or self._closed(e):
                continue
            for code in self._collect_codes(rule, include_chosen_plans=True):
                if code in self.selected or code in seen or code in self.excluded:
                    continue
                seen.add(code)
                out.append(code)
        return out

    # ---------- 按规则分组(供 Web UI;available_by_rule 拍平去重 == available()) ----------
    def _slots_for_rule(self, rule: dict, seen: set) -> list:
        """一条规则下可选的 slot 列表:course 各自成 slot,equivalence 聚成「二选一」slot。

        口径与 available() 一致:已满足的 equivalence 不出;已选/已出现/被禁的码扣掉;
        seen 跨规则共享以全局去重(先出现的规则胜)。已选定 plan 分支的课递归并入本规则。
        slot = {'kind':'course','code'} | {'kind':'equiv','options':[...]}
        """
        slots: list = []
        for it in rule.get("items", []):
            k = it.get("kind")
            if k == "course":
                code = it.get("code")
                if (code and code not in self.selected
                        and code not in seen and code not in self.excluded):
                    seen.add(code)
                    slots.append({"kind": "course", "code": code})
            elif k == "equivalence":
                if self._item_done_units(it) > 0:  # 该等价项已满足,不再列备选
                    continue
                opts = [
                    o["code"]
                    for o in it.get("options", [])
                    if o.get("code") and o["code"] not in self.selected
                    and o["code"] not in seen and o["code"] not in self.excluded
                ]
                if opts:
                    seen.update(opts)
                    slots.append({"kind": "equiv", "options": opts})
            elif (
                k == "plan"
                and not self._is_self_program(it)
                and it.get("code") in self.chosen_plans
            ):
                for sr in it.get("rules", []):
                    slots += self._slots_for_rule(sr, seen)
        return slots

    def available_by_rule(self) -> dict:
        """每条「未收敛」顶层规则 -> 可选 slot 列表(course / equiv 二选一)。"""
        seen: set[str] = set()
        st = {e["ref"]: e for e in self.status()}
        out: dict[str, list] = {}
        for rule in self.rules:
            ref = rule.get("ref")
            if st[ref].get("inactive") or self._closed(st[ref]):
                continue
            slots = self._slots_for_rule(rule, seen)
            if slots:
                out[ref] = slots
        return out

    def _selected_in_rule(self, rule: dict, seen: set) -> list:
        """一条规则下「已选」的课程码(含 equivalence 备选与已选定 plan 分支的课)。

        与 available 不同:不跳过已满足的 equivalence、不跳过已 done 规则,
        这样用户已选的课在对应规则段里始终可见(可点击退课)。先出现的规则胜。
        """
        picked: list = []
        for it in rule.get("items", []):
            k = it.get("kind")
            if k == "course":
                code = it.get("code")
                if code in self.selected and code not in seen:
                    seen.add(code)
                    picked.append(code)
            elif k == "equivalence":
                for o in it.get("options", []):
                    code = o.get("code")
                    if code in self.selected and code not in seen:
                        seen.add(code)
                        picked.append(code)
            elif (
                k == "plan"
                and not self._is_self_program(it)
                and it.get("code") in self.chosen_plans
            ):
                for sr in it.get("rules", []):
                    picked += self._selected_in_rule(sr, seen)
        return picked

    def selected_by_rule(self) -> dict:
        """每条顶层规则 -> 已选课程码列表(供 UI 在规则段内显示已选,可退课)。

        失活分支不列(其已选课经 attribution 流入 E/F);开放规则列归属到它的码。"""
        seen: set[str] = set()
        out: dict[str, list] = {}
        inactive = self._inactive_refs()
        att = self.attribution()
        for rule in self.rules:
            ref = rule.get("ref")
            if ref in inactive:
                continue
            picked = self._selected_in_rule(rule, seen)
            for code, aref in sorted(att["assigned"].items()):
                if aref == ref and code not in seen:
                    seen.add(code)
                    picked.append(code)
            if picked:
                out[ref] = picked
        return out

    def overall(self) -> dict:
        """程序整体视图:总进度(子规则不重复计)、公式满足、分支组、未归属课。"""
        st = self.status()
        att = self.attribution()
        total = sum(e["units_counted"] for e in st
                    if not e.get("child_of") and not e.get("inactive"))
        done_map = {e["ref"]: e["done"] for e in st}
        return {
            "total_counted": total,
            "total_units": self.total_units,
            "rule_logic": self.rule_logic,
            "logic_fallback": self.logic_fallback,
            "branch_groups": self.branch_groups(),
            "branch": self.branch_state(),
            "formula_satisfied": self._eval_logic(self.logic_tree, done_map),
            "unattributed": att["unattributed"],
        }

    # ---------- 学分映射(供排课 scheduler) ----------
    def _walk_units(self, rule: dict, acc: dict) -> None:
        for it in rule.get("items", []):
            k = it.get("kind")
            if k == "course" and it.get("code"):
                acc.setdefault(it["code"], _units(it))
            elif k == "equivalence":
                for o in it.get("options", []):
                    if o.get("code"):
                        acc.setdefault(o["code"], _units(o))
            elif k == "plan" and not self._is_self_program(it):
                self._walk_units(it, acc)
            for sr in it.get("rules", []):
                self._walk_units(sr, acc)

    def units_map(self) -> dict:
        """本 program 规则树里所有课程码 -> 学分(供排课;缺失由 scheduler 兜底 DEFAULT_UNITS)。"""
        acc: dict[str, float] = {}
        for rule in self.rules:
            self._walk_units(rule, acc)
        return acc

    def level_cap_status(self) -> list:
        """程序级 level 上限的实时状态(如「level 1 最多 24 学分」)。

        每条:{level, max_units, used(已选该级别学分), over(是否超), text}。
        级别 = 课码第一个数字(CSSE1001 -> 1)。无 cap 数据时返回 []。
        """
        if not self.level_caps and not self.elective_caps:
            return []
        um = self.units_map()

        def units_of(c):
            return float(um.get(c) or self._course_units.get(c) or DEFAULT_UNITS)

        def used_map(codes):
            acc: dict[int, float] = {}
            for c in codes:
                m = re.search(r"\d", c)
                if m:
                    lv = int(m.group())
                    acc[lv] = acc.get(lv, 0.0) + units_of(c)
            return acc

        out = []
        all_used = used_map(self.selected)
        for cap in self.level_caps:                   # 程序级:全部已选
            used = all_used.get(cap["level"], 0.0)
            out.append({"level": cap["level"], "max_units": cap["max_units"],
                        "used": used, "over": used > cap["max_units"],
                        "scope": "program", "text": cap["text"]})
        if self.elective_caps:                        # 选修范围:扣掉核心组与已选 major 的课
            elect_used = used_map(self._elective_selected())
            for cap in self.elective_caps:
                used = elect_used.get(cap["level"], 0.0)
                out.append({"level": cap["level"], "max_units": cap["max_units"],
                            "used": used, "over": used > cap["max_units"],
                            "scope": "electives", "text": cap["text"]})
        return out

    def _elective_selected(self) -> set:
        """选修口径的已选码 = 已选 -('all' 型核心组枚举 ∪ 已选 plan 分支枚举)。"""
        core: set = set()
        for r in self.rules:
            if r.get("select_type") == "all":
                core |= self._enum_codes(r)
        for code in self.chosen_plans:
            pl = self._plans.get(code)
            if pl:
                for sr in pl.get("rules", []):
                    core |= self._enum_codes(sr)
        return {c for c in self.selected if c not in core}

    # ---------- 先修软门(阶段三b) ----------
    def _missing_codes(self, tree: dict) -> list:
        out: list[str] = []
        stack = [tree]
        while stack:
            n = stack.pop()
            if n.get("op") == "course":
                if n["code"] not in self.selected:
                    out.append(n["code"])
            else:
                stack += n.get("children", [])
        return list(dict.fromkeys(out))

    def locked_status(self, code: str) -> dict:
        """某课的先修锁态:unlocked / locked / unknown(无数据或无法解析)。"""
        tree = self._prereq.get(code)
        if tree is None:                       # 无先修数据(确无 / 未爬到,都按解锁)
            return {"code": code, "state": "unlocked", "missing": [], "reason": None}
        if tree.get("op") == "raw":
            return {"code": code, "state": "unknown", "missing": [],
                    "reason": tree.get("unparsed", "")}
        ok, reason = satisfied(tree, self.selected)
        if ok:
            return {"code": code, "state": "unlocked", "missing": [], "reason": None}
        return {"code": code, "state": "locked",
                "missing": self._missing_codes(tree), "reason": reason}

    def available_detailed(self) -> list:
        """available() 各码附先修锁态(不隐藏 locked,只打标)。"""
        return [self.locked_status(c) for c in self.available()]

    def _all_referenced_codes(self) -> set:
        codes: set[str] = set()

        def walk(rule):
            for it in rule.get("items", []):
                k = it.get("kind")
                if k == "course" and it.get("code"):
                    codes.add(it["code"])
                elif k == "equivalence":
                    codes.update(o["code"] for o in it.get("options", []) if o.get("code"))
                for sr in it.get("rules", []):
                    walk(sr)
        for rule in self.rules:
            walk(rule)
        return codes

    def prereq_report(self, conn) -> dict:
        """先修覆盖缺口(显式报告,不静默):按 DB 真实状态分类每个引用码。

        区分 4 态:有先修树 / 无法解析(raw)/ 确无先修(jsonb null)/ 未回填(SQL null 或无行)。
        一个码可能有多 offering 行,按「最强信号」归类:tree > raw > null > 未回填。
        """
        refs = self._all_referenced_codes()
        rows = conn.execute(
            "SELECT code, "
            " bool_or(prerequisite_parsed IS NOT NULL AND prerequisite_parsed <> 'null'::jsonb "
            "         AND prerequisite_parsed->>'op' <> 'raw') AS has_tree, "
            " bool_or(prerequisite_parsed->>'op' = 'raw') AS has_raw, "
            " bool_or(prerequisite_parsed = 'null'::jsonb) AS has_null "
            "FROM courses WHERE code = ANY(%s) GROUP BY code",
            (list(refs),),
        ).fetchall()
        state: dict[str, str] = {}
        for code, has_tree, has_raw, has_null in rows:
            state[code] = ("with_prereq" if has_tree else "unparseable" if has_raw
                           else "no_prereq" if has_null else "no_data")
        no_data = sorted([c for c in refs if c not in state]
                         + [c for c, s in state.items() if s == "no_data"])
        cnt = lambda s: sum(1 for v in state.values() if v == s)
        return {
            "referenced": len(refs),
            "with_prereq": cnt("with_prereq"), "unparseable": cnt("unparseable"),
            "no_prereq": cnt("no_prereq"),
            "no_data_not_scraped": len(no_data), "no_data_codes": no_data,
        }


def _print_status(label: str, st: list) -> None:
    print(f"\n===== {label} =====")
    for e in st:
        flag = "✓" if e["done"] else " "
        cap = "" if e.get("units_max") is None else f" 上限{e['units_max']}"
        over = " !超额" if e.get("over_max") else ""
        print(f"  [{flag}] {e['ref']:<4} {e['title'][:42]:<42} "
              f"{e['select_type']:<6} {e['units_done']:>4}/{e['units_required']:<4}{cap} "
              f"剩 {e['remaining']}{over}")
        if e.get("plan_options"):
            chosen = set(e.get("chosen_plans", []))
            for po in e["plan_options"]:
                mark = "★" if po["code"] in chosen else "·"
                pcap = "" if po.get("units_max") is None else f"/max{po['units_max']}"
                print(f"        {mark} {po['code']}  {po['name']} "
                      f"({po['subtype']}, 需{po['units_min']}{pcap})")


if __name__ == "__main__":
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        sim = PlanSimulator(conn, "2559")
        print(f"program {sim.program_id}: {sim.title}  total_units={sim.total_units}")
        print(f"可选 plan 分支: {sorted(sim._plans)}")

        st0 = sim.status()
        _print_status("初始 status", st0)
        avail0 = sim.available()
        print(f"\n初始 available 课程数: {len(avail0)}")
        print(f"  样例: {avail0[:12]}")

        # 选两门核心课
        sim.select("CSSE1001")
        sim.select("CSSE2002")
        st1 = sim.status()
        avail1 = sim.available()
        print("\n>> 已 select CSSE1001, CSSE2002")
        ruleA = next(e for e in st1 if e["ref"] == "A")
        print(f"   规则A 进度: {ruleA['units_done']}/{ruleA['units_required']} (done={ruleA['done']})")
        print(f"   available 课程数: {len(avail0)} -> {len(avail1)}")
        removed = [c for c in avail0 if c not in avail1]
        print(f"   从可选列表移除: {removed}")

        # 选第一个 major 分支
        first_plan = sim.status()[1]["plan_options"][0]["code"]
        sim.choose_plan(first_plan)
        st2 = sim.status()
        avail2 = sim.available()
        _print_status(f"choose_plan({first_plan}) 后 status", st2)
        ruleB_before = next(e for e in st1 if e["ref"] == "B")
        ruleB_after = next(e for e in st2 if e["ref"] == "B")
        print(f"\n>> choose_plan({first_plan})")
        print(f"   规则B 必需学分: {ruleB_before['units_required']} -> {ruleB_after['units_required']}")
        print(f"   规则B chosen_plans: {ruleB_after.get('chosen_plans')}")
        print(f"   available 课程数: {len(avail1)} -> {len(avail2)}")
        added = [c for c in avail2 if c not in avail1]
        print(f"   分支展开新增可选课: {added}")

        # 反向验证 deselect 生效
        sim.deselect("CSSE1001")
        ruleA2 = next(e for e in sim.status() if e["ref"] == "A")
        print(f"\n>> deselect CSSE1001 -> 规则A 进度回退: {ruleA['units_done']} -> {ruleA2['units_done']}")

        # ---------------- 复现用例断言(修后应全部通过) ----------------
        print("\n===== 复现用例断言 =====")
        sim2 = PlanSimulator(conn, "2559")

        # 1) 自引用 '2559' 不在可选 plan 分支
        assert "2559" not in sim2._plans, "2559 仍被收进可选分支"
        print("  [OK] '2559' 不在可选 plan 分支:", sorted(sim2._plans))

        # 2) choose_plan('2559') 报错
        try:
            sim2.choose_plan("2559")
            raise AssertionError("choose_plan('2559') 未报错")
        except ValueError:
            print("  [OK] choose_plan('2559') 抛 ValueError")

        # 3) status() 各选修组带 units_max
        st = sim2.status()
        by_ref = {e["ref"]: e for e in st}
        for ref, exp in (("C.1", 16.0), ("C.2", 22.0), ("D", 16.0), ("F", 16.0)):
            assert by_ref[ref]["units_max"] == exp, f"{ref} units_max={by_ref[ref]['units_max']} != {exp}"
        print("  [OK] C.1/C.2/D/F units_max:",
              {r: by_ref[r]["units_max"] for r in ("C.1", "C.2", "D", "F")})

        # 4) 择一:choose ARTINC2559 再 choose CYBERC2559 -> 只保留后者,required 不累加成 32
        sim2.choose_plan("ARTINC2559")
        sim2.choose_plan("CYBERC2559")
        ruleB = next(e for e in sim2.status() if e["ref"] == "B")
        assert ruleB["chosen_plans"] == ["CYBERC2559"], f"chosen_plans={ruleB['chosen_plans']}"
        assert ruleB["units_required"] == 16.0, f"required={ruleB['units_required']}(择一应为 16)"
        print(f"  [OK] 择一互斥: chosen_plans={ruleB['chosen_plans']} required={ruleB['units_required']}")

        # 5) 选 MATH1061 后 available 不再含同 equivalence 的 MATH1081
        sim3 = PlanSimulator(conn, "2559")
        av_before = sim3.available()
        assert "MATH1081" in av_before, "前提:初始 available 应含 MATH1081"
        sim3.select("MATH1061")
        av_after = sim3.available()
        assert "MATH1081" not in av_after, "MATH1081 仍在 available(equivalence 未收敛)"
        print("  [OK] 选 MATH1061 后 available 不含 MATH1081")

        # 6) units_max 封顶在 status() 真实生效:C.2(min4/max22)选满全部 course
        #    (27 门 * 2 = 54 学分)-> counted 封顶到 22、over_max=True、不算有效进度超额。
        #    新语义:C.2 在 No-Major 分支下,须先 choose_branch('C') 激活(默认 B=Major)。
        sim4 = PlanSimulator(conn, "2559")
        if sim4.branch_groups():
            sim4.choose_branch("C")
        c2_rule = next(r for r in sim4.rules if r.get("ref") == "C.2")
        for it in c2_rule.get("items", []):
            if it.get("kind") == "course" and it.get("code"):
                sim4.select(it["code"])
        c2 = next(e for e in sim4.status() if e["ref"] == "C.2")
        assert c2["units_done"] > c2["units_max"], "前提:C.2 应已超额"
        assert c2["units_counted"] == c2["units_max"], f"counted={c2['units_counted']} 未封顶到 max"
        assert c2["over_max"] is True, "over_max 未置 True"
        print(f"  [OK] C.2 封顶: done={c2['units_done']} counted={c2['units_counted']} "
              f"(max={c2['units_max']}) over_max={c2['over_max']}")

        print("\n所有复现用例断言通过。")
