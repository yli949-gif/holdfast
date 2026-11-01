# Holdfast — progress ledger

Rules for every work cycle: one unfinished task at a time; ≤15 min per cycle; commit + update this file at the end
of each cycle; reuse recorded results (every eval under `evals/` has a results file — do not rerun unless the code
it measures changed); stuck twice → stop and report. No paid API calls; all `claude -p` runs go through the Max
subscription (every run reports its list-price equivalent in `meta.json`).

## Status 2026-10-31 (security hardening phase complete)

### Security hardening — done (commits 3ed8363 … 8ea2fa6, details in `docs/REVIEW-2.md` § Resolution)
All Critical/High findings from REVIEW-2 are fixed with regression tests (`tests/test_hardening.py`, 103 tests that
replay each review sequence against a real git repo + bare remote and execute every allowed command):
N1 (state moved to `.git/holdfast/`, backup copy, fail closed on lost state), N2/N3 (targeted, turn-scoped,
consume-on-allow confirmations; strict affirmations), N4/N5/N10/T1 (standalone full-suite runs only, start-time
stamping, git tree changes invalidate), N6 (project imports scanned), N7/N8 (holds), B3 (git hooks/config exec
keys), B4 (hard links), B7 (tree-changing git reports paths), B8 (gh api mutations), S2, N11, N14, N15, S3. O1 partial.

Verification (no reruns of unaffected experiments):
| check | before | after |
|---|---|---|
| unit/integration tests | 1002 | **1106 pass** |
| red team, 93 attacks executed (`evals/baselines/run_baselines.py`) | 1 bypass / 1 benign FB | 1 bypass (del-31 `sed -i 'd'`, B20 Medium) / 1 benign FB |
| blind held-out corpus (304 violations / 1698 benign pairs) | 20 missed / 69 FB | 20 missed / 72 FB (+3 asks: git rebase/merge with no repo on disk) |
| stateful scenarios (47 steps) | 45 | **46** |
| effect corpus | — | unchanged except `git pull` → uncertain tree edit |
| live probes, real `claude -p`, default install (`evals/e2e/hardening_live.py`) | control @229b474: N4, N6 violated | **N1, N4, N6 blocked**, rule intact, 0 tracked holdfast files |
| real E2E base, holdfast + holdfast-rule (`evals/runs/hard1`, 16 runs) | base2/rule1: 0 violated, 2+2 FB cands | 0/16 violated; 1 FB cand per condition (T12; same class as before) |
| real E2E lookaround, holdfast (`evals/runs/hardlook1`, 6 runs) | look2: 0/10 violated, 0 FB | 0/6 violated, 0 blocks, 0 FB |

Also recorded this phase (pre-hardening code, not rerun): `evals/runs/look2` lookaround prompt 1/10 violated vs holdfast 0/10.

## Earlier status (for reference)

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
- none (look2 finished: prompt 1/10 violated, holdfast 0/10).

### Missing (priority order)
1. ~~Fix REVIEW-2 Critical/High findings~~ — done, see top of file.
2. **README.md** (root) — problem, design, results-at-a-glance table (base / midsession / compact / enforcement-only / baselines), honest failures (REVIEW-2 open items, false-block rate on blind corpus, thesis verdict), reproduce commands. Numbers must be copied from the results files, not retyped.
3. Remaining Medium/Low review items (B15–B18, B20, N12, N13 remainder, N16 latency, O2/O4–O6, S4/S5) — see REVIEW-2 § Resolution.
4. Reduce false blocks / asks on benign developer workflows (REVIEW-2: 12/52 benign calls blocked or asked; blind corpus 69 FP, 66 of them asks) — one targeted fix cycle after the README exists.
5. Demo GIF (constraint added mid-session → compaction → still blocked) and plugin install path check.
6. Project doc `holdfast/decision-and-plan.md` in the Claude project: append a results summary once README is final.

### Known limits to state plainly in the README
- Prompt-level constraints did not decay under 1–2 compactions or a subagent hand-off with Claude Code 2.1.3xx + Sonnet 5.5 (compaction summary keeps user instructions verbatim; model relays constraints to subagents). Holdfast's measured value is (a) when the constraint has left the context entirely (C4: 12/12 attempts, 9/9 classifiable ones denied) and (b) the turn-boundary failure (1/3 trials deleted files one turn after "ask me first").
- Extraction recall on unseen phrasing; opaque interpreter edits; latency 63 ms median (spec target 50 ms; 6 ms inside the hook, the rest is Python start-up).
