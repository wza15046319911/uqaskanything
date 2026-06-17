"""
simulator.py — stage 7: course-planning simulator (deterministic state machine)

Driven by the programs.rules rule tree: after the user adds/removes selected courses or picks a major/minor branch,
each top-level rule's progress (status) and "what can still be picked" (available) change in real time.
Prerequisites are not considered for now; all progress judgments are pure deterministic code, no LLM call.

Rule-tree nodes (see programs.rules in the DB):
  top-level rule {ref,title,select_type:'all'|'select',units_min,units_max,items:[...]}
  item.kind:
    - course      {code,name,units}
    - equivalence {options:[{code,name,units},...]}   picking any one satisfies this "item" units
    - plan        {code,name,subtype,units_min,units_max,rules:[...recursive...]}  major/minor branch
    - wildcard    any course (not bound to a specific code, available does not enumerate it)

Progress conventions:
  - select_type='all' : required group, units_required = units_min, done once met.
  - select_type='select': elective group, units_required = units_min (may be 0/None=0), done once met.
  - units_max (if any): elective group units cap. Units over units_max do not count as effective progress,
    counted progress is capped at min(done_units, units_max); the raw done_units is still exposed, with an over_max flag.
  - an equivalence group counts as "one fillable item": picking any one option counts only that one's units once (no repeat accumulation).
  - plan rule: status lists the selectable plan branches. This module implements **select-one** semantics:
    multiple branches under one plan rule are mutually exclusive, calling choose_plan again replaces the old pick in the same rule;
    rule required units = "complete 1 branch" (the chosen branch's units_min, or the smallest units_min among branches if none chosen).
    Note: if a program's plan rule is really select-many (multiple majors at once), confirm and adjust separately.
  - units default to item.units per course, falling back to 2 if missing.
  - self-reference: the rules tree occasionally embeds a "whole degree" node (code == program_id or subtype contains 'Program'),
    which is not a selectable major/minor branch; indexing and recursion always skip it, to avoid infinite recursion and mistaking the whole degree for a branch.

Usage:
    python simulator.py            # run program_id=2559 self-test against the real DB
"""
from __future__ import annotations
import os
import re

import psycopg

from app.core.config import DSN

DEFAULT_UNITS = 2.0


def _units(item: dict) -> float:
    """Get the units of an item (course or equivalence option), use the default if missing."""
    u = item.get("units")
    return float(u) if u is not None else DEFAULT_UNITS


def satisfied(tree: dict | None, selected: set) -> tuple[bool, str | None]:
    """Whether the prerequisite tree is satisfied by selected. Returns (ok, reason); a non-empty reason means locked or warning.

    Soft-gate convention: tree=None (no prerequisite) -> satisfied; op=raw (unparseable) -> satisfied but with a warning (never hard-block).
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
    """Program-level boolean formula -> tree. "Part A AND ( Part B OR Part C )" ->
    {"op":"and"|"or","children":[...]} | {"op":"part","ref":"A"}.
    Any unrecognized leftover character or reduction failure -> None (caller falls back to AND-all, never invents structure).
    Precedence: AND binds tighter than OR, parentheses highest (UQ formulas always have parentheses in practice)."""
    if not s:
        return None
    s = re.sub(r"^\s*(AND|OR)\s+", "", s, flags=re.I)   # tolerance: official data sometimes has a stray connective at the start
    toks = re.findall(r"\(|\)|\bAND\b|\bOR\b|Part\s+[\w.\-]+", s, re.I)
    toks = [t.upper() if t.upper() in ("AND", "OR") else t for t in toks]
    if re.sub(r"\(|\)|\bAND\b|\bOR\b|Part\s+[\w.\-]+|\s+", "", s, flags=re.I):
        return None                                  # leftover beyond tokens, refuse to parse
    pos = 0

    def expr():                                      # OR layer
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

    def term():                                      # AND layer
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
    """All part refs referenced by the formula tree."""
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
    """Course-planning progress state machine for a single program.

    Public methods:
      select(code) / deselect(code)  —— add/remove a selected course
      choose_plan(plan_code)         —— pick a major/minor branch
      status() -> list               —— progress of each top-level rule
      available() -> list            —— course codes not yet picked and belonging to an unfinished rule
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
        self.rules = row[2] or []          # top-level rule list (JSONB already deserialized to list[dict])
        self.selected: set[str] = set()     # selected course codes
        self.chosen_plans: set[str] = set()  # chosen plan branch codes
        self._bin_of: dict[str, int] = {}    # code -> claimed leaf-rule id (filled by _assign during status())

        # program-level banned courses (No credit will be given for…): removed from the available list. Empty set if table not built (no error).
        self.excluded: set[str] = set()
        if conn.execute("SELECT to_regclass('program_exclude')").fetchone()[0]:
            self.excluded = {
                r[0] for r in conn.execute(
                    "SELECT course_code FROM program_exclude WHERE program_id = %s",
                    (program_id,)).fetchall()
            }

        # prerequisites (stage 3b): code -> parsed tree (only real tree/raw loaded). Empty if column not migrated/backfilled (soft gate degrades to all-unlocked).
        # exclude jsonb 'null' (truly no prerequisite) and SQL NULL (not backfilled): both "not in _prereq" are treated as unlocked.
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

        # program-level units cap per level (level_cap in aux_rules, e.g. "at most 24 units at level 1").
        # data-driven: parsed from programs.aux_rules; empty if column/data missing (no error).
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

        # program-level boolean formula (e.g. "Part A AND ( Part B OR Part C ) AND ...").
        # column missing/value empty -> no formula (keep the AND-all original semantics); if the formula parses but
        # references a non-existent ref -> also fall back to AND-all, but expose logic_fallback=True explicitly (not silent).
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
        # OR-group branch choice: group_key("B|C") -> chosen ref; default to the first in the group if none chosen (e.g. B=Major)
        self.branch: dict[str, str] = {}
        # SubRule parent-child index (e.g. C -> [C.1, C.2])
        self._child_of: dict[str, str] = {
            ch: r["ref"] for r in self.rules
            for ch in (r.get("children_refs") or []) if r.get("ref")
        }

        # whole-DB course units: open rules (E/F) can count out-of-tree codes, attribution and validation both query this
        self._course_units: dict[str, float] = {
            r[0]: (float(r[1]) if r[1] is not None else DEFAULT_UNITS)
            for r in conn.execute(
                "SELECT DISTINCT ON (code) code, units FROM courses ORDER BY code"
            ).fetchall()
        }
        self._all_codes = set(self._course_units)

        # cross-program "Program-type reference" expansion: replace an empty plan reference pointing to a whole program (e.g. 5257 A.4
        # "2u from MCyberSec program electives", 2560 B.3 referencing 5257) in place with the referenced
        # program's course pool. Must run before _index_plans (after expansion these references are no longer selectable majors).
        self._expand_program_refs(conn)

        # elective-scope level cap (rule notes: "no more than N units at level L", e.g. 2559 elective L1<=14)
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

        # prebuilt index: plan_code -> plan node, for easy expansion after choose_plan
        self._plans: dict[str, dict] = {}
        # plan_code -> group key: branches under the same plan rule share one key, used for "select-one" exclusivity
        self._plan_group: dict[str, int] = {}
        self._index_plans(self.rules)

    # ---------- internal: self-reference check ----------
    def _is_self_program(self, node: dict) -> bool:
        """Decide whether a plan node is a "whole degree" self-reference node (not a selectable branch).

        Match condition (any): code equals this program_id; or the subtype text contains 'Program'.
        Such a node (e.g. 2559's {code='2559', subtype='Undergraduate Program'}) just nests the
        whole degree one more layer; it is neither a selectable major/minor nor valid, and causes recursive self-reference, so skip it uniformly.
        """
        if node.get("code") == self.program_id:
            return True
        subtype = node.get("subtype") or ""
        return "Program" in subtype

    # ---------- internal: cross-program Program reference expansion ----------
    @staticmethod
    def _is_program_ref(it: dict) -> bool:
        """Whether this is a "Program-type" plan reference (pointing to a whole program, not a major/minor branch):
        code is a pure-digit program_id, or the subtype text contains 'Program', and rules are not expanded (empty)."""
        if it.get("kind") != "plan" or it.get("rules"):
            return False
        code = str(it.get("code") or "")
        return code.isdigit() or "Program" in (it.get("subtype") or "")

    def _program_pools(self, conn, codes: set) -> dict:
        """Course pool of the referenced program: {program_code: set(course_codes)}. Only collect direct course/equiv
        codes and drill into normal major/minor plans, stop at a Program-type reference (prevent self-reference / mutual-reference cycles)."""
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
        """Replace Program-type references in place with the referenced program's course pool (course items).

        self-ref (code==program_id) = this program's own course pool; cross-ref likewise references another program.
        If the referenced program is not in the DB or its pool is empty -> the reference stays as-is (downstream still skips it as self-reference),
        and is recorded in self.unresolved_program_refs (not silent). Must be called after _course_units and before _index_plans."""
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
            """The course/equiv codes explicitly listed in a set of rules (all sub-rules of a plan)."""
            acc: set = set()
            for r in rules or []:
                for it in r.get("items", []):
                    acc.update(self._item_codes(it))
                    if it.get("kind") == "plan" and it.get("rules"):
                        acc |= plan_codes(it["rules"])
            return acc

        def walk_replace(rules, exclude: set):
            """exclude: course codes already explicitly listed/expanded within the same plan. The top-level call passes empty exclude
            (top-level elective rules keep the full pool, cross-rule dedup is left to _claims); when entering a plan, exclude takes that
            plan's explicit course codes, to avoid "in-major program electives" re-listing the major's required courses and causing _plan_units_done
            to double-count. After expanding one reference, merge the used codes into exclude, to prevent multiple references within the same plan re-listing them."""
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

    # ---------- internal: indexing ----------
    def _index_plans(self, rules: list, visited: set | None = None) -> None:
        """Recursively collect all selectable plan branches (including nested sub-plans); skip self-reference whole-degree nodes."""
        if visited is None:
            visited = set()
        for r in rules:
            # all plan branches under the same rule r share one group key (identified by the rule object's id)
            group = id(r)
            for it in r.get("items", []):
                if it.get("kind") != "plan":
                    continue
                if self._is_self_program(it):  # self-reference whole-degree node is not collected as a selectable branch
                    continue
                code = it.get("code")
                if code:
                    if code in visited:  # prevent recursive self-reference from re-expanding same-code nodes
                        continue
                    visited.add(code)
                    self._plans[code] = it
                    self._plan_group[code] = group
                self._index_plans(it.get("rules", []), visited)

    # ---------- add/remove selection ----------
    def select(self, code: str) -> None:
        self.selected.add(code)

    def deselect(self, code: str) -> None:
        self.selected.discard(code)

    def choose_plan(self, plan_code: str) -> None:
        # a self-reference whole-degree node (e.g. program_id itself) is not a selectable branch, reject directly
        if plan_code == self.program_id or plan_code not in self._plans:
            raise ValueError(f"plan 分支不存在: {plan_code!r}(可选: {sorted(self._plans)})")
        # select-one semantics: mutually exclusive within the same plan rule, first clear the chosen branch in the same group, then pick the new one
        group = self._plan_group.get(plan_code)
        if group is not None:
            same_group = {c for c in self.chosen_plans if self._plan_group.get(c) == group}
            self.chosen_plans -= same_group
        self.chosen_plans.add(plan_code)

    # ---------- OR branch (program-level formula, e.g. 2559's Major(B) / No-Major(C) pick-one) ----------
    def branch_groups(self) -> list[list[str]]:
        """Switchable OR groups in the formula (or nodes whose children are all part).
        An OR mixed with compound sub-expressions is not switchable (all treated as active, reported separately by sweep)."""
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
        """The currently chosen branch of each OR group (default to the first in the group if not explicitly chosen, i.e. B=Major for 2559)."""
        return {"|".join(g): self.branch.get("|".join(g), g[0])
                for g in self.branch_groups()}

    def _inactive_refs(self) -> set:
        """Unchosen OR branches and all their sub-rules (inactive: no progress counted, not offered, courses flow into open rules)."""
        out: set = set()
        for key, sel in self.branch_state().items():
            out |= {ref for ref in key.split("|") if ref != sel}
        grew = True
        while grew:                                  # cascade sub-rules (C inactive -> C.1/C.2 inactive)
            grew = False
            for r in self.rules:
                if r.get("ref") in out:
                    for ch in r.get("children_refs") or []:
                        if ch not in out:
                            out.add(ch)
                            grew = True
        return out

    # ---------- open rules and attribution of out-of-plan courses ----------
    def _open_rule(self, rule: dict) -> bool:
        """Open rule: select type, no enumerable items (empty items = pick any within the program course list, e.g. E; only wildcard = any course,
        e.g. F / 2557 A.6; only a self-reference whole-degree plan = pick any within the program, e.g. 2455 J 'BE(Hons) Program
        Elective Courses'). Progress comes from attribution(), not from items.
        When units_max is None: only open up "pure wildcard" free electives (any course, e.g. A.6 'General Elective');
        an empty-list rule (E) or self-reference rule with no cap would become an infinite sink, so do not open it."""
        if rule.get("select_type") != "select" or rule.get("children_refs"):
            return False
        items = rule.get("items", [])
        if items and all(
            it.get("kind") == "plan" and self._is_self_program(it) for it in items
        ):
            return rule.get("units_max") is not None
        if not all(it.get("kind") == "wildcard" for it in items):
            return False
        if rule.get("units_max") is None:
            return bool(items)
        return True

    def _enum_codes(self, rule: dict) -> set:
        """All course codes enumerated by the rule (course + all equivalence options; recurses into chosen plan branches)."""
        out: set = set()
        for it in rule.get("items", []):
            out |= set(self._item_codes(it))
            if (it.get("kind") == "plan" and not self._is_self_program(it)
                    and it.get("code") in self.chosen_plans):
                for sr in it.get("rules", []):
                    out |= self._enum_codes(sr)
        return out

    def _open_level_cap(self, rule: dict) -> int | None:
        """Course-level cap of an open rule: if notes state the undergraduate course list (e.g. F), limit to level<=6."""
        return 6 if "undergraduate" in (rule.get("notes") or "").lower() else None

    def attribution(self) -> dict:
        """Deterministic attribution of out-of-plan selected codes: {"assigned": {code: ref}, "unattributed": [code,...]}.

        Codes consumed by active enumerated rules do not take part; remaining codes (sorted, deterministic) try open rules in rule-tree order:
        an empty-list rule (E) is limited to "within the program course list" (all enumerated codes in the tree), a wildcard rule (F) takes any valid code
        (limited to level<=6 if notes mark undergraduate); a single rule stops once filled to units_max.
        Banned by the program / not in the courses DB / nowhere to go -> unattributed."""
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
                mx = self._units_max(r)            # None = no cap (pure wildcard free elective)
                if mx is not None and fill[ref] + u > mx:
                    continue
                assigned[code] = ref
                fill[ref] += u
                break
            else:
                unattributed.append(code)
        return {"assigned": assigned, "unattributed": unattributed}

    # ---------- progress calculation ----------
    def _item_codes(self, item: dict) -> list[str]:
        """All course codes involved in an item (course=itself; equivalence=all options; plan/wildcard=empty)."""
        k = item.get("kind")
        if k == "course":
            return [item["code"]] if item.get("code") else []
        if k == "equivalence":
            return [o["code"] for o in item.get("options", []) if o.get("code")]
        return []

    def _claimable_codes(self, rule: dict) -> list:
        """All course codes a rule can claim (course + each equivalence option + courses of chosen plan branches),
        aligned with the counting convention (compare _claims/_rule_units_done). Not filtered by whether selected."""
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
        """A rule's slack = available units - required units (parent rules / rules with no enumerable course do not claim, recorded as +inf).
        The smaller the slack the "tighter" (required, or options just barely enough), the higher the priority to claim shared courses."""
        if rule.get("children_refs"):
            return float("inf")
        codes = set(self._claimable_codes(rule))
        avail = sum(self._course_units.get(c, DEFAULT_UNITS) for c in codes)
        return max(0.0, avail - self._effective_required(rule))

    def _ordered_rules(self) -> list:
        """Claim order: by ascending slack (tight/required rules claim shared courses first), keep original tree order for equal slack.
        Only used for dedup allocation of shared courses; each caller's output is mapped by ref, unrelated to UI display order."""
        return [r for _, r in sorted(enumerate(self.rules),
                                     key=lambda ir: (self._claim_slack(ir[1]), ir[0]))]

    def _direct_codes(self, rule: dict) -> list:
        """The course/equivalence codes the rule directly lists (does not drill into plan; plan forms its own bin)."""
        out: list = []
        for it in rule.get("items", []):
            if it.get("kind") in ("course", "equivalence"):
                out += self._item_codes(it)
        return out

    def _assign(self) -> dict:
        """Global leaf-level claiming: assign each selected course uniquely to one "leaf bin", each bin not exceeding its own units_max,
        do augmenting matching by top-level rule tightness, so as many top-level rules as possible reach _effective_required. Returns code -> bin_id
        (bin_id = id(leaf rule dict)). The counting layer uses this to decide which rule each course counts in — replacing the old two-layer split of
        "top-level only claims, plan-internal sub-rules each cap and sum" (the latter both double-counts across sub-rules and piles courses into a capped
        sub-rule that truncates them while other sub-rules sit empty, leaving the major undercounted).

        Leaf bin = a top-level non-parent, non-inactive, non-open rule itself (counts its direct course/equiv); each sub-rule recursively expanded from a chosen plan
        branch under that rule (top is still recorded as that top-level rule, for roll-up). Open rules (E/F/A.6) go through
        attribution and do not claim; parent rules only aggregate and do not claim directly."""
        inactive = self._inactive_refs()
        bins: dict[int, dict] = {}        # bin_id -> {top, cap, codes}
        top_bins: dict[str, list] = {}    # top_ref -> [bin_id,...]
        code_bins: dict[str, list] = {}   # code -> [bin_id,...]
        top_req: dict[str, float] = {}

        def add_bin(rule: dict, top: str):
            bid = id(rule)
            codes: list = []
            for it in rule.get("items", []):           # equiv group folds into one representative, same convention as _item_done_units
                k = it.get("kind")
                if k == "course" and it.get("code") in self.selected:
                    codes.append(it["code"])
                elif k == "equivalence":
                    picked = [o["code"] for o in it.get("options", [])
                              if o.get("code") in self.selected]
                    if picked:
                        codes.append(max(picked, key=lambda c: self._course_units.get(c, DEFAULT_UNITS)))
            # by descending units: fill high-units courses first, to avoid small courses filling a capped bin and blocking big ones (suboptimal packing)
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
            """Add one selected course to top (into one of its bins not yet at cap), moving from another top if needed
            (if the donor drops below req, recursively refill it first)."""
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

        for top in sorted(top_bins, key=top_slack):    # tight top-level rules reach target first
            while top_load[top] < top_req[top] and augment(top, set()):
                pass
        for c, locs in code_bins.items():              # merge remaining selected courses (for over_max / total)
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
        """Units already contributed by an item. course: counted once selected; equivalence: if any option is selected counts only one (the selected
        option with the largest units). When rule_id is passed, only count codes assigned to this rule (id) by _assign (prevents cross-rule double-counting);
        when rule_id=None, only check whether selected (for judging whether an equivalence is satisfied)."""
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
        """Within a rule, the sum of units contributed by course/equivalence items claimed to this rule (id)."""
        return sum(self._item_done_units(it, id(rule)) for it in rule.get("items", []))

    def _plan_units_done(self, plan: dict) -> float:
        """A chosen plan branch: the sum of units contributed by its sub-rules (claimed by id), each sub-rule capped by units_max;
        a chosen plan nested inside a sub-rule is counted recursively. Each course is uniquely assigned by _assign, no repeat, no waste."""
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
        """Required units of a rule/branch = units_min (None treated as 0)."""
        m = rule_or_plan.get("units_min")
        return float(m) if m is not None else 0.0

    def _effective_required(self, rule: dict) -> float:
        """The rule's true required units under the "claim/slack" convention, consistent with _base_entry's required.

        Rule with plan items: when the rule's own units_min is None, its true requirement comes from the chosen major/minor branch
        (from-plans elective, e.g. 2460's A.2.1: itself None, after choosing a major must complete that major's 16u);
        chosen branch takes the max of branch mins, unchosen takes the min of branch mins. If the rule has its own units_min, use it.
        A rule with no plan items falls back to _required. Used by _claim_slack / _claims to prioritize shared courses,
        otherwise a None rule would be treated as "loose" (larger slack) and lose its due courses to a same-code lower-min rule."""
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
        """Units cap of a rule/branch = units_max (None means no cap)."""
        m = rule_or_plan.get("units_max")
        return float(m) if m is not None else None

    def _capped(self, done: float, cap: float | None) -> tuple[float, bool]:
        """Cap by limit: returns (units counted toward progress, whether over). cap=None means no cap."""
        if cap is not None and done > cap:
            return cap, True
        return done, False

    def _eval_logic(self, tree: dict | None, done_map: dict, branchable: bool = True) -> bool:
        """Formula evaluation: leaf=that rule's done; and=all true; or=any true. tree=None -> all non-sub-rules done.
        When branchable=True (program-level formula), an all-part OR group is evaluated by branch_state's chosen branch
        (UI pick-one, e.g. B OR C); when branchable=False (sub-rule internal formula, e.g. B.1 OR B.2),
        OR is always any-of — an internal sub-rule OR is not exposed as a switchable branch group and should not depend on branch_state."""
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
        """Extract all rule refs referenced in a parsed formula. Used to discard a malformed formula referencing a non-sub-rule
        (official data has occasional typos, e.g. writing A.1.3 as A.2.3), to avoid the rule never reaching done."""
        if not tree:
            return set()
        if tree.get("op") == "part":
            return {tree["ref"]}
        out: set = set()
        for c in tree.get("children", []):
            out |= self._logic_refs(c)
        return out

    def status(self) -> list:
        """Progress of each top-level rule.

        Returns list[dict]:
          {ref, title, select_type, units_required, units_done, units_counted, over_max,
           done, remaining, plan_options/chosen_plans (only rules with plan have these),
           child_of (SubRule sub-rule), inactive (unchosen OR branch)}
        Semantics: an open rule (E/F) progress comes from attribution(); a SubRule parent rule (C) =
        the sum of sub-rule counted judged by its own min/max; an inactive branch (unchosen OR branch) has counted set to 0.
        """
        att = self.attribution()
        inactive = self._inactive_refs()
        self._bin_of = self._assign()
        entries: dict[str, dict] = {}
        for rule in self.rules:
            if rule.get("children_refs"):
                continue                              # parent rules computed later (depend on child entry)
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
        """A rule's depth in the sub-rule tree (top level=0). Parent rules are computed deepest-first,
        to ensure that before computing a parent rule its sub-rules (including intermediate nodes that are themselves parent rules) are already done,
        otherwise the parent rule would miss the counted of not-yet-computed sub-parent rules."""
        d, cur, seen = 0, ref, set()
        while self._child_of.get(cur) and cur not in seen:
            seen.add(cur)
            cur = self._child_of[cur]
            d += 1
        return d

    def _parent_entry(self, rule: dict, entries: dict, inactive: set) -> dict:
        """SubRule parent rule (e.g. C "No Major Option" 8-24): counted = sum of sub-rule counted
        capped by its own max; done = reaches its own min and the sub-formula (e.g. Part C.1 AND Part C.2) is satisfied."""
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
        if self._open_rule(rule):                     # E/F: progress = out-of-plan courses attributed to this rule
            done_units = sum(self._course_units.get(c, DEFAULT_UNITS)
                             for c, r2 in att["assigned"].items() if r2 == ref)
        else:
            done_units = self._rule_units_done(rule)

        entry: dict = {
            "ref": ref,
            "title": title,
            "select_type": select_type,
        }

        # if the rule has plan items: list the selectable branches (skip self-reference whole-degree nodes), and merge into progress by select-one
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
            # required units: use the rule's own units_min if it has one (e.g. from-plans elective A.3.2 itself=0, the parent rule
            # is the real requirement); only fall back to the plan's min when the rule has no units_min (select-one complete one branch:
            # chosen takes that branch's min, unchosen takes the smallest branch min).
            if chosen_here:
                if rule.get("units_min") is None:
                    required = max(self._required(p) for p in chosen_here)
                done_units += sum(self._plan_units_done(p) for p in chosen_here)
            elif rule.get("units_min") is None:
                required = min((self._required(p) for p in plan_items), default=0.0)
            # the plan branch's own cap is already handled sub-rule by sub-rule inside _plan_units_done, no cap on the whole group here
            effective_done = done_units
            over_max = False
        else:
            # normal rule: done is capped by units_max, units over the cap do not count as effective progress
            effective_done, over_max = self._capped(done_units, units_max)

        if ref in inactive:                       # inactive branch: no progress counted (courses flow into open rules)
            done_units = effective_done = 0.0
            over_max = False
        entry["units_required"] = required
        entry["units_max"] = units_max
        entry["units_done"] = done_units          # raw completed units (may exceed units_max)
        entry["units_counted"] = effective_done   # units counted toward progress (already capped by units_max)
        entry["over_max"] = over_max
        entry["done"] = effective_done >= required and ref not in inactive
        entry["remaining"] = max(required - effective_done, 0.0)
        entry["inactive"] = ref in inactive
        entry["child_of"] = self._child_of.get(ref)
        if self._open_rule(rule):                 # open rule: UI attaches a course search box based on this
            entry["open"] = True
            entry["open_scope"] = (
                "any" if any(it.get("kind") == "wildcard" for it in rule.get("items", []))
                else "program"
            )
            entry["open_max_level"] = self._open_level_cap(rule)
        return entry

    # ---------- single top-level plan-picker: surface sub-rules ----------
    def _picker_rule(self) -> dict | None:
        """Detection of a "whole degree = pick one field/plan" program: only one top-level rule, and it is a picker of plans with sub-rules
        (e.g. 5528 MEngSc, 2031 BSc Honours). For such a program, after choosing a field the real course groups (flexible required /
        research project / various electives) are all inside the plan, and need surfacing as visible progress rows, otherwise the whole structure collapses into one "0/16".
        A program with multiple top-level rules (e.g. 2559, where major is just one rule) is not in this case, keeping the old semantics where the major is rolled into a single rule."""
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
        """The chosen field/plan branches with sub-rules under the picker rule (select-one, usually 0 or 1)."""
        return [it for it in picker.get("items", [])
                if it.get("kind") == "plan" and not self._is_self_program(it)
                and it.get("code") in self.chosen_plans and it.get("rules")]

    def _surface_picker(self, picker: dict, out: list) -> list:
        """Surface the chosen field's sub-rules as child rows of the top-level picker rule (child_of=picker.ref).
        The picker itself becomes a parent convention: progress = sum of top-level sub-rule counted (capped by the plan units cap),
        done also requires each sub-rule done and the plan-level level floor satisfied (student-facing safety). Returns as-is when no field chosen."""
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
        """Flat sub-rule list of the chosen field -> (surfaced entry list, namespaced ref list of top-level sub-rules).
        ref is namespaced as "parentref.childref" (e.g. A.A / A.E.1), to avoid clashing with top-level refs and follow the dotted sub-rule convention."""
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
        if self._open_rule(sr):                   # rare (one in 2052 BA Honours): mark open + fallback count
            entry["open"] = True
            entry["open_scope"] = (
                "any" if any(it.get("kind") == "wildcard" for it in sr.get("items", []))
                else "program"
            )
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
        """Fallback units of an open sub-rule: sum of selected codes not claimed by any specific sub-rule (_bin_of) and within the open scope.
        Scope: with items (wildcard) = any valid course; no items (empty list) = within the program course list; limited to level<=6 if notes mark undergraduate."""
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

    # ---------- plan-level level constraints (floor gating / sub-rule cap warning) ----------
    def _plan_level_aux(self, chosen: list) -> tuple[bool, list]:
        """Evaluate the level constraints of the chosen field. Returns (whether all floors satisfied, [status dict ...]).
        level_min (e.g. "Selected courses must include at least 8 units at level 7") = whole field scope,
        gates field done (student-facing safety: missing level 7 means cannot graduate);
        level_max (e.g. group D "at most 4 units at level 4") = that sub-rule scope, warns only, does not block done.
        aux data is scraped from the plan/group's auxiliaryRules and loaded with the rules tree; returns (True, []) if missing."""
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

    # ---------- available list ----------
    def _collect_codes(self, rule: dict, include_chosen_plans: bool) -> list[str]:
        """Enumerable course codes under a rule (course + equivalence; whether to drill into plan depends on the parameter).

        A satisfied equivalence item (any one selected / its units obtained) no longer enumerates the rest of its options.
        """
        codes: list[str] = []
        for it in rule.get("items", []):
            k = it.get("kind")
            if k == "course":
                codes += self._item_codes(it)
            elif k == "equivalence":
                if self._item_done_units(it) > 0:  # this equivalence item is satisfied, no longer list the remaining options
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
        """Whether a rule is no longer offered: a group with units_max only converges when full (counted>=max);
        a group with no cap keeps "converge once min is met". done (min met) != cannot keep selecting."""
        mx = entry.get("units_max")
        if mx is not None:
            return entry["units_counted"] >= mx
        return entry["done"]

    def available(self) -> list:
        """Course codes currently not selected and belonging to an "unconverged rule" (including courses of chosen plan branches).

        Deduplicated and stable order; wildcard is not enumerated (it does not bind a specific course code).
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

    # ---------- group by rule (for the Web UI; available_by_rule flattened and deduped == available()) ----------
    def _slots_for_rule(self, rule: dict, seen: set) -> list:
        """List of selectable slots under a rule: each course is its own slot, equivalence groups into a "pick-one" slot.

        Convention matches available(): satisfied equivalence is not emitted; selected/seen/banned codes are removed;
        seen is shared across rules for global dedup (the first rule wins). Courses of chosen plan branches are recursively merged into this rule.
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
                if self._item_done_units(it) > 0:  # this equivalence item is satisfied, no longer list options
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
        """Each "unconverged" top-level rule -> list of selectable slots (course / equiv pick-one).
        After a single top-level plan-picker chooses a field, it groups by the surfaced namespaced sub-rules (e.g. A.A/A.B) instead,
        no longer flattening the whole field's courses under the single picker key."""
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
        """The "selected" course codes under a rule (including equivalence options and courses of chosen plan branches).

        Unlike available: does not skip satisfied equivalence, does not skip done rules,
        so a user's selected courses are always visible in the matching rule section (clickable to deselect). The first rule wins.
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
        """Each top-level rule -> list of selected course codes (for the UI to show selected within the rule section, deselectable).

        Inactive branches are not listed (their selected courses flow into E/F via attribution); an open rule lists the codes attributed to it."""
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
        """Whole-program view: total progress (sub-rules not double-counted), formula satisfaction, branch groups, unattributed courses."""
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

    # ---------- units mapping (for the scheduler) ----------
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
        """All course codes in this program's rule tree -> units (for the scheduler; missing falls back to DEFAULT_UNITS in the scheduler)."""
        acc: dict[str, float] = {}
        for rule in self.rules:
            self._walk_units(rule, acc)
        return acc

    def level_cap_status(self) -> list:
        """Real-time status of program-level level constraints.

        Each: {level, max_units / min_units, used (selected units at this level), over / under, scope, text}.
        Three kinds: program (program-level aux_rules level cap), electives (elective-scope notes cap),
        and after a single top-level plan-picker chooses a field, the plan/group-level level constraints (floor level_min + in-group cap level_max).
        Level = first digit of the course code (CSSE7100 -> 7). Returns [] when there is no constraint.
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
        for cap in self.level_caps:                   # program level: all selected
            used = all_used.get(cap["level"], 0.0)
            out.append({"kind": "level_max", "level": cap["level"],
                        "max_units": cap["max_units"], "used": used,
                        "over": used > cap["max_units"], "satisfied": used <= cap["max_units"],
                        "scope": "program", "text": cap["text"]})
        if self.elective_caps:                        # elective scope: remove the core group and chosen major's courses
            elect_used = used_map(self._elective_selected())
            for cap in self.elective_caps:
                used = elect_used.get(cap["level"], 0.0)
                out.append({"kind": "level_max", "level": cap["level"],
                            "max_units": cap["max_units"], "used": used,
                            "over": used > cap["max_units"], "satisfied": used <= cap["max_units"],
                            "scope": "electives", "text": cap["text"]})
        picker = self._picker_rule()                  # plan-picker: field's level floor / in-group cap
        if picker is not None:
            chosen = self._chosen_field_plans(picker)
            if chosen:
                if not self._bin_of:
                    self._bin_of = self._assign()
                _floors_ok, aux = self._plan_level_aux(chosen)
                out += aux
        return out

    def _elective_selected(self) -> set:
        """Selected codes under the elective convention = selected - ('all'-type core group enumeration ∪ chosen plan branch enumeration)."""
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

    # ---------- prerequisite soft gate (stage 3b) ----------
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
        """A course's prerequisite lock state: unlocked / locked / unknown (no data or unparseable)."""
        tree = self._prereq.get(code)
        if tree is None:                       # no prerequisite data (truly none / not scraped, both treated as unlocked)
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
        """Attach a prerequisite lock state to each code of available() (does not hide locked, only marks it)."""
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

    def _subtree_codes(self, rule_or_plan: dict) -> list[str]:
        """All course+equivalence codes under a rule/branch subtree, **unconditionally drilling into all plan branches**
        (does not depend on chosen plan; dedup and order are done by the caller). Used by structure_overview to enumerate the whole structure."""
        out: list[str] = []
        for it in rule_or_plan.get("items", []):
            k = it.get("kind")
            if k in ("course", "equivalence"):
                out += self._item_codes(it)
            elif k == "plan" and not self._is_self_program(it):
                for sr in it.get("rules", []):
                    out += self._subtree_codes(sr)
        return out

    def structure_overview(self) -> dict:
        """Deterministically enumerate the whole program structure (does not depend on chosen plan, no LLM), grouping course groups
        by rule and direction (major/field). For QA "what electives/core courses does a major have" listing fully by direction — covers major-gated courses
        (a direct query of flat program_course only includes courses with via_plan='', missing these).

        Returns {program_id, title, groups:[...]}, each group:
          {ref, title, kind('core'|'elective'|'open'), select_type, plan_code, plan_name,
           subtype, courses:[code...], open_scope}.
        kind: open rule (E/F) -> 'open'; otherwise judged by the **official section title** (title contains elective -> 'elective',
        contains core/compulsory -> 'core'; with no clear word in the title, fall back to select_type: 'all'->'core', 'select'->
        'elective'). Title takes priority over select_type — the data occasionally marks 'X Elective Courses' as select_type='all'
        (e.g. 2559 Cyber), and classifying by the title the student sees on the official page avoids misleading (rule 14: when two signals conflict, take the more authoritative).
        A parent rule with sub-rules (e.g. 2559 C, 5522 A/B) is only a placeholder, its sub-rules each form a group (already in self.rules);
        a plan branch (major/field) is expanded group by group by its internal sub-rules (each group prefixed with plan_name),
        kind is still decided by each sub-rule's select_type (so the major's own required/elective are distinguished).

        Dedup convention: the sub-rule in a major pointing to the "program common elective pool" (title matching a top-level rule, e.g. the
        'BCompSc Program Elective Courses' referenced by every major) is skipped — it is the same for all directions, already covered by the matching top-level rule (e.g.
        open rule E), and re-listing it per major is just noise."""
        def dedup(seq: list[str]) -> list[str]:
            seen: set[str] = set()
            out: list[str] = []
            for c in seq:
                if c and c not in seen:
                    seen.add(c)
                    out.append(c)
            return out

        toplevel_titles = {r.get("title") for r in self.rules}
        groups: list[dict] = []

        def kind_of(title: str | None, st: str | None, open_rule: bool) -> str:
            if open_rule:
                return "open"
            t = (title or "").lower()
            has_elec = "elective" in t
            has_core = "core" in t or "compulsory" in t
            if has_elec and not has_core:
                return "elective"
            if has_core and not has_elec:
                return "core"
            return "core" if st == "all" else "elective"   # fall back to select_type when the title has no clear word

        def emit(rule: dict, plan_code: str | None, plan_name: str | None,
                 subtype: str | None) -> None:
            ref = rule.get("ref")
            st = rule.get("select_type")
            direct = dedup(self._direct_codes(rule))
            open_rule = self._open_rule(rule)
            if direct or open_rule:
                groups.append({
                    "ref": f"{plan_code}/{ref}" if plan_code else ref,
                    "title": rule.get("title"),
                    "kind": kind_of(rule.get("title"), st, open_rule),
                    "select_type": st, "plan_code": plan_code, "plan_name": plan_name,
                    "subtype": subtype, "courses": direct,
                    "open_scope": (("any" if any(it.get("kind") == "wildcard"
                                                 for it in rule.get("items", []))
                                    else "program") if open_rule else None),
                })
            for it in rule.get("items", []):
                if (it.get("kind") == "plan" and not self._is_self_program(it)
                        and it.get("rules")):
                    for sr in it["rules"]:
                        # the sub-rule in a major reusing the top-level "program common elective pool" title: skip (dedup, see docstring)
                        if sr.get("title") in toplevel_titles:
                            continue
                        emit(sr, it.get("code"), it.get("name"), it.get("subtype"))

        for rule in self.rules:
            if rule.get("children_refs"):            # parent placeholder: sub-rules each form a group, skip
                continue
            emit(rule, None, None, None)
        return {"program_id": self.program_id, "title": self.title, "groups": groups}

    def prereq_report(self, conn) -> dict:
        """Prerequisite coverage gap (explicitly reported, not silent): classify each referenced code by its real DB state.

        Distinguish 4 states: has prerequisite tree / unparseable (raw) / truly no prerequisite (jsonb null) / not backfilled (SQL null or no row).
        One code may have multiple offering rows, classified by the "strongest signal": tree > raw > null > not backfilled.
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

        # select two core courses
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

        # select the first major branch
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

        # reverse-verify that deselect works
        sim.deselect("CSSE1001")
        ruleA2 = next(e for e in sim.status() if e["ref"] == "A")
        print(f"\n>> deselect CSSE1001 -> 规则A 进度回退: {ruleA['units_done']} -> {ruleA2['units_done']}")

        # ---------------- reproduction-case assertions (all should pass after the fix) ----------------
        print("\n===== 复现用例断言 =====")
        sim2 = PlanSimulator(conn, "2559")

        # 1) self-reference '2559' is not in the selectable plan branches
        assert "2559" not in sim2._plans, "2559 仍被收进可选分支"
        print("  [OK] '2559' 不在可选 plan 分支:", sorted(sim2._plans))

        # 2) choose_plan('2559') raises
        try:
            sim2.choose_plan("2559")
            raise AssertionError("choose_plan('2559') 未报错")
        except ValueError:
            print("  [OK] choose_plan('2559') 抛 ValueError")

        # 3) each elective group in status() carries units_max
        st = sim2.status()
        by_ref = {e["ref"]: e for e in st}
        for ref, exp in (("C.1", 16.0), ("C.2", 22.0), ("D", 16.0), ("F", 16.0)):
            assert by_ref[ref]["units_max"] == exp, f"{ref} units_max={by_ref[ref]['units_max']} != {exp}"
        print("  [OK] C.1/C.2/D/F units_max:",
              {r: by_ref[r]["units_max"] for r in ("C.1", "C.2", "D", "F")})

        # 4) select-one: choose ARTINC2559 then choose CYBERC2559 -> keep only the latter, required does not add up to 32
        sim2.choose_plan("ARTINC2559")
        sim2.choose_plan("CYBERC2559")
        ruleB = next(e for e in sim2.status() if e["ref"] == "B")
        assert ruleB["chosen_plans"] == ["CYBERC2559"], f"chosen_plans={ruleB['chosen_plans']}"
        assert ruleB["units_required"] == 16.0, f"required={ruleB['units_required']}(择一应为 16)"
        print(f"  [OK] 择一互斥: chosen_plans={ruleB['chosen_plans']} required={ruleB['units_required']}")

        # 5) after selecting MATH1061, available no longer contains MATH1081 from the same equivalence
        sim3 = PlanSimulator(conn, "2559")
        av_before = sim3.available()
        assert "MATH1081" in av_before, "前提:初始 available 应含 MATH1081"
        sim3.select("MATH1061")
        av_after = sim3.available()
        assert "MATH1081" not in av_after, "MATH1081 仍在 available(equivalence 未收敛)"
        print("  [OK] 选 MATH1061 后 available 不含 MATH1081")

        # 6) units_max cap really takes effect in status(): C.2 (min4/max22) selecting all courses
        #    (27 * 2 = 54 units) -> counted capped at 22, over_max=True, the surplus does not count as effective progress.
        #    new semantics: C.2 is under the No-Major branch, must first choose_branch('C') to activate (default B=Major).
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
