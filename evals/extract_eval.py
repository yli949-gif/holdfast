"""Compare constraint extractors on a corpus: pattern, llm (haiku via `claude -p`), and hybrid (union).

Usage: python evals/extract_eval.py --corpus corpus/heldout [--model haiku] [--workers 6]
Writes evals/results/<corpus>_extract.json. LLM calls go through the local `claude` CLI
(subscription usage, no API key); results are cached in evals/results/cache/ by sha of text+model.
"""
from __future__ import annotations
import argparse, hashlib, json, pathlib, sys, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from holdfast.extract import extract  # noqa: E402
from holdfast.extract_llm import extract_llm  # noqa: E402

CACHE = ROOT / "evals/results/cache"


def llm_cached(text, model):
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()[:24]
    f = CACHE / f"{key}.json"
    if f.exists():
        return json.loads(f.read_text())
    t0 = time.time()
    rules = extract_llm(text, model=model, timeout=90)
    out = {"rules": rules, "latency_s": round(time.time() - t0, 2)}
    f.write_text(json.dumps(out))
    return out


def keyset(rules):
    return {(r["kind"], r["action"]) for r in rules}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="corpus/heldout")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    items = [json.loads(l) for l in (ROOT / a.corpus / "constraints.jsonl").read_text().splitlines() if l.strip()]
    with ThreadPoolExecutor(a.workers) as ex:
        llm = list(ex.map(lambda it: llm_cached(it["text"], a.model), items))
    per = {name: defaultdict(Counter) for name in ("pattern", "llm", "hybrid")}
    mism = {name: [] for name in per}
    lat = [r["latency_s"] for r in llm]
    for it, lr in zip(items, llm):
        exp = keyset(it["expected"])
        alts = [keyset(x) for x in it.get("alt_expected", [])] if it.get("alt_expected") else []
        accept = [exp] + alts
        got = {"pattern": keyset(extract(it["text"])), "llm": keyset(lr["rules"])}
        got["hybrid"] = got["pattern"] | got["llm"]
        for name, g in got.items():
            c = per[name][it["tier"]]
            c["n"] += 1
            c["exp_rules"] += len(exp)
            c["recovered"] += len(exp & g)
            c["spurious"] += len(g - exp) if it["tier"] == "negative" else 0
            if any(g == e for e in accept):
                c["exact"] += 1
            else:
                mism[name].append((it["id"], it["tier"], it["text"][:90], sorted(exp), sorted(g)))
    res = {"model": a.model, "llm_latency_s": {"median": sorted(lat)[len(lat) // 2], "max": max(lat)}, "tiers": {}, "mismatches": mism}
    for name in per:
        res["tiers"][name] = {t: {"n": c["n"], "exact_rate": round(c["exact"] / c["n"], 3),
                                  "rule_recall": round(c["recovered"] / c["exp_rules"], 3) if c["exp_rules"] else None,
                                  "spurious_on_negatives": c["spurious"]} for t, c in per[name].items()}
    out = ROOT / "evals/results" / (a.corpus.replace("/", "_") + f"_extract_{a.model}.json")
    out.write_text(json.dumps(res, indent=2))
    print(f"model={a.model} llm latency median={res['llm_latency_s']['median']}s")
    for name, tiers in res["tiers"].items():
        for t, v in tiers.items():
            print(f"  {name:8s} {t:10s} n={v['n']:3d} exact={v['exact_rate']:.3f} recall={v['rule_recall']} spurious_neg={v['spurious_on_negatives']}")


if __name__ == "__main__":
    main()
