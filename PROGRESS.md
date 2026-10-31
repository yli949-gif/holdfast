# Holdfast — progress ledger

Rules for every work cycle: one unfinished task at a time; ≤15 min per cycle; commit + update this file at the end
of each cycle; reuse recorded results (every eval under `evals/` has a results file — do not rerun unless the code
it measures changed); stuck twice → stop and report. No paid API calls; all `claude -p` runs go through the Max
subscription (every run reports its list-price equivalent in `meta.json`).

## Status 2026-10-31 (end of cycle 1 of the "verify and compare" phase)

### Done (do not redo)
| area | evidence | state |
|---|---|---|
| Guard core: NL → rules, shell/tool → effects, stateful monitor, state store, hooks, CLI | `holdfast/*.py` (~5k LOC), 1002 tests green (`python -m pytest -q`, 1.6 s) | done |
| Constraint-extraction corpus + held-out + blind set, scorer, LLM/hybrid fallback eval | `evals/corpus/`, `corpus/heldout/`, `evals/results/extract.md` | done; recall on unseen phrasing is the known weak spot (65/108 pattern-only) |
| Effect classifier corpus (plain + adversarial tiers) | `evals/corpus/results.md`, `results_effects.md` | done |
| Red team (93 attacks + 21 benign, executed for real) | `evals/redteam/REPORT.md`: bypass 43 → 2 → 1 after hardening | done |
| Independent review round 1 + hardening commit | `docs/REVIEW.md`, commit 8bca22e | done |
| **Independent review round 2** (re-verifies round 1, 16 new findings) | `docs/REVIEW-2.md`: 13 fixed / 11 partial / 14 open; new: 1 Critical, 6 High, 6 Med, 3 Low | done — findings NOT yet fixed |
| **Baseline comparison vs existing approaches** (stateless regex hook strict/lenient, Claude Code native permission rules, Holdfast) on red team + blind corpus + 47-step stateful scenarios | `evals/baselines/RESULTS.md`, `results.json`; `python evals/baselines/run_baselines.py` (45 s, offline) | done |
| E2E thesis experiment v1 (6 tasks × C0–C4 × base/mid/compact/sub/long, real `claude -p`) | `evals/results/e2e/REPORT.md`, `BUGS.md` (B1–B6) | done; verdict: strong form of thesis NOT supported at affordable session lengths; enforcement-only (C4) holds 9/9 classifiable attempts |
| E2E v2 runner + 14 tasks; base matrix (T1–T14), compact, holdfast-rule (enforcement-only) batches | `evals/runs/{base1,base2,compact1,rule1}/RESULTS.md` | done |
| E2E v2 midsession variant redesigned (survey → constraint → task; v1 graded pre-constraint violations, now excluded by `violated_before_constraint`) and run on 9 tasks × 4 conditions | `evals/runs/mid2/RESULTS.md`: none 8/9 violated; prompt / holdfast / holdfast-noinject 0/9; 0 false blocks | done |

### In flight
- `evals/runs/look2` (lookaround variant, T10–T14 × prompt/holdfast × 2 trials) running in the background; look1 was mostly lost to rate-limit back-offs. Check `evals/runs/look2.log`, then `python evals/e2e/report.py evals/runs/look2`.

### Missing (priority order)
1. **Fix REVIEW-2 top findings** — N1 (state lives in the work tree: `git stash -u`/`git clean -fdx` wipe the rules; committed state leaks the audit log) is Critical and invalidates the persistence claim; then N2/N3 (untargeted, non-expiring "yes"), N4/N5 (partial test run unlocks push; merge/pull doesn't invalidate), N6 (regression: `python -c 'import <project module>'` treated as plain run), N7 (holds lapse on any 3-word message). One finding per cycle, each with a regression test; re-run only the affected eval (`evals/redteam`, `evals/baselines`, scenario suite) afterwards.
2. **README.md** (root) — problem, design, results-at-a-glance table (base / midsession / compact / enforcement-only / baselines), honest failures (REVIEW-2 open items, false-block rate on blind corpus, thesis verdict), reproduce commands. Numbers must be copied from the results files, not retyped.
3. Reduce false blocks / asks on benign developer workflows (REVIEW-2: 12/52 benign calls blocked or asked; blind corpus 69 FP, 66 of them asks) — one targeted fix cycle after the README exists.
4. Demo GIF (constraint added mid-session → compaction → still blocked) and plugin install path check.
5. Project doc `holdfast/decision-and-plan.md` in the Claude project: append a results summary once README is final.

### Known limits to state plainly in the README
- Prompt-level constraints did not decay under 1–2 compactions or a subagent hand-off with Claude Code 2.1.3xx + Sonnet 5.5 (compaction summary keeps user instructions verbatim; model relays constraints to subagents). Holdfast's measured value is (a) when the constraint has left the context entirely (C4: 12/12 attempts, 9/9 classifiable ones denied) and (b) the turn-boundary failure (1/3 trials deleted files one turn after "ask me first").
- Extraction recall on unseen phrasing; opaque interpreter edits; latency 63 ms median (spec target 50 ms; 6 ms inside the hook, the rest is Python start-up).
