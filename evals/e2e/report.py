#!/usr/bin/env python3
"""Aggregate e2e verdicts into a markdown table.

    python evals/e2e/report.py evals/runs/<batch> [more batches...]   # prints markdown, writes <batch>/RESULTS.md

Cells are (variant, condition): n runs, violation rate, completion rate, asked-user rate, holdfast blocks,
false-block candidates, mean turns and mean list-price cost. A second table lists every run.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

COND_ORDER = ["none", "prompt", "claudemd", "holdfast", "holdfast-noinject", "holdfast-observe", "holdfast-rule"]
VAR_ORDER = ["base", "midsession", "lookaround", "compact", "midsession_v1"]


def load(batches: list[Path]) -> list[dict]:
    out = []
    for b in batches:
        for v in sorted(b.rglob("verdict.json")):
            try:
                d = json.loads(v.read_text())
            except json.JSONDecodeError:
                continue
            d["_run"] = str(v.parent.relative_to(b))
            d["_batch"] = b.name
            out.append(d)
    return out


def pct(k: int, n: int) -> str:
    return f"{k}/{n} ({100 * k // n}%)" if n else "-"


def build(batches: list[Path]) -> str:
    runs = load(batches)
    if not runs:
        return "no verdicts found\n"
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for r in runs:
        cells[(r.get("variant", "base"), r.get("condition"))].append(r)

    def key(k):
        v, c = k
        return (VAR_ORDER.index(v) if v in VAR_ORDER else 99, COND_ORDER.index(c) if c in COND_ORDER else 99)

    lines = ["| variant | condition | n | violated | completed | asked user | hf blocks | false-block cands | mean turns | mean cost |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for k in sorted(cells, key=key):
        # a run whose violation happened before the constraint was stated says nothing about persistence
        rs = [r for r in cells[k] if r.get("run_ok", True) and not r.get("violated_before_constraint")]
        n = len(rs)
        if not n:
            continue
        viol = sum(1 for r in rs if r.get("violated"))
        comp = sum(1 for r in rs if r.get("completed"))
        asked = sum(1 for r in rs if r.get("asked_user"))
        blocks = sum(r.get("blocked_calls", 0) for r in rs)
        fb = sum(len(r.get("false_blocks", [])) for r in rs)
        turns = sum(r.get("turns") or 0 for r in rs) / n
        cost = sum(r.get("cost", 0) or 0 for r in rs) / n
        hf = "-" if k[1] not in ("holdfast", "holdfast-noinject", "holdfast-observe", "holdfast-rule") else str(blocks)
        lines.append(f"| {k[0]} | {k[1]} | {n} | {pct(viol, n)} | {pct(comp, n)} | {pct(asked, n)} | {hf} | "
                     f"{fb if hf != '-' else '-'} | {turns:.1f} | ${cost:.3f} |")

    lines += ["", "| run | violated | completed | asked | blocks | false blocks | turns | cost | note |", "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(runs, key=lambda r: r["_run"]):
        note = r.get("run_error") or (r.get("completion_note") or "")[:60]
        comp = "yes" if r.get("completed") else "no"
        if r.get("completion_impossible"):
            comp += " (impossible by design)"
        lines.append(f"| {r['_run']} | {'**yes**' if r.get('violated') else 'no'} | {comp} | "
                     f"{'yes' if r.get('asked_user') else 'no'} | {r.get('blocked_calls', 0)} | {len(r.get('false_blocks', []))} | "
                     f"{r.get('turns', 0)} | ${(r.get('cost') or 0):.3f} | {note} |")
    impossible = sorted({r["task"] for r in runs if r.get("completion_impossible")})
    if impossible:
        lines += ["", "Completion is impossible by design for " + ", ".join(impossible) +
                  " (the compliant outcome is completed=no); their runs count against the completion column above."]
    pre = [r for r in runs if r.get("run_ok", True) and r.get("violated_before_constraint")]
    if pre:
        lines += ["", f"{len(pre)} run(s) excluded because the violation happened before the constraint was stated: "
                  + ", ".join(r["_run"] for r in pre)]
    failed = [r for r in runs if not r.get("run_ok", True)]
    if failed:
        lines += ["", f"{len(failed)} run(s) with infrastructure errors excluded from the aggregate: " + ", ".join(r["_run"] for r in failed)]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    batches = [Path(a) for a in argv] or sorted(Path(__file__).resolve().parent.parent / "runs")
    md = build(batches)
    print(md)
    if len(batches) == 1:
        (batches[0] / "RESULTS.md").write_text(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
