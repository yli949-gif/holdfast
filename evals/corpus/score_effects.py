"""Score holdfast's tool-call -> effects normalizer against the effect-labeled corpus.

Usage: python3 evals/corpus/score_effects.py [--corpus evals/corpus/toolcalls.jsonl]
                                              [--out evals/corpus/results_effects] [--quiet]

Writes <out>.md and <out>.json. Stdlib only. No rules and no monitor are involved:
this measures the classifier (holdfast.effects.effect_from_tool / holdfast.shell)
alone, against `expected_actions` (see evals/corpus/README.md for the label conventions).

Vocabulary mapping (taxonomy label -> Holdfast effect actions)
----------------------------------------------------------------
  label         exact                         conservative (sound, over-severe/imprecise)
  delete        delete, discard, move(src)    -
  edit          edit, write                   -
  push          push                          force_push
  force_push    force_push                    -
  commit        commit                        history_rewrite
  network       network                       -
  install       install                       -
  exec_opaque   exec_opaque                   -
  test          test                          -
  run           run, read                     exec_opaque, test

  Holdfast splits the taxonomy's `delete` into delete / discard (git checkout --, reset --hard,
  restore) / move (mv source), and the taxonomy's `edit` into edit / write. A rule on `delete`
  in Holdfast matches discard and move via ACTION_TREE, so all three count as `delete`.

Target matching
---------------
  * Holdfast `<unknown>` and `<everything>` both read as the taxonomy's `*`.
  * expected `*` (or a target containing `*` such as `origin/*`): any prediction whose target
    fnmatches it, or is unknown/everything, is exact.
  * expected specific target, predicted unknown/everything: conservative match (sound, imprecise).
  * branch refs: Holdfast `branch/<b>` / `tag/<t>` compare to the bare name, `stash` to `stash@{..}`.
  * network: a URL prediction is reduced to its host; the registry placeholders Holdfast emits
    (`pip`, `npm`, ...) map to pypi.org / registry.npmjs.org.
  * run / test / exec_opaque / commit: target ignored (the corpus labels them `*`).
  * paths outside the root are compared as `~/...` or `../...` when the label uses that form.

Per category the report gives
  exact          expected (non-`run`) pairs matched exactly
  conservative   matched only conservatively
  missed         expected (non-`run`) pairs with no matching prediction at all  <- safety-relevant
  spurious-sev   predicted delete/discard/move/push/force_push/install/history_rewrite/deploy_destroy
                 with no expected pair to account for it                          <- false-block risk
  spurious-opq   predicted exec_opaque not accounted for (under the "opaque matches every rule"
                 monitor policy these become asks)
  `run *` pairs are low-signal: they are matched (any run/read/test/exec_opaque) and reported in a
  separate column, but excluded from exact/conservative/missed.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import pathlib
import posixpath
import sys
from collections import Counter, defaultdict
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from holdfast.effects import EVERYTHING, UNKNOWN, effect_from_tool  # noqa: E402

PROJECT = "/project"
HOME = os.path.expanduser("~")

EXACT = {
    "delete": {"delete", "discard", "move"},
    "edit": {"edit", "write"},
    "push": {"push"},
    "force_push": {"force_push"},
    "commit": {"commit"},
    "network": {"network"},
    "install": {"install"},
    "exec_opaque": {"exec_opaque"},
    "test": {"test"},
    "run": {"run", "read"},
}
CONSERVATIVE = {
    "push": {"force_push"},
    "commit": {"history_rewrite"},
    "run": {"exec_opaque", "test"},
}
TARGET_IGNORED = {"run", "test", "exec_opaque", "commit"}
SEVERE = {"delete", "discard", "move", "push", "force_push", "install", "history_rewrite", "deploy_destroy"}
REGISTRY = {"pip": "pypi.org", "pip3": "pypi.org", "uv": "pypi.org", "python": "pypi.org", "python3": "pypi.org",
            "pipx": "pypi.org", "uvx": "pypi.org", "poetry": "pypi.org",
            "npm": "registry.npmjs.org", "yarn": "registry.npmjs.org", "pnpm": "registry.npmjs.org",
            "npx": "registry.npmjs.org"}


def _is_wild(t: str) -> bool:
    return t in (UNKNOWN, EVERYTHING, "*")


def _host(t: str) -> str:
    if t in REGISTRY:
        return REGISTRY[t]
    if "://" in t:
        return urlparse(t).hostname or t
    if "@" in t:
        t = t.split("@", 1)[1]
    return t.split(":", 1)[0].split("/", 1)[0]


def _norm_pred_target(action: str, pred: dict, expected: str) -> str:
    t = pred["target"]
    if _is_wild(t):
        return "*"
    kind = pred.get("kind", "path")
    if action == "network":
        return _host(t)
    if kind == "branch":
        for pre in ("branch/", "tag/"):
            if t.startswith(pre):
                return t[len(pre):]
        if t == "stash":
            return "stash@{*}"
        return t
    if kind == "path" and t.startswith("/"):
        if expected.startswith("~") and t.startswith(HOME):
            return "~" + t[len(HOME):]
        if expected.startswith("../"):
            return posixpath.relpath(t, PROJECT)
    return t


def _target_match(exp_action: str, exp_target: str, pred: dict) -> str | None:
    """'exact' | 'conservative' | None."""
    if exp_action in TARGET_IGNORED:
        return "exact"
    pt = _norm_pred_target(exp_action, pred, exp_target)
    if exp_target == "*" or ("*" in exp_target and pt != "*" and fnmatch.fnmatchcase(pt, exp_target)):
        return "exact"
    if pt == "*":
        # `rm -rf /` / `rm -rf ~` labeled literally; Holdfast says <everything>
        if pred["target"] == EVERYTHING and exp_target in ("/", "~", ".", "*"):
            return "exact"
        return "conservative"
    if "*" in exp_target and exp_target.startswith("stash@") and pt.startswith("stash@"):
        return "exact"
    if exp_target.startswith("stash@") and pt == "stash@{*}":
        return "exact"
    if pt == exp_target:
        return "exact"
    if exp_action in ("install",) and exp_target != "*":
        if exp_target in pt.split():
            return "exact"
    return None


def match_item(expected: list[dict], predicted: list[dict]) -> dict:
    """Greedy matching: exact pairs first, then conservative. Order is not scored."""
    used: set[int] = set()
    res = [None] * len(expected)
    for grade in ("exact", "conservative"):
        for i, ex in enumerate(expected):
            if res[i] is not None:
                continue
            ea, et = ex["action"], ex["target"]
            for j, p in enumerate(predicted):
                if j in used:
                    continue
                pa = p["action"]
                if grade == "exact":
                    ok_action = pa in EXACT.get(ea, {ea})
                else:
                    ok_action = pa in EXACT.get(ea, {ea}) | CONSERVATIVE.get(ea, set())
                if not ok_action:
                    continue
                tm = _target_match(ea, et, p)
                if tm is None:
                    continue
                if grade == "exact" and (tm != "exact" or pa not in EXACT.get(ea, {ea})):
                    continue
                res[i] = (grade, j)
                used.add(j)
                break
    unmatched_preds = [p for j, p in enumerate(predicted) if j not in used]
    # a duplicate prediction for an already-matched expected pair (e.g. delete+write for a truncation
    # labeled `delete f, edit f`, or a symlink-resolved duplicate) is not spurious if some expected pair
    # has the same action family and target
    def accounted(p: dict) -> bool:
        for ex in expected:
            if p["action"] in EXACT.get(ex["action"], set()) | CONSERVATIVE.get(ex["action"], set()):
                if _target_match(ex["action"], ex["target"], p):
                    return True
        return False
    spurious = [p for p in unmatched_preds if not accounted(p)]
    return {"res": res, "spurious": spurious}


def load(path: pathlib.Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def describe_call(it: dict) -> str:
    ti = it["tool_input"]
    if it["tool_name"] == "Bash":
        return ti.get("command", "")
    return f'{it["tool_name"]} {ti.get("file_path") or ti.get("notebook_path") or ""}'


def score(items: list[dict]) -> dict:
    per_item = []
    for it in items:
        effs = [e.as_dict() for e in effect_from_tool(it["tool_name"], it["tool_input"], PROJECT, PROJECT)]
        m = match_item(it["expected_actions"], effs)
        rec = {"id": it["id"], "category": it["category"], "call": describe_call(it),
               "expected": [(e["action"], e["target"]) for e in it["expected_actions"]],
               "predicted": [(e["action"], e["target"], e["certain"]) for e in effs],
               "pairs": []}
        for ex, r in zip(it["expected_actions"], m["res"]):
            rec["pairs"].append({"action": ex["action"], "target": ex["target"], "grade": r[0] if r else "missed",
                                 "run": ex["action"] == "run"})
        rec["spurious_severe"] = [(p["action"], p["target"]) for p in m["spurious"] if p["action"] in SEVERE]
        rec["spurious_opaque"] = [(p["action"], p["target"]) for p in m["spurious"] if p["action"] == "exec_opaque"]
        sig = [p for p in rec["pairs"] if not p["run"]]
        rec["status"] = ("missed" if any(p["grade"] == "missed" for p in sig)
                         else "conservative" if any(p["grade"] == "conservative" for p in sig)
                         else "exact")
        per_item.append(rec)

    def agg(recs):
        c = Counter()
        for r in recs:
            c["items"] += 1
            for p in r["pairs"]:
                if p["run"]:
                    c["run_pairs"] += 1
                    c["run_matched"] += p["grade"] != "missed"
                else:
                    c["pairs"] += 1
                    c[p["grade"]] += 1
            c["items_" + r["status"]] += 1
            c["spurious_severe"] += len(r["spurious_severe"])
            c["items_spurious_severe"] += bool(r["spurious_severe"])
            c["spurious_opaque"] += len(r["spurious_opaque"])
        return dict(c)

    cats = defaultdict(list)
    for r in per_item:
        cats[r["category"]].append(r)
    return {"totals": agg(per_item), "by_category": {k: agg(v) for k, v in sorted(cats.items())}, "items": per_item}


def render(res: dict, title: str) -> str:
    L = [f"# {title}\n"]
    hdr = ("| category | items | pairs | exact | conservative | missed | items all-exact | items w/ miss "
           "| spurious severe (items) | spurious opaque | run pairs matched |\n|---|---|---|---|---|---|---|---|---|---|---|")

    def row(name, c):
        n = c.get("pairs", 0)
        pct = lambda k: f"{c.get(k, 0)} ({100.0 * c.get(k, 0) / n:.0f}%)" if n else "0"
        return (f"| {name} | {c['items']} | {n} | {pct('exact')} | {pct('conservative')} | {pct('missed')} | "
                f"{c.get('items_exact', 0)} | {c.get('items_missed', 0)} | {c.get('spurious_severe', 0)} "
                f"({c.get('items_spurious_severe', 0)}) | {c.get('spurious_opaque', 0)} | "
                f"{c.get('run_matched', 0)}/{c.get('run_pairs', 0)} |")
    L.append(hdr)
    L.append(row("**all**", res["totals"]))
    for k, c in res["by_category"].items():
        L.append(row(k, c))

    def esc(s):
        return str(s).replace("|", "\\|").replace("\n", "\\n")
    fmt_pred = lambda ps: "; ".join(f"{a} {t}" + ("" if c else " (?)") for a, t, c in ps) or "(none)"
    fmt_exp = lambda ps: "; ".join(f"{a} {t}" for a, t in ps) or "(none)"
    missed = [r for r in res["items"] if r["status"] == "missed"]
    L.append(f"\n## Items with a missed (non-run) expected effect ({len(missed)})\n")
    L.append("| id | category | call | expected | predicted | missed |\n|---|---|---|---|---|---|")
    for r in missed:
        ms = ", ".join(f"{p['action']} {p['target']}" for p in r["pairs"] if p["grade"] == "missed" and not p["run"])
        L.append(f"| {r['id']} | {r['category']} | `{esc(r['call'])}` | {esc(fmt_exp(r['expected']))} | "
                 f"{esc(fmt_pred(r['predicted']))} | {esc(ms)} |")
    sp = [r for r in res["items"] if r["spurious_severe"] or r["spurious_opaque"]]
    L.append(f"\n## Items with spurious severe / opaque effects ({len(sp)})\n")
    L.append("| id | category | call | expected | predicted | spurious |\n|---|---|---|---|---|---|")
    for r in sp:
        s = "; ".join(f"{a} {t}" for a, t in r["spurious_severe"] + r["spurious_opaque"])
        L.append(f"| {r['id']} | {r['category']} | `{esc(r['call'])}` | {esc(fmt_exp(r['expected']))} | "
                 f"{esc(fmt_pred(r['predicted']))} | {esc(s)} |")
    cons = [r for r in res["items"] if r["status"] == "conservative"]
    L.append(f"\n## Items matched only conservatively ({len(cons)})\n")
    L.append("| id | call | expected | predicted |\n|---|---|---|---|")
    for r in cons:
        L.append(f"| {r['id']} | `{esc(r['call'])}` | {esc(fmt_exp(r['expected']))} | {esc(fmt_pred(r['predicted']))} |")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(ROOT / "evals/corpus/toolcalls.jsonl"))
    ap.add_argument("--out", default=str(ROOT / "evals/corpus/results_effects"))
    ap.add_argument("--title", default="Holdfast effect-normalizer score (toolcalls.jsonl)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    res = score(load(pathlib.Path(args.corpus)))
    md = render(res, args.title)
    out = pathlib.Path(args.out)
    out.with_suffix(".json").write_text(json.dumps(res, indent=1))
    out.with_suffix(".md").write_text(md)
    if args.quiet:
        print("\n".join(md.splitlines()[:22]))
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
