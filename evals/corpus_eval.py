"""Score holdfast's classifier and extractor against the implementation-independent corpus.

Usage: python evals/corpus_eval.py [--verbose] [--out evals/results/corpus.json]

Metrics (per tier):
  category-level: missed violations (FN) = expected category not detected;
                  false blocks (FP) = detected category not expected.
  item-level:     exact category-set match; path-set match when expected paths non-empty.
Debatable items are scored but reported separately; `prod_if_main` items are scored with
touch_prod treated as optional (either label accepted).
"""
from __future__ import annotations
import argparse, json, pathlib, statistics, sys, time
from collections import Counter, defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from holdfast.classify import classify  # noqa: E402
from holdfast.extract import extract, extract_confirmations  # noqa: E402

CWD = "/proj"


def load(name):
    return [json.loads(l) for l in (ROOT / "corpus" / name).read_text().splitlines() if l.strip()]


def score_toolcalls(verbose):
    items = load("toolcalls.jsonl")
    per_tier = defaultdict(lambda: Counter())
    cat_conf = defaultdict(lambda: Counter())  # category -> tp/fn/fp
    misses, falses, latencies = [], [], []
    for it in items:
        exp = set(it["expected"].get("categories", []))
        optional = set(it.get("debatable_categories", []) or it["expected"].get("debatable_categories", []))
        if it.get("prod_if_main") or it["expected"].get("prod_if_main"):
            optional.add("touch_prod")
        t0 = time.perf_counter()
        acts = classify(it["tool_name"], it["tool_input"], it.get("cwd", CWD), home="/")
        latencies.append((time.perf_counter() - t0) * 1000)
        # run_cmd only matters when a rule names a command; it is emitted for every Bash call
        # by design, so it is scored only when the corpus expects it.
        opaque = any(a.opaque for a in acts)
        got = {a.category for a in acts if not a.opaque and (a.category != "run_cmd" or "run_cmd" in exp)}
        got_paths = {p for a in acts for p in a.paths}
        if opaque:
            opaque_cats = {a.category for a in acts if a.opaque}
            got |= (opaque_cats & exp)  # opaque guesses count only where expected (-> 'ask' in engine)
        tier = it["tier"]
        fn = (exp - got) - optional
        fp = (got - exp) - optional
        # suspicious items: spec wants ask (opaque) rather than exact category
        if it.get("suspicious") and opaque:
            fn = set()
        c = per_tier[tier]
        c["n"] += 1
        if it.get("debatable"):
            c["debatable"] += 1
        if not fn and not fp:
            c["exact"] += 1
        if opaque:
            c["opaque_ask"] += 1
        if fn:
            c["items_with_fn"] += 1
            misses.append((it["id"], tier, it["tool_input"].get("command") or it["tool_input"].get("file_path"), sorted(fn), sorted(got)))
        if fp:
            c["items_with_fp"] += 1
            falses.append((it["id"], tier, it["tool_input"].get("command") or it["tool_input"].get("file_path"), sorted(fp), sorted(exp)))
        for cat in exp - optional:
            cat_conf[cat]["tp" if cat in got else "fn"] += 1
        for cat in got - exp - optional:
            cat_conf[cat]["fp"] += 1
        exp_paths = set(it["expected"].get("paths", []))
        if exp_paths:
            c["path_items"] += 1
            if exp_paths <= got_paths:
                c["path_ok"] += 1
    out = {"tiers": {}, "categories": {}, "misses": misses, "false_blocks": falses,
           "latency_ms": {"median": statistics.median(latencies), "p95": sorted(latencies)[int(len(latencies) * 0.95) - 1]}}
    for tier, c in per_tier.items():
        out["tiers"][tier] = {
            "n": c["n"], "exact_match": c["exact"], "exact_rate": round(c["exact"] / c["n"], 3),
            "missed_violation_rate": round(c["items_with_fn"] / c["n"], 3),
            "false_block_rate": round(c["items_with_fp"] / c["n"], 3),
            "path_recall": round(c["path_ok"] / c["path_items"], 3) if c["path_items"] else None,
            "debatable": c["debatable"], "opaque_ask": c["opaque_ask"]}
    for cat, c in cat_conf.items():
        tp, fn, fp = c["tp"], c["fn"], c["fp"]
        out["categories"][cat] = {"tp": tp, "fn": fn, "fp": fp,
                                  "recall": round(tp / (tp + fn), 3) if tp + fn else None,
                                  "precision": round(tp / (tp + fp), 3) if tp + fp else None}
    if verbose:
        print("\nMISSED (expected but not detected):")
        for m in misses:
            print("  ", *m)
        print("\nFALSE BLOCKS (detected but not expected):")
        for f in falses:
            print("  ", *f)
    return out


def score_constraints(verbose):
    items = load("constraints.jsonl")
    tiers = defaultdict(Counter)
    misses = []
    for it in items:
        exp = {(r["kind"], r["action"]) for r in it["expected"]}
        rules = extract(it["text"])
        got = {(r['kind'], r['action']) for r in rules}
        c = tiers[it["tier"]]
        c["n"] += 1
        c["exp_rules"] += len(exp)
        c["recovered"] += len(exp & got)
        c["spurious"] += len(got - exp)
        if exp == got:
            c["exact"] += 1
        else:
            misses.append((it["id"], it["tier"], it["text"], sorted(exp), sorted(got)))
    out = {t: {"n": c["n"], "exact_rate": round(c["exact"] / c["n"], 3),
               "rule_recall": round(c["recovered"] / c["exp_rules"], 3) if c["exp_rules"] else None,
               "spurious_rules": c["spurious"]} for t, c in tiers.items()}
    if verbose:
        print("\nCONSTRAINT EXTRACTION MISMATCHES:")
        for m in misses:
            print("  ", *m)
    return {"tiers": out, "mismatches": misses}


def score_confirmations(verbose):
    items = load("confirmations.jsonl")
    c = Counter()
    wrong = []
    for it in items:
        got = bool(extract_confirmations(it["text"]))
        exp = it["expected"]["is_confirmation"]
        c["n"] += 1
        if got == exp:
            c["ok"] += 1
        else:
            wrong.append((it["id"], it["text"], exp, got))
    if verbose and wrong:
        print("\nCONFIRMATION MISMATCHES:")
        for w in wrong:
            print("  ", *w)
    return {"n": c["n"], "accuracy": round(c["ok"] / c["n"], 3), "wrong": wrong}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "evals/results/corpus.json"))
    a = ap.parse_args()
    res = {"toolcalls": score_toolcalls(a.verbose), "constraints": score_constraints(a.verbose),
           "confirmations": score_confirmations(a.verbose)}
    pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(a.out).write_text(json.dumps(res, indent=2))
    print("\n== tool-call classification ==")
    for t, v in res["toolcalls"]["tiers"].items():
        print(f"  {t:12s} n={v['n']:3d} exact={v['exact_rate']:.3f} missed={v['missed_violation_rate']:.3f} falseblock={v['false_block_rate']:.3f} path_recall={v['path_recall']}")
    print("  latency ms:", res["toolcalls"]["latency_ms"])
    print("== per category ==")
    for cat, v in sorted(res["toolcalls"]["categories"].items()):
        print(f"  {cat:11s} recall={v['recall']} precision={v['precision']} (tp={v['tp']} fn={v['fn']} fp={v['fp']})")
    print("== constraint extraction ==")
    for t, v in res["constraints"]["tiers"].items():
        print(f"  {t:12s} n={v['n']:3d} exact={v['exact_rate']:.3f} rule_recall={v['rule_recall']} spurious={v['spurious_rules']}")
    print("== confirmations ==", res["confirmations"]["accuracy"])


if __name__ == "__main__":
    main()
