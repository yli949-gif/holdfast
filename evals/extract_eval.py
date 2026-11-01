"""Score the NL -> rule extractors on labelled corpora.

Main corpus: evals/corpus/constraints.jsonl (v2 schema; see LABELS below). Split into a frozen
dev / held-out split by sha1(id) rank (60/40). Develop only against `--split dev`.

Secondary corpora: corpus/constraints.jsonl and corpus/heldout/constraints.jsonl (v1 schema:
kind in forbid/confirm/order/scope, action in the nine v1 categories). They are scored at the
(kind, action) level only, after mapping v2 rules onto v1 keys (see `v1_keys`).

Extractors:
  pattern  holdfast.rules.extract_rules (deterministic)
  llm      holdfast.rules.extract_rules_llm (`claude -p --model haiku`), cached by sha(text)
  hybrid   pattern; if it returns nothing and the text has a modal/negation cue, the LLM

Usage:
  python3 evals/extract_eval.py --split dev                       # pattern only
  python3 evals/extract_eval.py --split heldout --llm --llm-limit 15
  python3 evals/extract_eval.py --v1                              # also score the v1 corpora
  python3 evals/extract_eval.py --split dev --show                # print every miss

LABELS (v2): each expected rule is {kind, action, paths?, exclude_paths?, branches?, pattern?,
command?, prerequisite?, expires?}. Missing fields mean empty. `paths`/`exclude_paths` are compared
as sets after collapsing a known alias expansion (rules._PATH_ALIASES) to its first glob;
`branches` after dropping the "*/x" duplicates. `pattern` / `command` in a label are EXAMPLE
strings that the predicted regex must match (an empty label requires an empty regex).
{"prod": true, "kind": K} is shorthand for the production bundle:
K modify /prod/, K deploy /prod/, K run (command matches "kubectl apply -n production"), and (since 2026-10-31)
K network /prod/ (push to a production branch/remote, production hosts and endpoints).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
import time
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from holdfast.rules import _PATH_ALIASES, Rule, extract_rules, extract_rules_llm, has_constraint_cue  # noqa: E402

CORPUS = ROOT / "evals/corpus/constraints.jsonl"
V1 = [ROOT / "corpus/constraints.jsonl", ROOT / "corpus/heldout/constraints.jsonl"]
CACHE = ROOT / "evals/results/extract_llm_cache.json"


def _h(item_id: str) -> str:
    return hashlib.sha1(item_id.encode()).hexdigest()


_DEV_IDS: set | None = None


def split_of(item_id: str) -> str:
    """Frozen 60/40 split: the 60% of corpus ids with the smallest sha1(id) are dev, the rest held out."""
    global _DEV_IDS
    if _DEV_IDS is None:
        ids = sorted((json.loads(l)["id"] for l in CORPUS.read_text().splitlines() if l.strip()), key=_h)
        _DEV_IDS = set(ids[: round(0.6 * len(ids))])
    return "dev" if item_id in _DEV_IDS else "heldout"


# ------------------------------------------------------------------ canonicalisation

def _canon_paths(paths) -> frozenset:
    ps = list(paths or [])
    out = set(ps)
    for globs in _PATH_ALIASES.values():
        if len(globs) > 1 and set(globs) <= out:
            out -= set(globs)
            out.add(globs[0])
    return frozenset(out)


def _canon_branches(bs) -> frozenset:
    return frozenset(b for b in (bs or []) if not (b.startswith("*/") and b[2:] in bs))


def expand_label(rules: list[dict]) -> list[dict]:
    out = []
    for r in rules:
        if r.get("prod"):
            k = r["kind"]
            out += [dict(kind=k, action="modify", pattern="production"), dict(kind=k, action="deploy", pattern="production"),
                    dict(kind=k, action="run", command="kubectl apply -n production"),
                    dict(kind=k, action="network", pattern="production")]
        else:
            out.append(r)
    return out


def rule_eq(pred: Rule, lab: dict) -> bool:
    sc = pred.scope
    if (pred.kind, pred.action) != (lab["kind"], lab["action"]):
        return False
    if (pred.prerequisite or "") != lab.get("prerequisite", "") or (getattr(pred, "expires", "") or "") != lab.get("expires", ""):
        return False
    if _canon_paths(sc.paths) != _canon_paths(lab.get("paths")):
        return False
    if _canon_paths(getattr(sc, "exclude_paths", [])) != _canon_paths(lab.get("exclude_paths")):
        return False
    if _canon_branches(sc.branches) != frozenset(lab.get("branches", [])):
        return False
    for fld in ("pattern", "command"):
        want, got = lab.get(fld, ""), getattr(sc, fld) or ""
        if bool(want) != bool(got) or (want and not re.search(got, want, re.I)):
            return False
    return True


def set_match(preds: list[Rule], labs: list[dict]) -> bool:
    if len(preds) != len(labs):
        return False
    used = set()
    for lab in labs:
        hit = next((i for i, p in enumerate(preds) if i not in used and rule_eq(p, lab)), None)
        if hit is None:
            return False
        used.add(hit)
    return True


def n_matched(preds: list[Rule], labs: list[dict]) -> int:
    used, n = set(), 0
    for lab in labs:
        hit = next((i for i, p in enumerate(preds) if i not in used and rule_eq(p, lab)), None)
        if hit is not None:
            used.add(hit)
            n += 1
    return n


def ka(rules) -> Counter:
    return Counter((r.kind, r.action) if isinstance(r, Rule) else (r["kind"], r["action"]) for r in rules)


# ------------------------------------------------------------------ extractors

_cache: dict | None = None


def llm_cached(text: str, model: str = "haiku", allow_call: bool = True):
    """Returns (rules, latency_s, cached) or None when not cached and calls are not allowed."""
    global _cache
    if _cache is None:
        _cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    key = hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()[:24]
    if key in _cache:
        e = _cache[key]
        return [Rule.from_dict(r) for r in e["rules"]], e["latency_s"], True
    if not allow_call:
        return None
    t0 = time.time()
    rules = extract_rules_llm(text, model=model, timeout=120)
    lat = round(time.time() - t0, 2)
    _cache[key] = {"text": text, "model": model, "rules": [r.as_dict() for r in rules], "latency_s": lat}
    CACHE.write_text(json.dumps(_cache, indent=1))
    return rules, lat, False


# ------------------------------------------------------------------ scoring

def score(items, predict) -> dict:
    s = Counter()
    misses = []
    for it in items:
        preds = predict(it)
        if preds is None:
            continue
        labs = expand_label(it["expected"])
        alts = [expand_label(a) for a in it.get("alt_expected", [])]
        s["n"] += 1
        neg = not labs
        s["neg" if neg else "pos"] += 1
        if any(set_match(preds, L) for L in [labs] + alts):
            s["exact"] += 1
            s["exact_neg" if neg else "exact_pos"] += 1
            best = next(L for L in [labs] + alts if set_match(preds, L))
            s["tp"] += len(best)
            s["pred"] += len(preds)
            s["gold"] += len(best)
            continue
        if neg:
            s["spurious_rules"] += len(preds)
            s["neg_fired"] += 1
        elif any(ka(preds) == ka(L) for L in [labs] + alts):
            s["partial"] += 1
        s["tp"] += max(n_matched(preds, L) for L in [labs] + alts)
        s["pred"] += len(preds)
        s["gold"] += len(labs)
        misses.append((it["id"], it["tier"], it["text"], [r.describe() + (f" [expires={r.expires}]" if getattr(r, "expires", "") else "") for r in preds],
                       labs))
    s["precision"] = round(s["tp"] / s["pred"], 3) if s["pred"] else 1.0
    s["recall"] = round(s["tp"] / s["gold"], 3) if s["gold"] else 1.0
    return {"stats": dict(s), "misses": misses}


def fmt(name: str, r: dict) -> str:
    s = r["stats"]
    pos, neg = s.get("pos", 0), s.get("neg", 0)
    return (f"| {name} | {s['n']} | {s.get('exact', 0)}/{s['n']} ({s.get('exact', 0) / max(s['n'], 1):.0%}) "
            f"| {s.get('exact_pos', 0)}/{pos} | {s.get('partial', 0)} | {s.get('neg_fired', 0)}/{neg} ({s.get('spurious_rules', 0)} rules) "
            f"| {s['precision']:.2f} | {s['recall']:.2f} |")


HEADER = ("| extractor / split | n | exact set match | exact (positives) | partial (kind+action right, scope/expiry wrong) "
          "| negatives that fired (spurious rules) | rule precision | rule recall |\n|---|---|---|---|---|---|---|---|")


# ------------------------------------------------------------------ v1 corpora (kind+action level)

V1_ACTION = {"modify": "edit_path", "edit": "edit_path", "write": "edit_path", "run": "run_cmd"}


def v1_keys(rules: list[Rule]) -> frozenset:
    out = set()
    for r in rules:
        sc = r.scope
        kind = {"require_before": "order", "freeze": "forbid"}.get(r.kind, r.kind)
        if re.search(r"prod", (sc.pattern or "") + (sc.command or "")):
            out.add((kind, "touch_prod"))
            continue
        if getattr(sc, "exclude_paths", None):
            out.add(("scope", "edit_path"))
            continue
        out.add((kind, V1_ACTION.get(r.action, r.action)))
    return frozenset(out)


def score_v1(path: pathlib.Path, extract) -> dict:
    items = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    s = Counter()
    misses = []
    for it in items:
        got = v1_keys(extract(it["text"]))
        accept = [frozenset((e["kind"], e["action"]) for e in it["expected"])] + \
                 [frozenset((e["kind"], e["action"]) for e in a) for a in it.get("alt_expected", [])]
        neg = it["tier"] == "negative"
        s["n"] += 1
        s["neg" if neg else "pos"] += 1
        if got in accept:
            s["exact"] += 1
            s["exact_neg" if neg else "exact_pos"] += 1
        else:
            if neg:
                s["neg_fired"] += 1
            misses.append((it["id"], it["text"], sorted(got), sorted(accept[0])))
    return {"stats": dict(s), "misses": misses}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(CORPUS.relative_to(ROOT)), help="labelled v2 corpus (the 60/40 split applies to the main corpus only)")
    ap.add_argument("--split", default="dev", choices=["dev", "heldout", "all"])
    ap.add_argument("--llm", action="store_true", help="also score llm + hybrid")
    ap.add_argument("--llm-limit", type=int, default=15, help="max NEW claude -p calls this run")
    ap.add_argument("--llm-sample", type=int, default=0, help="score the LLM on the first N items by sha1(id) rank (a fixed, un-cherry-picked sample)")
    ap.add_argument("--v1", action="store_true")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()

    corpus = ROOT / a.corpus
    items = [json.loads(l) for l in corpus.read_text().splitlines() if l.strip()]
    if corpus == CORPUS:
        items = [it for it in items if a.split == "all" or split_of(it["id"]) == a.split]
    else:
        a.split = "all"
    print(f"corpus={a.corpus} split={a.split} n={len(items)} "
          f"(pos={sum(1 for i in items if i['expected'])}, neg={sum(1 for i in items if not i['expected'])})")
    print(HEADER)
    res = {"pattern": score(items, lambda it: extract_rules(it["text"]))}
    print(fmt(f"pattern / {a.split}", res["pattern"]))

    if a.llm:
        budget = [a.llm_limit]
        lats = []
        only = {it["id"] for it in sorted(items, key=lambda it: _h(it["id"]))[: a.llm_sample]} if a.llm_sample else None

        def llm(it):
            if only is not None and it["id"] not in only:
                return None
            got = llm_cached(it["text"], allow_call=budget[0] > 0)
            if got is None:
                return None
            rules, lat, cached = got
            if not cached:
                budget[0] -= 1
            lats.append(lat)
            return rules

        llm_preds = {it["id"]: llm(it) for it in items}
        sub = [it for it in items if llm_preds[it["id"]] is not None]
        res["pattern_sub"] = score(sub, lambda it: extract_rules(it["text"]))
        res["llm"] = score(sub, lambda it: llm_preds[it["id"]])

        def hybrid(it):
            p = extract_rules(it["text"])
            return p if p or not has_constraint_cue(it["text"]) else llm_preds[it["id"]]
        res["hybrid"] = score(sub, hybrid)
        n_llm_hybrid = sum(1 for it in sub if not extract_rules(it["text"]) and has_constraint_cue(it["text"]))
        print(fmt(f"pattern / {a.split} LLM subset", res["pattern_sub"]))
        print(fmt(f"llm (haiku) / {a.split} LLM subset", res["llm"]))
        print(fmt(f"hybrid / {a.split} LLM subset", res["hybrid"]))
        if lats:
            ls = sorted(lats)
            print(f"\nLLM subset n={len(sub)}; latency per call: median {ls[len(ls) // 2]}s, min {ls[0]}s, max {ls[-1]}s; "
                  f"hybrid would call the LLM on {n_llm_hybrid}/{len(sub)} items")

    out = ROOT / "evals/results" / f"extract_{pathlib.Path(a.corpus).stem}_{a.split}.json"
    out.write_text(json.dumps({k: v for k, v in res.items()}, indent=1, default=str))

    if a.v1:
        print("\nv1 corpora, (kind, action) level only:")
        print("| corpus | n | exact | positives exact | negatives that fired |\n|---|---|---|---|---|")
        for p in V1:
            r = score_v1(p, extract_rules)
            s = r["stats"]
            print(f"| {p.relative_to(ROOT)} | {s['n']} | {s.get('exact', 0)}/{s['n']} ({s.get('exact', 0) / s['n']:.0%}) "
                  f"| {s.get('exact_pos', 0)}/{s['pos']} | {s.get('neg_fired', 0)}/{s['neg']} |")
            res[f"v1:{p.parent.name}"] = r
            if a.show and p == V1[0]:
                for m in r["misses"]:
                    print("   v1 MISS", m)

    if a.show:
        for name, r in res.items():
            if name.startswith("v1:"):
                continue
            print(f"\n## misses: {name}")
            for mid, tier, text, got, want in r["misses"]:
                print(f"- {mid} [{tier}] {text!r}\n    got:  {got}\n    want: {[{k: v for k, v in w.items()} for w in want]}")


if __name__ == "__main__":
    main()
