import json
import psycopg
from simulator import PlanSimulator, DSN


def find_math_equiv_rule(rules):
    """Find the top-level rule whose equivalence item contains MATH1061/MATH1081."""
    for rule in rules:
        for it in rule.get("items", []):
            if it.get("kind") == "equivalence":
                codes = {o.get("code") for o in it.get("options", [])}
                if "MATH1061" in codes:
                    return rule, it
    return None, None


def core_remaining_slots(sim):
    """Count how many top-level 'all' (core) rules are not yet done."""
    return [e for e in sim.status() if e["select_type"] == "all" and not e["done"]]


with psycopg.connect(DSN) as conn:
    conn.read_only = True
    sim = PlanSimulator(conn, "2559")
    print(f"program {sim.program_id}: {sim.title}")

    rule, equiv = find_math_equiv_rule(sim.rules)
    if rule is None:
        print("!! could not find MATH1061 equivalence item in any top-level rule")
        raise SystemExit(1)

    equiv_codes = [o.get("code") for o in equiv.get("options", [])]
    ref = rule.get("ref")
    print(f"\nMATH equivalence located in rule ref={ref!r} title={rule.get('title')!r}")
    print(f"  equivalence options: {equiv_codes}")
    print(f"  rule select_type={rule.get('select_type')} units_min={rule.get('units_min')} units_max={rule.get('units_max')}")

    # ---- BEFORE ----
    st_before = {e["ref"]: e for e in sim.status()}
    av_before = sim.available()
    rule_before = st_before[ref]
    print("\n===== BEFORE select(MATH1061) =====")
    print(f"  rule {ref}: units_done={rule_before['units_done']} "
          f"units_required={rule_before['units_required']} done={rule_before['done']} "
          f"remaining={rule_before['remaining']}")
    print(f"  MATH1061 in available: {'MATH1061' in av_before}")
    print(f"  MATH1081 in available: {'MATH1081' in av_before}")
    print(f"  item_done_units(equiv) = {sim._item_done_units(equiv)}")

    # snapshot of core (all) rules and remaining count
    core_before = core_remaining_slots(sim)
    print(f"  unfinished core ('all') rules count: {len(core_before)} "
          f"refs={[e['ref'] for e in core_before]}")
    total_required_before = sum(e["units_required"] for e in core_before)
    total_remaining_before = sum(e["remaining"] for e in core_before)
    print(f"  sum core remaining units (unfinished core rules): {total_remaining_before}")

    # ---- ACT ----
    sim.select("MATH1061")

    # ---- AFTER ----
    st_after = {e["ref"]: e for e in sim.status()}
    av_after = sim.available()
    rule_after = st_after[ref]
    print("\n===== AFTER select(MATH1061) =====")
    print(f"  rule {ref}: units_done={rule_after['units_done']} "
          f"units_required={rule_after['units_required']} done={rule_after['done']} "
          f"remaining={rule_after['remaining']}")
    print(f"  MATH1061 in available: {'MATH1061' in av_after}")
    print(f"  MATH1081 in available: {'MATH1081' in av_after}")
    print(f"  item_done_units(equiv) = {sim._item_done_units(equiv)}")

    core_after = core_remaining_slots(sim)
    print(f"  unfinished core ('all') rules count: {len(core_after)} "
          f"refs={[e['ref'] for e in core_after]}")
    total_remaining_after = sum(e["remaining"] for e in core_after)
    print(f"  sum core remaining units (unfinished core rules): {total_remaining_after}")

    # ---- ASSERTIONS / VERDICT ----
    print("\n===== VERDICT =====")

    # The MATH equivalence item value: equivalence counts as ONE filled slot.
    one_member_units = sim._item_done_units(equiv)
    math1061_units = next(o.get("units", 2) for o in equiv.get("options", []) if o.get("code") == "MATH1061")
    print(f"1) equivalence item value after selecting only MATH1061 = {one_member_units} "
          f"(MATH1061 units={math1061_units})")
    counts_once = abs(one_member_units - float(math1061_units)) < 1e-9
    print(f"   -> counts exactly the one selected member (not doubled): {counts_once}")

    # MATH1081 must NOT still be demanded (not in available list anymore)
    alt_not_demanded = ("MATH1081" not in av_after) and ("MATH1081" in av_before or True)
    alt_was_offered = "MATH1081" in av_before
    print(f"2) MATH1081 was offered before: {alt_was_offered}; "
          f"MATH1081 still demanded after: {'MATH1081' in av_after}")
    print(f"   -> alternative NOT still demanded: {alt_not_demanded}")

    # Remaining core dropped by exactly one slot (one equivalence slot worth of units)
    rule_remaining_drop = rule_before["remaining"] - rule_after["remaining"]
    print(f"3) rule {ref} remaining dropped by {rule_remaining_drop} "
          f"(one slot = {math1061_units} units)")
    dropped_one_slot = abs(rule_remaining_drop - float(math1061_units)) < 1e-9

    # Also: among all core rules, total remaining dropped by exactly one slot worth
    total_drop = total_remaining_before - total_remaining_after
    print(f"   total core remaining dropped by {total_drop} across all unfinished core rules")
    total_dropped_one_slot = abs(total_drop - float(math1061_units)) < 1e-9

    # equivalence handled as ONE slot overall
    handled_as_one_slot = counts_once and alt_not_demanded and dropped_one_slot
    print(f"\nEquivalence handled as ONE slot: {handled_as_one_slot}")
    print(f"  counts_once={counts_once} alt_not_demanded={alt_not_demanded} "
          f"dropped_one_slot={dropped_one_slot} total_dropped_one_slot={total_dropped_one_slot}")

    result = {
        "rule_ref": ref,
        "equiv_options": equiv_codes,
        "math1061_units": float(math1061_units),
        "equiv_item_value_after": one_member_units,
        "math1081_in_available_before": alt_was_offered,
        "math1081_in_available_after": "MATH1081" in av_after,
        "rule_remaining_before": rule_before["remaining"],
        "rule_remaining_after": rule_after["remaining"],
        "rule_remaining_drop": rule_remaining_drop,
        "counts_once": counts_once,
        "alt_not_demanded": alt_not_demanded,
        "dropped_one_slot": dropped_one_slot,
        "handled_as_one_slot": handled_as_one_slot,
    }
    print("\nRESULT_JSON " + json.dumps(result))
