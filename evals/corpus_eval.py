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
import argparse, json, os, pathlib, statistics, sys, tempfile, time
from collections import Counter, defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from holdfast.classify import classify  # noqa: E402
from holdfast.extract import extract, extract_confirmations  # noqa: E402

CWD = "/proj"


CORPUS_DIR = ROOT / "corpus"


def load(name):
    return [json.loads(l) for l in (CORPUS_DIR / name).read_text().splitlines() if l.strip()]


_TMP_ROOT = None


def _materialise(it):
    """Copy an item's /proj tree shape into a temp dir: create the files its expected paths name."""
    global _TMP_ROOT
    if _TMP_ROOT is None:
        _TMP_ROOT = tempfile.mkdtemp(prefix="holdfast-corpus-")
    src_cwd = it.get("cwd", CWD)
    cwd = os.path.join(_TMP_ROOT, it["id"], src_cwd.strip("/"))
    rewrite = lambda s: s.replace(src_cwd, cwd, 1) if isinstance(s, str) and s.startswith(src_cwd) else s
    tool_input = {k: rewrite(v) for k, v in it["tool_input"].items()}
    for p in it["expected"].get("paths", []):
        q = rewrite(p)
        os.makedirs(os.path.dirname(q), exist_ok=True)
        with open(q, "w") as fh:
            fh.write("existing content\n")
    return cwd, tool_input, True


def score_toolcalls(verbose):
    items = load("toolcalls.jsonl")
    per_tier = defaultdict(lambda: Counter())
    cat_conf = defaultdict(lambda: Counter())  # category -> tp/fn/fp
    misses, falses, path_misses, latencies = [], [], [], []
    for it in items:
        exp = set(it["expected"].get("categories", []))
        optional = set(it.get("debatable_categories", []) or it["expected"].get("debatable_categories", []))
        if it.get("prod_if_main") or it["expected"].get("prod_if_main"):
            optional.add("touch_prod")
        cwd, tool_input, exp_paths_override = it.get("cwd", CWD), it["tool_input"], None
        if it["tool_name"] == "Write" and tool_input.get("content") == "" and "delete" in exp:
            # SPEC §2: an empty Write is a truncation only *over an existing file*; existence is a runtime
            # fact, so (corpus/README.md §1) the runner materialises the file in a throwaway tree and
            # rewrites cwd / paths accordingly. Added in round 2 -- round 1 scored these without the file.
            cwd, tool_input, exp_paths_override = _materialise(it)
        t0 = time.perf_counter()
        acts = classify(it["tool_name"], tool_input, cwd, home="/")
        latencies.append((time.perf_counter() - t0) * 1000)
        if exp_paths_override is not None:
            for a in acts:  # map temp-tree paths back to the corpus' /proj so path scoring is unchanged
                a.paths = [p.replace(cwd, it.get("cwd", CWD), 1) if p.startswith(cwd) else p for p in a.paths]
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
            else:
                path_misses.append((it["id"], tier, it["tool_input"].get("command") or it["tool_input"].get("file_path"),
                                    sorted(exp_paths - got_paths), sorted(got_paths)))
    out = {"tiers": {}, "categories": {}, "misses": misses, "false_blocks": falses, "path_misses": path_misses,
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
        print("\nPATH MISSES (expected path not among detected paths; category may still be right):")
        for m in path_misses:
            print("  ", *m)
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
    ap.add_argument("--out", default=None)
    ap.add_argument("--corpus", default="corpus", help="corpus or corpus/heldout")
    a = ap.parse_args()
    global CORPUS_DIR
    CORPUS_DIR = ROOT / a.corpus
    a.out = a.out or str(ROOT / "evals/results" / (a.corpus.replace("/", "_") + ".json"))
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
