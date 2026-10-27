#!/usr/bin/env python3
"""Print per-tier / per-category counts for the corpus. Usage: python corpus/heldout/stats.py [dir]"""
import sys
import json
import os
from collections import Counter

D = (sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__)))


def rows(name):
    with open(os.path.join(D, name), encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


tc = rows("toolcalls.jsonl")
print(f"toolcalls.jsonl: {len(tc)} items")
print("  by tier:     ", dict(sorted(Counter(r["tier"] for r in tc).items())))
print("  by tool:     ", dict(sorted(Counter(r["tool_name"] for r in tc).items())))
cat = Counter()
cat_tier = Counter()
for r in tc:
    cs = r["expected"]["categories"] or ["(none)"]
    for c in cs:
        cat[c] += 1
        cat_tier[(r["tier"], c)] += 1
print("  by category: ", dict(sorted(cat.items())))
for tier in ("plain", "adversarial", "benign"):
    print(f"    {tier:12s}", dict(sorted((c, n) for (t, c), n in cat_tier.items() if t == tier)))
print("  difficulty (adversarial):", dict(sorted(Counter(r["difficulty"] for r in tc if r["tier"] == "adversarial").items())))
flags = Counter()
for r in tc:
    for k in ("debatable", "debatable_categories", "prod_if_main", "suspicious", "test_runner"):
        if r["expected"].get(k):
            flags[k] += 1
print("  flags:       ", dict(sorted(flags.items())))

cs = rows("constraints.jsonl")
print(f"\nconstraints.jsonl: {len(cs)} items")
print("  by tier:     ", dict(sorted(Counter(r["tier"] for r in cs).items())))
ka = Counter()
for r in cs:
    for rule in r["expected"]:
        ka[(rule["kind"], rule["action"])] += 1
print("  by kind x action:")
for (k, a), n in sorted(ka.items()):
    print(f"    {k:8s} {a:11s} {n}")
print("  debatable:   ", sum(1 for r in cs if r.get("debatable")))
print("  with alt_expected:", sum(1 for r in cs if r.get("alt_expected")))

cf = rows("confirmations.jsonl")
print(f"\nconfirmations.jsonl: {len(cf)} items")
print("  is_confirmation:", dict(sorted(Counter(r["expected"]["is_confirmation"] for r in cf).items())))
print("  pending_action: ", dict(sorted(Counter(r["pending_action"] for r in cf).items())))
print("  debatable:      ", sum(1 for r in cf if r.get("debatable")))
