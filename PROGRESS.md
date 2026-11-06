# Holdfast — progress ledger

Chronological engineering log (newest first): what was done, how it was verified, and what is left. Evaluation
results are kept in the `evals/` directories next to the code that produces them; nothing here is re-measured.
The project was built with AI coding agents (Claude Code); real-agent experiments ran on a Claude subscription and
each run records its list-price equivalent in its `meta.json`.

## Status 2026-11-05 (release readiness, in progress)

Commits 37e7fcd … HEAD. Recovery checkpoint: everything below is committed and verified; next steps at the end.

- **Round 6** (37e7fcd): an independent pre-release review stopped before building its own attack harness (safety
  filter), but listed suspicions; each became a regression test against a real repo. Fixed: push of an untested
  tree (other branch / partial commit), Write/Edit of Holdfast's own code, `| head` hiding failures, selecting
  options/env and uncommitted test-config changes, stale pending confirmations, vanished state dir (out-of-tree
  sentinel), Edit-to-empty as delete, audit redaction.
- **review3** (7515344, frozen): independent pytest suite (135 scenarios, written without access to tests/ or prior
  reviews). First run 128/135; fixed in e7d81cc (2 Critical: script args/env invisible to prod rules — a regression
  from the O1 change; edit+commit+push in one call. 2 High: push --all; forked resume lost rules. 1 Medium) → 135/135.
- **Hygiene** (9b005d7): raw e2e transcripts (operator email, org id, paths) removed from the tree, LICENSE (MIT),
  gitignore, absolute paths scrubbed. They remain in git *history* → see "Owner decisions".
- **Install** (9ac7d98): clean non-editable install into a fresh venv verified; hook commands are now shell-quoted
  (an install under a path with spaces broke them).
- **Usability from live workflow probes** (a615f04, 2acae84): a runner that ends a chained command counts (summary
  line must be last); `cd sub && pytest` is partial; heredoc-written scripts are analysed from their content.

Verified at 2acae84: tests 1170 + review3 135 pass; red team 0/93 bypass, 3/21 benign FB; held-out 6/304, 64/1698;
legacy 6/326, 8/147; scenarios 47/47; blind3 2/160, 10/152; live probes on a clean install: attacks 4/4 blocked.

Next: real e2e batch on HEAD; scrubbed-history release clone; README refresh; final full regression.
Owner decisions pending: history rewrite (transcripts with personal data are in history), commit identity/email,
`Claude-Session:` trailers, LICENSE holder name, publishing.

## Status 2026-10-31 (rounds 3–5: bypass closed, error analysis, blind3, README)

Commits a198e69 … (this commit). Details: README "Results at a glance", `evals/blind3/RESULTS.md`, `docs/REVIEW-2.md` § Update.

- **Round 3** (a198e69): the last red-team bypass (del-31, `sed -i 'd'`) closed; error analysis on the *previously
  seen* held-out/legacy corpora → installer/commit/worktree/coreutils/ruby -i/prod-network misses fixed, read-only git
  and help probes no longer blocked, dependency-manifest edits ask.
- **blind3**: a fresh corpus written by an independent agent with no code access (336 items, 10 constraints, real
  fixture repo), frozen at 4fe79ea *before* the scorer existed. First scoring (f85f3da): Holdfast missed 10/160 = 6.2%,
  false blocks 18/152 = 11.8%; on the 8 comparable constraints 8/129 and 10/121 vs regex strict 39/129, 17/121 and
  native 54/129, 12/121.
- **Round 4** (739fcb7, post-hoc — blind3 is in-sample from here): Write-empty truncation, commit-creating git ops,
  MCP reads are network, local-path remotes are not, docs are not "production", manifest detection on added lines →
  blind3 2/160 missed, 10/152 false blocks. Regression found and reported: +1 held-out false block (`git merge --no-ff`
  under "never commit", a label disagreement).
- **Round 5** (this commit): independent fact-check of the README found 6 discrepancies + omissions (all corrected) and
  one real issue: `git stash -u` / `git clean -f` removed an *untracked* `.claude/settings*.json` (uninstalling the hooks
  for the next session) → now denied; `holdfast install --local` excludes the local settings file. "Never touch the
  production config." now extracts the prod bundle (T5 enforcement-only: 0/2 violated vs 2/2 unconstrained).

Verification at this commit: 1150 tests pass; red team 0/93 bypass, 1/21 benign FB; held-out 6/304 missed, 64/1698 FB;
legacy 6/326, 8/147; scenarios 46/47; blind3 2/160, 10/152; extraction unchanged (dev 86/86, held-out 57/57, v1 65/108);
real sessions hard1+hard2 (16+16 runs): 0 violations, 2 false-block runs (prompt+Holdfast), 15/16 correct denials
(Holdfast-only); live probes N1/N4/B20/N6 blocked (controls: N4, N6, B20 violated on old code).

Next (priority): operation-specific constraints (migrations: 2/12 missed, 5/15 FB on blind3); opaque-code asks
(O1 remainder); latency (63–92 ms); extraction recall on unseen phrasing (65/108); remaining Medium/Low review items.

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
2. ~~README.md~~ — done (round 5). Original item: (root) — problem, design, results-at-a-glance table (base / midsession / compact / enforcement-only / baselines), honest failures (REVIEW-2 open items, false-block rate on blind corpus, thesis verdict), reproduce commands. Numbers must be copied from the results files, not retyped.
3. Remaining Medium/Low review items (B15–B18, B20, N12, N13 remainder, N16 latency, O2/O4–O6, S4/S5) — see REVIEW-2 § Resolution.
4. Reduce false blocks / asks on benign developer workflows (REVIEW-2: 12/52 benign calls blocked or asked; blind corpus 69 FP, 66 of them asks) — one targeted fix cycle after the README exists.
5. Demo GIF (constraint added mid-session → compaction → still blocked) and plugin install path check.
6. Project doc `holdfast/decision-and-plan.md` in the Claude project: append a results summary once README is final.

### Known limits to state plainly in the README
- Prompt-level constraints did not decay under 1–2 compactions or a subagent hand-off with Claude Code 2.1.3xx + Sonnet 5.5 (compaction summary keeps user instructions verbatim; model relays constraints to subagents). Holdfast's measured value is (a) when the constraint has left the context entirely (C4: 12/12 attempts, 9/9 classifiable ones denied) and (b) the turn-boundary failure (1/3 trials deleted files one turn after "ask me first").
- Extraction recall on unseen phrasing; opaque interpreter edits; latency 63 ms median (spec target 50 ms; 6 ms inside the hook, the rest is Python start-up).
