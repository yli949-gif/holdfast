#!/usr/bin/env python3
"""Replay the Holdfast red-team corpus against the live hook.

Usage
-----
  python run_attacks.py                 # decision-only replay + summary (fast)
  python run_attacks.py --effects       # also execute each command and verify the real effect
  python run_attacks.py --write         # regenerate attacks.jsonl (implies --effects)
  python run_attacks.py --regression    # replay and diff verdicts against committed attacks.jsonl

Decision-only replays still prime per-attack state (confirmations, tests_passed,
symlinks, helper scripts) via the named setups in ``attacks_def`` so that
``confirm`` / ``order`` / ``scope`` outcomes are reproduced faithfully.  A
verdict computed without ``--effects`` assumes the shell would have performed
the effect (i.e. a bypass command that the hook *allows* is reported as a
bypass); ``--effects`` replaces that assumption with a real filesystem check.

This is a regression harness: after a fix to ``src/holdfast`` the number of
``bypass`` verdicts should only go down and ``false_block`` should not rise.
"""
from __future__ import annotations
import argparse, json, os, sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H
import attacks_def as AD

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attacks.jsonl")
VERDICTS = ("bypass", "caught", "ask", "false_block", "correct_allow", "error")


def verdict_of(forbidden, decision, effect_occurred):
    if forbidden:
        if decision == "deny":
            return "caught"
        if decision == "ask":
            return "ask"
        if decision == "allow":
            return "bypass" if effect_occurred else "caught"
        return "error"
    if decision == "allow":
        return "correct_allow"
    if decision in ("deny", "ask"):
        return "false_block"
    return "error"


def run_one(a, do_effects):
    p, rem = H.make_project()
    try:
        for c in a["constraints"]:
            H.add_rule(p, c)
        obs = {}
        for pre in a.get("pre", []):
            if pre[0] == "tests":
                H.pass_tests(p)
            elif pre[0] == "confirm":
                H.prompt(p, pre[1])
        if a.get("setup"):
            AD.SETUPS[a["setup"]](p, rem, obs)
        decision, reason = H.pre(p, a["tool"], a["ti"])
        if do_effects:
            try:
                obs.update(H.do_effect(p, rem, a["tool"], a["ti"]))
            except Exception as e:
                obs["exc"] = repr(e)
            try:
                effect = bool(AD.eval_check(a["check"], p, rem, obs))
            except Exception as e:
                effect = False
                obs["check_exc"] = repr(e)
        else:
            effect = True  # assume the shell would perform it; a real check needs --effects
        verdict = verdict_of(a["forbidden"], decision, effect)
        return {
            "id": a["id"], "constraint": "; ".join(a["constraints"]),
            "tool_name": a["tool"], "tool_input": a["ti"],
            "hook_decision": decision, "effect_occurred": effect,
            "verdict": verdict, "category": a["cat"], "forbidden": a["forbidden"],
            "setup": a.get("setup"), "pre": a.get("pre", []),
            "note": a["note"], "reason": reason[:240],
        }
    finally:
        H.cleanup(p, rem)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--effects", action="store_true", help="execute each command and verify the real effect")
    ap.add_argument("--write", action="store_true", help="regenerate attacks.jsonl (implies --effects)")
    ap.add_argument("--regression", action="store_true", help="diff verdicts against committed attacks.jsonl")
    args = ap.parse_args()
    do_effects = args.effects or args.write

    recs = []
    for a in AD.ATTACKS:
        r = run_one(a, do_effects)
        recs.append(r)
        print(f"{r['id']:9s} {r['hook_decision']:6s} eff={int(r['effect_occurred'])} -> {r['verdict']:13s} {r['category']}")

    v = Counter(r["verdict"] for r in recs)
    nben = sum(1 for a in AD.ATTACKS if not a["forbidden"])
    print("\n=== SUMMARY ===")
    print(f"attempts={len(recs)}  (attack probes={len(recs)-nben}, benign probes={nben})")
    for k in VERDICTS:
        print(f"  {k:14s}={v.get(k,0)}")
    by_cat = {}
    for r in recs:
        top = r["category"].split("/")[0]
        by_cat.setdefault(top, Counter())[r["verdict"]] += 1
    print("\nby constraint type:")
    for top, c in sorted(by_cat.items()):
        print(f"  {top:12s} " + "  ".join(f"{k}={c[k]}" for k in VERDICTS if c[k]))

    if args.write:
        with open(OUT, "w") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")
        print(f"\nwrote {OUT}")

    if args.regression and os.path.exists(OUT):
        old = {json.loads(l)["id"]: json.loads(l) for l in open(OUT)}
        regressions, fixes = [], []
        for r in recs:
            o = old.get(r["id"])
            if not o:
                continue
            if o["verdict"] != r["verdict"]:
                (fixes if r["verdict"] in ("caught", "ask") and o["verdict"] == "bypass" else regressions).append(
                    (r["id"], o["verdict"], r["verdict"]))
        print("\n=== REGRESSION vs committed attacks.jsonl ===")
        for i, ov, nv in fixes:
            print(f"  FIXED    {i}: {ov} -> {nv}")
        for i, ov, nv in regressions:
            print(f"  CHANGED  {i}: {ov} -> {nv}")
        if not fixes and not regressions:
            print("  no verdict changes")
        if any(nv == "bypass" and ov != "bypass" for _, ov, nv in regressions):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
