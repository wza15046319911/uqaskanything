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

from app.core.config import DSN

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
        self._bin_of: dict[str, int] = {}    # code -> 认领归属叶子规则 id(status() 时由 _assign 填)

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

        # 跨程序「Program 型引用」展开:把指向整 program 的空 plan 引用(如 5257 A.4
        # 「2u from MCyberSec program electives」、2560 B.3 引用 5257)就地替换成被引
        # program 的课程池。必须在 _index_plans 前做(展开后这些引用不再作为可选 major)。
        self._expand_program_refs(conn)

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

    # ---------- 内部:跨程序 Program 引用展开 ----------
    @staticmethod
    def _is_program_ref(it: dict) -> bool:
        """是否「Program 型」plan 引用(指向整 program,非 major/minor 分支):
        code 是纯数字 program_id,或 subtype 文本含 'Program',且 rules 未展开(空)。"""
        if it.get("kind") != "plan" or it.get("rules"):
            return False
        code = str(it.get("code") or "")
        return code.isdigit() or "Program" in (it.get("subtype") or "")

    def _program_pools(self, conn, codes: set) -> dict:
        """被引 program 的课程池:{program_code: set(course_codes)}。只收直接 course/equiv
        码并下钻普通 major/minor plan,遇 Program 型引用即停(防自引用/互引成环)。"""
        rows = conn.execute(
            "SELECT program_id, rules FROM programs WHERE program_id = ANY(%s)",
            (list(codes),)).fetchall()
        have = {pid: rules for pid, rules in rows}

        def collect(rules, acc):
            for r in rules or []:
                for it in r.get("items", []):
                    k = it.get("kind")
                    if k == "course" and it.get("code"):
                        acc.add(it["code"])
                    elif k == "equivalence":
                        acc.update(o["code"] for o in it.get("options", []) if o.get("code"))
                    elif k == "plan" and not self._is_program_ref(it):
                        collect(it.get("rules"), acc)
        pools: dict[str, set] = {}
        for pid in codes:
            if pid in have:
                acc: set = set()
                collect(have[pid], acc)
                pools[pid] = {c for c in acc if c in self._all_codes}
        return pools

    def _expand_program_refs(self, conn) -> None:
        """就地把 Program 型引用替换成被引 program 的课程池(course items)。

        self-ref(code==program_id)= 本 program 自己的课池;cross-ref 同理引用别的 program。
        被引 program 不在库或课池为空 -> 该引用保持原样(下游仍按自引用跳过),
        并记入 self.unresolved_program_refs(不静默)。需在 _course_units 后、_index_plans 前调用。"""
        self.unresolved_program_refs: list = []
        refs: set = set()

        def walk_collect(rules):
            for r in rules or []:
                for it in r.get("items", []):
                    if self._is_program_ref(it):
                        refs.add(str(it["code"]))
                    elif it.get("kind") == "plan" and it.get("rules"):
                        walk_collect(it["rules"])
        walk_collect(self.rules)
        if not refs:
            return
        pools = self._program_pools(conn, refs)

        def plan_codes(rules) -> set:
            """一组规则(某 plan 的全部子规则)里显式列出的 course/equiv 码。"""
            acc: set = set()
            for r in rules or []:
                for it in r.get("items", []):
                    acc.update(self._item_codes(it))
                    if it.get("kind") == "plan" and it.get("rules"):
                        acc |= plan_codes(it["rules"])
            return acc

        def walk_replace(rules, exclude: set):
            """exclude:同一 plan 内已显式列出/已展开过的课码。顶层调用 exclude 为空
            (顶层电选规则保持全池,跨规则去重交给 _claims);进入某 plan 时 exclude 取该
            plan 的显式课码,避免「专业内 program 电选」重复列出专业必修课导致 _plan_units_done
            重复计数。展开一处引用后,把已用课码并入 exclude,防同 plan 内多处引用再次重列。"""
            for r in rules or []:
                new_items: list = []
                for it in r.get("items", []):
                    if self._is_program_ref(it):
                        pool = pools.get(str(it["code"]))
                        pool = {c for c in pool if c not in exclude} if pool else set()
                        if not pool:
                            self.unresolved_program_refs.append(
                                {"ref": r.get("ref"), "code": str(it["code"])})
                            new_items.append(it)
                            continue
                        for c in sorted(pool):
                            new_items.append(
                                {"kind": "course", "code": c, "units": self._course_units[c]})
                        exclude |= pool
                    else:
                        if it.get("kind") == "plan" and it.get("rules"):
                            walk_replace(it["rules"], exclude | plan_codes(it["rules"]))
                        new_items.append(it)
                r["items"] = new_items
        walk_replace(self.rules, set())

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
        """开放规则:select 型、无可枚举项(空 items=程序课表内任选,如 E;仅 wildcard=任意课,
        如 F / 2557 A.6)。进度来自 attribution(),不来自 items。
        units_max 为 None 时:仅放开「纯 wildcard」自由选修(任意课,如 A.6 'General Elective');
        空表规则(E,程序课表内任选)无上限会成无限吸口,不放开。"""
        if rule.get("select_type") != "select" or rule.get("children_refs"):
            return False
        items = rule.get("items", [])
        if not all(it.get("kind") == "wildcard" for it in items):
            return False
        if rule.get("units_max") is None:
            return bool(items)
        return True

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
                mx = self._units_max(r)            # None = 无上限(纯 wildcard 自由选修)
                if mx is not None and fill[ref] + u > mx:
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

    def _claimable_codes(self, rule: dict) -> list:
        """一条规则可认领的全部课程码(course + equivalence 各选项 + 已选定 plan 分支的课),
        与计数口径对齐(对照 _claims/_rule_units_done)。不按是否已选过滤。"""
        out: list = []
        for it in rule.get("items", []):
            k = it.get("kind")
            if k in ("course", "equivalence"):
                out += self._item_codes(it)
            elif (k == "plan" and not self._is_self_program(it)
                    and it.get("code") in self.chosen_plans):
                for sr in it.get("rules", []):
                    out += self._claimable_codes(sr)
        return out

    def _claim_slack(self, rule: dict) -> float:
        """规则的松弛度 = 可选学分 - 必需学分(父规则/无可枚举课不参与认领,记 +inf)。
        松弛度越小越「紧」(必修,或备选恰好够),越该优先认领共享课。"""
        if rule.get("children_refs"):
            return float("inf")
        codes = set(self._claimable_codes(rule))
        avail = sum(self._course_units.get(c, DEFAULT_UNITS) for c in codes)
        return max(0.0, avail - self._effective_required(rule))

    def _ordered_rules(self) -> list:
        """认领顺序:按松弛度升序(紧/必修规则先认领共享课),同松弛度保持原树序。
        仅用于共享课去重分配;各调用方输出按 ref 映射,与 UI 展示顺序无关。"""
        return [r for _, r in sorted(enumerate(self.rules),
                                     key=lambda ir: (self._claim_slack(ir[1]), ir[0]))]

    def _direct_codes(self, rule: dict) -> list:
        """规则自身直接列出的 course/equivalence 课码(不下钻 plan;plan 另成 bin)。"""
        out: list = []
        for it in rule.get("items", []):
            if it.get("kind") in ("course", "equivalence"):
                out += self._item_codes(it)
        return out

    def _assign(self) -> dict:
        """全局叶子级认领:把每门已选课唯一归属到一个「叶子 bin」,各 bin 不超自身 units_max,
        按顶层规则紧度做增广匹配,让尽量多顶层规则达到 _effective_required。返回 code -> bin_id
        (bin_id = id(叶子规则 dict))。计数层据此判每门课算在哪条规则——取代旧「顶层只认领、
        plan 内部子规则再各自封顶求和」的两层割裂(后者既会跨子规则重复计数,又会把课堆进有上限
        子规则被截掉、其它子规则空着导致 major 计不满)。

        叶子 bin = 顶层非父、非失活、非开放规则自身(计其直接 course/equiv);该规则下已选定 plan
        分支递归展开的每条子规则(top 仍记为该顶层规则,用于上卷)。开放规则(E/F/A.6)走
        attribution 不进认领;父规则只聚合不直接认领。"""
        inactive = self._inactive_refs()
        bins: dict[int, dict] = {}        # bin_id -> {top, cap, codes}
        top_bins: dict[str, list] = {}    # top_ref -> [bin_id,...]
        code_bins: dict[str, list] = {}   # code -> [bin_id,...]
        top_req: dict[str, float] = {}

        def add_bin(rule: dict, top: str):
            bid = id(rule)
            codes: list = []
            for it in rule.get("items", []):           # equiv 组折叠成一个代表,口径同 _item_done_units
                k = it.get("kind")
                if k == "course" and it.get("code") in self.selected:
                    codes.append(it["code"])
                elif k == "equivalence":
                    picked = [o["code"] for o in it.get("options", [])
                              if o.get("code") in self.selected]
                    if picked:
                        codes.append(max(picked, key=lambda c: self._course_units.get(c, DEFAULT_UNITS)))
            # 按 units 降序:大学分课先填,避免小课占满有上限 bin 后大课进不来(装箱次优)
            codes = sorted(dict.fromkeys(codes),
                           key=lambda c: self._course_units.get(c, DEFAULT_UNITS), reverse=True)
            bins[bid] = {"top": top, "cap": self._units_max(rule), "codes": codes}
            top_bins.setdefault(top, []).append(bid)
            for c in codes:
                code_bins.setdefault(c, []).append(bid)

        def walk_plan(plan: dict, top: str):
            for sr in plan.get("rules", []):
                add_bin(sr, top)
                for it in sr.get("items", []):
                    if (it.get("kind") == "plan" and not self._is_self_program(it)
                            and it.get("code") in self.chosen_plans):
                        walk_plan(it, top)

        for rule in self.rules:
            ref = rule.get("ref")
            if ref in inactive or rule.get("children_refs") or self._open_rule(rule):
                continue
            add_bin(rule, ref)
            top_req[ref] = self._effective_required(rule)
            for it in rule.get("items", []):
                if (it.get("kind") == "plan" and not self._is_self_program(it)
                        and it.get("code") in self.chosen_plans):
                    walk_plan(it, ref)

        assign: dict[str, int] = {}
        bin_load: dict[int, float] = {bid: 0.0 for bid in bins}
        top_load: dict[str, float] = {top: 0.0 for top in top_bins}

        def units(c):
            return self._course_units.get(c, DEFAULT_UNITS)

        def can_add(bid, u):
            cap = bins[bid]["cap"]
            return cap is None or bin_load[bid] + u <= cap

        def place(c, bid):
            assign[c] = bid
            bin_load[bid] += units(c)
            top_load[bins[bid]["top"]] += units(c)

        def unplace(c):
            bid = assign.pop(c)
            bin_load[bid] -= units(c)
            top_load[bins[bid]["top"]] -= units(c)

        def augment(top, visited):
            """给 top 增加一门已选课(进它某条未满 cap 的 bin),必要时从别的 top 腾挪
            (腾走方掉到 req 以下时先递归补回)。"""
            for bid in top_bins[top]:
                for c in bins[bid]["codes"]:
                    if c in visited:
                        continue
                    visited.add(c)
                    owner = assign.get(c)
                    if owner is None:
                        if can_add(bid, units(c)):
                            place(c, bid)
                            return True
                        continue
                    if not can_add(bid, units(c)) or bins[owner]["top"] == top:
                        continue
                    donor = bins[owner]["top"]
                    while top_load[donor] - units(c) < top_req[donor] and augment(donor, visited):
                        pass
                    if top_load[donor] - units(c) >= top_req[donor]:
                        unplace(c)
                        place(c, bid)
                        return True
            return False

        def top_slack(top):
            codes: set = set()
            for bid in top_bins[top]:
                codes |= set(bins[bid]["codes"])
            return sum(units(c) for c in codes) - top_req[top]

        for top in sorted(top_bins, key=top_slack):    # 紧的顶层规则先达标
            while top_load[top] < top_req[top] and augment(top, set()):
                pass
        for c, locs in code_bins.items():              # 剩余已选课归并(供 over_max/总分)
            if c in assign:
                continue
            for bid in locs:
                if can_add(bid, units(c)):
                    place(c, bid)
                    break
            else:
                place(c, locs[0])
        return assign

    def _item_done_units(self, item: dict, rule_id: int | None = None) -> float:
        """某 item 已贡献的学分。course:选了即计;equivalence:选了任一选项只按一门计(取已选
        选项里 units 最大的)。传 rule_id 时只计经 _assign 归属本规则(id)的码(防跨规则重复计数);
        rule_id=None 时只看是否已选(供 equivalence 是否已满足的判断)。"""
        def mine(code):
            return code in self.selected and (
                rule_id is None or self._bin_of.get(code) == rule_id)

        k = item.get("kind")
        if k == "course":
            return _units(item) if mine(item.get("code")) else 0.0
        if k == "equivalence":
            picked = [o for o in item.get("options", []) if mine(o.get("code"))]
            return _units(max(picked, key=_units)) if picked else 0.0
        return 0.0

    def _rule_units_done(self, rule: dict) -> float:
        """一条规则内,认领归属本规则(id)的 course/equivalence 项已贡献学分之和。"""
        return sum(self._item_done_units(it, id(rule)) for it in rule.get("items", []))

    def _plan_units_done(self, plan: dict) -> float:
        """一个已选定 plan 分支:其各子规则(按 id 认领)已贡献学分之和,逐子规则按 units_max
        封顶;子规则里再嵌的已选 plan 递归计入。每门课经 _assign 唯一归属,不重复不浪费。"""
        total = 0.0
        for sr in plan.get("rules", []):
            sr_done, _ = self._capped(self._rule_units_done(sr), self._units_max(sr))
            total += sr_done
            for it in sr.get("items", []):
                if (it.get("kind") == "plan" and not self._is_self_program(it)
                        and it.get("code") in self.chosen_plans):
                    total += self._plan_units_done(it)
        return total

    def _required(self, rule_or_plan: dict) -> float:
        """规则/分支的必需学分 = units_min(None 视为 0)。"""
        m = rule_or_plan.get("units_min")
        return float(m) if m is not None else 0.0

    def _effective_required(self, rule: dict) -> float:
        """规则在「认领/松弛度」口径下的真实必需学分,与 _base_entry 的 required 保持一致。

        含 plan 项的规则:规则自身 units_min 为 None 时,其真实需求来自所选 major/minor 分支
        (from-plans 选修,如 2460 的 A.2.1:自身 None,选了 major 后需修满该 major 的 16u);
        已选分支取各分支 min 的最大,未选分支取各分支 min 的最小。规则自身有 units_min 则用它。
        无 plan 项的规则退回 _required。用于 _claim_slack / _claims 给共享课定优先级,
        否则 None 的规则会被当成「松」(slack 偏大),被同码的低 min 规则抢走应得的课。"""
        plan_items = [
            it for it in rule.get("items", [])
            if it.get("kind") == "plan" and not self._is_self_program(it)
        ]
        if not plan_items or rule.get("units_min") is not None:
            return self._required(rule)
        chosen_here = [p for p in plan_items if p.get("code") in self.chosen_plans]
        if chosen_here:
            return max(self._required(p) for p in chosen_here)
        return min((self._required(p) for p in plan_items), default=0.0)

    def _units_max(self, rule_or_plan: dict) -> float | None:
        """规则/分支的学分上限 = units_max(None 表示不封顶)。"""
        m = rule_or_plan.get("units_max")
        return float(m) if m is not None else None

    def _capped(self, done: float, cap: float | None) -> tuple[float, bool]:
        """按上限封顶:返回 (计入进度的学分, 是否超额)。cap=None 不封顶。"""
        if cap is not None and done > cap:
            return cap, True
        return done, False

    def _eval_logic(self, tree: dict | None, done_map: dict, branchable: bool = True) -> bool:
        """公式求值:leaf=该规则 done;and=全真;or=任一真。tree=None -> 全部非子规则 done。
        branchable=True(程序级公式)时,全 part 的 OR 组按 branch_state 选定分支求值
        (UI 二选一,如 B OR C);branchable=False(子规则内部公式,如 B.1 OR B.2)时,
        OR 一律取 any-of —— 子规则内部 OR 不暴露为可切换分支组,不应依赖 branch_state。"""
        if tree is None:
            return all(v for k, v in done_map.items() if k not in self._child_of)
        op = tree.get("op")
        if op == "part":
            return bool(done_map.get(tree["ref"], False))
        kids = tree.get("children", [])
        if op == "and":
            return all(self._eval_logic(c, done_map, branchable) for c in kids)
        if op == "or":
            if branchable and all(c.get("op") == "part" for c in kids):
                chosen = self.branch_state().get("|".join(c["ref"] for c in kids))
                return bool(done_map.get(chosen, False))
            return any(self._eval_logic(c, done_map, branchable) for c in kids)
        return False

    def _logic_refs(self, tree: dict | None) -> set:
        """取出已解析公式里引用的全部规则 ref。用于丢弃引用了非本规则子规则的畸形公式
        (官方数据偶有笔误,如把 A.1.3 误写成 A.2.3),避免该规则永远判不出 done。"""
        if not tree:
            return set()
        if tree.get("op") == "part":
            return {tree["ref"]}
        out: set = set()
        for c in tree.get("children", []):
            out |= self._logic_refs(c)
        return out

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
        self._bin_of = self._assign()
        entries: dict[str, dict] = {}
        for rule in self.rules:
            if rule.get("children_refs"):
                continue                              # 父规则后算(依赖子 entry)
            entries[rule.get("ref")] = self._base_entry(rule, att, inactive)
        parents = [r for r in self.rules if r.get("children_refs")]
        parents.sort(key=lambda r: self._rule_depth(r.get("ref")), reverse=True)
        for rule in parents:
            entries[rule.get("ref")] = self._parent_entry(rule, entries, inactive)
        out = [entries[r.get("ref")] for r in self.rules]
        picker = self._picker_rule()
        if picker is not None:
            out = self._surface_picker(picker, out)
        return out

    def _rule_depth(self, ref) -> int:
        """规则在子规则树中的深度(顶层=0)。父规则按深度从深到浅计算,
        确保算某父规则前其子规则(含本身也是父规则的中间节点)已先算好,
        否则父规则会漏掉尚未计算的子父规则的 counted。"""
        d, cur, seen = 0, ref, set()
        while self._child_of.get(cur) and cur not in seen:
            seen.add(cur)
            cur = self._child_of[cur]
            d += 1
        return d

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
        if sub and self._logic_refs(sub) - {k["ref"] for k in kids}:
            sub = None
        kids_ok = (self._eval_logic(sub, {k["ref"]: k["done"] for k in kids}, branchable=False)
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

    def _base_entry(self, rule: dict, att: dict, inactive: set) -> dict:
        ref = rule.get("ref")
        title = rule.get("title") or ""
        select_type = rule.get("select_type")
        required = self._required(rule)
        units_max = self._units_max(rule)
        if self._open_rule(rule):                     # E/F:进度=归属到本规则的计划外课
            done_units = sum(self._course_units.get(c, DEFAULT_UNITS)
                             for c, r2 in att["assigned"].items() if r2 == ref)
        else:
            done_units = self._rule_units_done(rule)

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
            # 必需学分:规则自身有 units_min 就用它(如 from-plans 选修 A.3.2 自身=0,父规则
            # 才是真实需求);仅当规则自身无 units_min 时才退回 plan 的 min(择一修满一个分支:
            # 已选取该分支 min,未选取各分支最小 min)。
            if chosen_here:
                if rule.get("units_min") is None:
                    required = max(self._required(p) for p in chosen_here)
                done_units += sum(self._plan_units_done(p) for p in chosen_here)
            elif rule.get("units_min") is None:
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

    # ---------- 单顶层 plan-picker:子规则上浮 ----------
    def _picker_rule(self) -> dict | None:
        """「整学位=选一个 field/plan」程序的判定:顶层只有一条规则、它是带子规则 plan 的选择器
        (如 5528 MEngSc、2031 BSc Honours)。这类程序选定 field 后,真正的课组(弹性必修 /
        研究项目 / 各类选修)全在 plan 内部,需上浮成可见进度行,否则结构全塌进一条「0/16」。
        多顶层规则的程序(如 2559,major 只是规则之一)不在此列,维持 major 卷入单条规则的旧语义。"""
        if len(self.rules) != 1:
            return None
        rule = self.rules[0]
        if rule.get("children_refs"):
            return None
        plans = [it for it in rule.get("items", [])
                 if it.get("kind") == "plan" and not self._is_self_program(it)]
        if plans and any(p.get("rules") for p in plans):
            return rule
        return None

    def _chosen_field_plans(self, picker: dict) -> list:
        """picker 规则下已选定、且带子规则的 field/plan 分支(择一,通常 0 或 1 个)。"""
        return [it for it in picker.get("items", [])
                if it.get("kind") == "plan" and not self._is_self_program(it)
                and it.get("code") in self.chosen_plans and it.get("rules")]

    def _surface_picker(self, picker: dict, out: list) -> list:
        """把已选 field 的子规则上浮为顶层规则 picker 的子行(child_of=picker.ref)。
        picker 自身改成 parent 口径:进度 = 顶层子规则计入之和(按 plan 学分上限封顶),
        done 还要求各子规则 done 且 plan 级 level 下限满足(student-facing 安全)。未选 field 时原样返回。"""
        parent_ref = picker.get("ref")
        chosen = self._chosen_field_plans(picker)
        if not chosen:
            return out
        parent_entry = out[0]
        children: list = []
        top_refs: list = []
        plan_cap = 0.0
        for plan in chosen:
            ents, tops = self._subrule_entries(plan.get("rules") or [], parent_ref)
            children += ents
            top_refs += tops
            plan_cap += self._units_max(plan) or self._required(plan)
        counted_raw = sum(e["units_done"] for e in children
                          if e.get("child_of") == parent_ref)
        counted = sum(e["units_counted"] for e in children
                      if e.get("child_of") == parent_ref)
        cap = plan_cap or None
        counted_capped, over_max = self._capped(counted, cap)
        required = parent_entry["units_required"]
        kids_ok = all(e["done"] for e in children if e.get("child_of") == parent_ref)
        floors_ok, _caps = self._plan_level_aux(chosen)
        parent_entry["children_refs"] = top_refs
        parent_entry["units_max"] = cap
        parent_entry["units_done"] = counted_raw
        parent_entry["units_counted"] = counted_capped
        parent_entry["over_max"] = over_max
        parent_entry["done"] = counted_capped >= required and kids_ok and floors_ok
        parent_entry["remaining"] = max(required - counted_capped, 0.0)
        return [parent_entry] + children

    def _subrule_entries(self, subs: list, parent_ref: str) -> tuple[list, list]:
        """已选 field 的扁平子规则列表 -> (上浮 entry 列表, 顶层子规则的命名空间 ref 列表)。
        ref 命名空间化为「父ref.子ref」(如 A.A / A.E.1),避免与顶层 ref 冲突、且沿用点号子规则约定。"""
        def ns(r: str) -> str:
            return f"{parent_ref}.{r}"

        child_of_local = {ch: r.get("ref") for r in subs
                          for ch in (r.get("children_refs") or [])}
        entries: dict[str, dict] = {}
        for sr in subs:
            if sr.get("children_refs"):
                continue
            entries[sr.get("ref")] = self._subrule_base_entry(
                sr, parent_ref, ns, child_of_local)
        parents = sorted((sr for sr in subs if sr.get("children_refs")),
                         key=lambda s: self._local_depth(s.get("ref"), child_of_local),
                         reverse=True)
        for sr in parents:
            entries[sr.get("ref")] = self._subrule_parent_entry(
                sr, entries, parent_ref, ns, child_of_local)
        ordered = [entries[sr.get("ref")] for sr in subs]
        top_refs = [ns(sr.get("ref")) for sr in subs
                    if not child_of_local.get(sr.get("ref"))]
        return ordered, top_refs

    def _local_depth(self, ref, child_of_local: dict) -> int:
        d, cur, seen = 0, ref, set()
        while child_of_local.get(cur) and cur not in seen:
            seen.add(cur)
            cur = child_of_local[cur]
            d += 1
        return d

    def _subrule_base_entry(self, sr: dict, parent_ref: str, ns, child_of_local: dict) -> dict:
        co = child_of_local.get(sr.get("ref"))
        required = self._required(sr)
        units_max = self._units_max(sr)
        done_units = self._rule_units_done(sr)
        counted, over_max = self._capped(done_units, units_max)
        entry = {
            "ref": ns(sr.get("ref")), "title": sr.get("title") or "",
            "select_type": sr.get("select_type"),
            "units_required": required, "units_max": units_max,
            "units_done": done_units, "units_counted": counted, "over_max": over_max,
            "done": counted >= required, "remaining": max(required - counted, 0.0),
            "inactive": False, "child_of": ns(co) if co else parent_ref,
        }
        if self._open_rule(sr):                   # 罕见(2052 BA Honours 1 条):标开放 + 兜底计数
            entry["open"] = True
            entry["open_scope"] = "any" if sr.get("items") else "program"
            entry["open_max_level"] = self._open_level_cap(sr)
            leftover = self._subrule_leftover_units(sr)
            entry["units_done"] = leftover
            entry["units_counted"], entry["over_max"] = self._capped(leftover, units_max)
            entry["done"] = entry["units_counted"] >= required
            entry["remaining"] = max(required - entry["units_counted"], 0.0)
        return entry

    def _subrule_parent_entry(self, sr: dict, entries: dict, parent_ref: str,
                              ns, child_of_local: dict) -> dict:
        co = child_of_local.get(sr.get("ref"))
        required = self._required(sr)
        units_max = self._units_max(sr)
        kids = [entries[ch] for ch in sr.get("children_refs", []) if ch in entries]
        raw = sum(k["units_done"] for k in kids)
        counted, over_max = self._capped(sum(k["units_counted"] for k in kids), units_max)
        sub = parse_rule_logic(sr.get("rule_logic"))
        if sub and self._logic_refs(sub) - set(sr.get("children_refs", [])):
            sub = None
        kids_ok = (self._eval_logic(
            sub, {ch: entries[ch]["done"] for ch in sr.get("children_refs", [])
                  if ch in entries}, branchable=False)
            if sub else all(k["done"] for k in kids))
        return {
            "ref": ns(sr.get("ref")), "title": sr.get("title") or "",
            "select_type": sr.get("select_type"),
            "children_refs": [ns(ch) for ch in sr.get("children_refs", [])],
            "units_required": required, "units_max": units_max,
            "units_done": raw, "units_counted": counted, "over_max": over_max,
            "done": counted >= required and kids_ok,
            "remaining": max(required - counted, 0.0),
            "inactive": False, "child_of": ns(co) if co else parent_ref,
        }

    def _subrule_leftover_units(self, sr: dict) -> float:
        """开放子规则的兜底学分:已选课里未被任何具体子规则(_bin_of)认领、且符合开放范围的码之和。
        范围:有 items(wildcard)=任意有效课;无 items(空表)=程序课表内;notes 标 undergraduate 的限 level<=6。"""
        prog_list = self._all_referenced_codes()
        wild = bool(sr.get("items"))
        cap_lv = self._open_level_cap(sr)
        total = 0.0
        for code in self.selected:
            if code in self._bin_of or code in self.excluded or code not in self._all_codes:
                continue
            if not wild and code not in prog_list:
                continue
            m = re.search(r"\d", code)
            lvl = int(m.group()) if m else None
            if cap_lv is not None and lvl is not None and lvl > cap_lv:
                continue
            total += self._course_units.get(code, DEFAULT_UNITS)
        return total

    # ---------- plan 级 level 约束(下限门控 / 子规则上限告警) ----------
    def _plan_level_aux(self, chosen: list) -> tuple[bool, list]:
        """已选 field 的 level 约束求值。返回 (下限是否全满足, [状态 dict ...])。
        level_min(如「Selected courses must include at least 8 units at level 7」)= 整 field 范围,
        门控 field done(student-facing 安全:漏修 level 7 会无法毕业);
        level_max(如 group D「at most 4 units at level 4」)= 该子规则范围,仅告警不挡 done。
        aux 数据由 scraper 抓 plan/group 的 auxiliaryRules、随 rules 树入库;缺则返回 (True, [])。"""
        def units_at(codes, level, or_higher) -> float:
            tot = 0.0
            for c in codes:
                m = re.search(r"\d", c)
                if not m:
                    continue
                lv = int(m.group())
                if lv == level or (or_higher and lv > level):
                    tot += self._course_units.get(c, DEFAULT_UNITS)
            return tot

        field_codes = [c for c in self.selected if c in self._all_codes]
        out: list = []
        for plan in chosen:
            for a in plan.get("aux_rules") or []:
                out.append(self._aux_status(
                    a, units_at(field_codes, a["level"], a.get("or_higher")), "field"))
            for sr in plan.get("rules") or []:
                sr_codes = [c for c in self.selected if self._bin_of.get(c) == id(sr)]
                for a in sr.get("aux_rules") or []:
                    if a["kind"] == "level_min":
                        scope, base = "field", field_codes
                    else:
                        scope, base = f"sub:{sr.get('ref')}", sr_codes
                    out.append(self._aux_status(
                        a, units_at(base, a["level"], a.get("or_higher")), scope))
        floors_ok = all(s["satisfied"] for s in out if s["kind"] == "level_min")
        return floors_ok, out

    @staticmethod
    def _aux_status(a: dict, used: float, scope: str) -> dict:
        units = float(a.get("units") or 0)
        if a.get("kind") == "level_min":
            return {"kind": "level_min", "level": a.get("level"), "min_units": units,
                    "used": used, "or_higher": bool(a.get("or_higher")),
                    "under": used < units, "satisfied": used >= units,
                    "scope": scope, "text": a.get("text", "")}
        return {"kind": "level_max", "level": a.get("level"), "max_units": units,
                "used": used, "over": used > units, "satisfied": used <= units,
                "scope": scope, "text": a.get("text", "")}

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
        """每条「未收敛」顶层规则 -> 可选 slot 列表(course / equiv 二选一)。
        单顶层 plan-picker 选定 field 后,改按上浮的命名空间子规则(如 A.A/A.B)分组,
        不再把整 field 课程平铺到 picker 一个键下。"""
        seen: set[str] = set()
        st = {e["ref"]: e for e in self.status()}
        out: dict[str, list] = {}
        picker = self._picker_rule()
        if picker is not None and self._chosen_field_plans(picker):
            parent_ref = picker.get("ref")
            for plan in self._chosen_field_plans(picker):
                for sr in plan.get("rules") or []:
                    if sr.get("children_refs"):
                        continue
                    ref_ns = f"{parent_ref}.{sr.get('ref')}"
                    e = st.get(ref_ns)
                    if not e or e.get("inactive") or self._closed(e):
                        continue
                    slots = self._slots_for_rule(sr, seen)
                    if slots:
                        out[ref_ns] = slots
            return out
        for rule in self._ordered_rules():
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
        picker = self._picker_rule()
        if picker is not None and self._chosen_field_plans(picker):
            parent_ref = picker.get("ref")
            for plan in self._chosen_field_plans(picker):
                for sr in plan.get("rules") or []:
                    if sr.get("children_refs"):
                        continue
                    picked = self._selected_in_rule(sr, seen)
                    if picked:
                        out[f"{parent_ref}.{sr.get('ref')}"] = picked
            return out
        for rule in self._ordered_rules():
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
        """程序级 level 约束的实时状态。

        每条:{level, max_units / min_units, used(已选该级别学分), over / under, scope, text}。
        含三类:program(程序级 aux_rules level cap)、electives(选修范围 notes cap)、
        以及单顶层 plan-picker 选定 field 后的 plan/group 级 level 约束(下限 level_min + 组内上限 level_max)。
        级别 = 课码第一个数字(CSSE7100 -> 7)。无任何约束时返回 []。
        """
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
            out.append({"kind": "level_max", "level": cap["level"],
                        "max_units": cap["max_units"], "used": used,
                        "over": used > cap["max_units"], "satisfied": used <= cap["max_units"],
                        "scope": "program", "text": cap["text"]})
        if self.elective_caps:                        # 选修范围:扣掉核心组与已选 major 的课
            elect_used = used_map(self._elective_selected())
            for cap in self.elective_caps:
                used = elect_used.get(cap["level"], 0.0)
                out.append({"kind": "level_max", "level": cap["level"],
                            "max_units": cap["max_units"], "used": used,
                            "over": used > cap["max_units"], "satisfied": used <= cap["max_units"],
                            "scope": "electives", "text": cap["text"]})
        picker = self._picker_rule()                  # plan-picker:field 的 level 下限 / 组内上限
        if picker is not None:
            chosen = self._chosen_field_plans(picker)
            if chosen:
                if not self._bin_of:
                    self._bin_of = self._assign()
                _floors_ok, aux = self._plan_level_aux(chosen)
                out += aux
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
