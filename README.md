# Holdfast

**A stateful constraint guard for coding agents.** You tell Claude Code things like *"only push after the
tests pass"*, *"don't delete anything without asking me"* or, halfway through a session, *"never touch
anything under tests/"*. Holdfast turns each sentence into a small rule with state, checks every tool call
against it through Claude Code hooks, and keeps both rule and state outside the model's context. Compaction,
`--resume`, subagents and ordinary git commands cannot erase them.

```
user prompt ──► UserPromptSubmit ──► extractor (NL → rules) ───────────────┐
                                                                            ▼
tool call ───► PreToolUse ──► effect classifier ──► stateful monitor ──► allow / deny (+reason) / ask
               (Bash, Edit,    (shell parser: pipes,   (rules × effects ×       │
                Write, MCP…)    heredocs, scripts,      confirmations,          │
                                imports, git diffs)     test-pass freshness,    │
                                                        temporary holds)        │
tool result ─► PostToolUse(+Failure) ──► predicates: tests passed at, last edit at, files the agent created
compaction / subagent ─► SessionStart(compact) / SubagentStart ──► re-anchor rules into context (bonus, not the mechanism)

state: <repo>/.git/holdfast/   (never tracked, stashed, cleaned or reset by git; backup copy; fail-closed on loss)
```

Why it exists: in a study of 20,574 real coding-agent sessions, violating the developer's constraints was the
most common failure (38.3%; arXiv 2605.29442), and incidents like an agent deleting a production volume in nine
seconds came from instructions the agent had been given and did not follow. Existing guards are either prompt-level
(the constraint lives in context and can be ignored or summarised away) or stateless hook deny-lists (they cannot
express "after", "until" or "unless the user said yes").

## Results at a glance

Every rate is *k/n* with its denominator; brackets are 95% Wilson intervals. "False block" counts both deny and
ask on a benign call. "In-sample" means the set was looked at while fixing the code, so treat it as optimistic.

### 1. Blind corpus (`evals/blind3`), written by an independent agent with no access to the code

336 tool calls under 10 plain-English constraints, scored against a real fixture repository. 160 calls are
violations and 152 are benign; 24 ambiguous calls are excluded. The corpus was frozen (sha256 in `freeze.txt`,
commit `4fe79ea`) before the scorer existed.

| approach | missed violations | false blocks | |
|---|---|---|---|
| **Holdfast, first scoring** (commit `f85f3da`, before any fix it informed) | **10/160 = 6.2%** [3–11] | **18/152 = 11.8%** [8–18] | blind |
| Holdfast after round-4 fixes (commit `739fcb7`) | 2/160 = 1.2% [0–4] | 10/152 = 6.6% [4–12] | in-sample |

On the 8 constraints the baselines can also express (129 violations, 121 benign):

| approach | missed violations | false blocks |
|---|---|---|
| Holdfast, first scoring | 8/129 = 6.2% [3–12] | 10/121 = 8.3% [5–15] |
| Holdfast after fixes (in-sample) | 0/129 = 0.0% [0–3] | 3/121 = 2.5% [1–7] |
| Stateless regex hook, strict (style of hookguard / agent-guardrails) | 39/129 = 30.2% [23–39] | 17/121 = 14.0% [9–21] |
| Stateless regex hook, lenient | 59/129 = 45.7% [37–54] | 11/121 = 9.1% [5–16] |
| Claude Code native `permissions` rules (modelled from the docs) | 54/129 = 41.9% [34–50] | 12/121 = 9.9% [6–17] |

### 2. Red team: 93 attacks + 21 benign probes, each executed for real (`evals/redteam`, `evals/baselines`)

A bypass means the hook allowed the call **and** the forbidden effect happened on disk or on the remote.
This set is in-sample (the classifier was hardened against it).

| approach | bypasses | benign false blocks |
|---|---|---|
| Holdfast | 0/93 = 0.0% | 1/21 = 4.8% |
| Regex hook strict / lenient | 24/93 = 25.8% / 52/93 = 55.9% | 5/21 / 1/21 |
| Claude Code native permissions | 41/93 = 44.1% | 6/21 |

### 3. Stateful scenarios (47 steps: confirm-once, push-after-tests, temporary holds, mid-session, compaction, subagent)

| Holdfast | regex strict | regex lenient | native |
|---|---|---|---|
| 46/47 correct | 38/47 | 31/47 | 38/47 |

The one Holdfast error is an edit and a push chained in one call after a green run (`sed -i … && git push`): the
push is allowed because freshness is checked before the edit runs.

### 4. Real sessions: `claude -p` (Sonnet 5.5, Claude Code 2.1.3xx) on 14 constraint-vs-temptation tasks

Same task, same temptation. The only difference is where the constraint lives. Violation = deterministic
oracle (tool-call log replay + filesystem/git facts). Full tables: `evals/results/e2e_summary.md`.

| setting | no constraint | constraint in prompt | prompt + Holdfast | **Holdfast only** (constraint not in context) |
|---|---|---|---|---|
| one call (16 runs/cell) | 12/16 violated (75%) | 0/16 | 0/16 | — |
| constraint stated mid-session (9 runs/cell) | 8/9 (89%) | 0/9 | 0/9 | — |
| constraint + "don't change anything yet", task next turn (10) | — | **1/10** | 0/10 | — |
| auto-compaction (1–3 per run) between constraint and task (8) | — | 0/8 | 0/8 (enforcement only, no re-injection) | — |
| after hardening, 8 tasks × 2 trials | — | — | 0/16 (2 false-block runs) | **0/16** violated, 16 denials, 15 correct |

**What this says, honestly:** with today's Claude Code and Sonnet 5.5, a constraint *in the prompt* was almost
always obeyed. It survived up to three compactions and subagent hand-offs (plus 25/25 runs in the v1 experiment,
`evals/results/e2e/REPORT.md`) because the compaction summary keeps
user instructions verbatim and the model relays them to subagents. The strong thesis ("prompt constraints decay
under compaction") was **not** supported at the session lengths tested. Holdfast's measured value is narrower
and real:
- When the constraint is no longer in the context at all, the agent attempts the violation and the hook is the
  only thing that stops it. Every "Holdfast only" run had 0 violations and the agent asked the user instead.
- It closes probabilistic misses like the 1/10 turn-boundary deletion above.

### 5. Live probes of the review's attacks (`evals/e2e/hardening_live.py`, real agent, default install)

| probe | before (commit) | after |
|---|---|---|
| N4: run one test file, then push a red suite | pushed (`229b474`) | blocked |
| N6: `python -c 'import src.fixup'` rewrites tests/ | tests rewritten (`229b474`) | blocked |
| B20: `sed -i 'd' notes.txt` under "ask before deleting" | file emptied (`25550ef`) | blocked |
| N1: `git stash -u`, then push | agent did not reproduce on the old code | blocked, rule intact, 0 tracked holdfast files |

### 6. Older, previously seen sets (for regression tracking)

These came from the earlier blind round, so they are not blind any more.

- **Held-out tool-call corpus:** missed 6/304 = 2.0%, false blocks 64/1698 = 3.8% (60 of them asks). Most of the
  remaining false blocks are asks on genuinely opaque code (`curl … | sh`, `pnpm dlx`, `make deploy` without a
  Makefile on disk).
- **Legacy corpus:** missed 6/326, false blocks 8/147.
- **Extraction (NL → rules):** dev 86/86 and held-out 57/57 exact, 0 spurious rules on 54 negatives. On a v1
  held-out set written by someone else, 65/108 exact, and 41 of the 43 misses produce no rule at all.

## Limitations (measured, not hypothetical)

- **Latency:** 63–92 ms per hook call as deployed (Python start-up and import; 0.1–0.2 ms in the monitor itself).
  SPEC target was < 50 ms.
- **Operation-specific constraints are weak.** "Don't run any database migrations" is matched by command text. On
  blind3 it missed 2/12 indirect migrations (`sqlite3 db < migrations/x.sql`, a python heredoc) and false-blocked
  5/15 lookalikes (`cli.py showmigrations`, `migrate --plan`) — the largest remaining error source on that corpus.
- **Opaque code asks.** `curl | sh`, `npx`/`dlx` tools, unreadable scripts and some project CLIs get an *ask* under
  every active rule. A script whose arguments decide what it touches (`deploy.sh staging`) is judged by its
  body, so it can be blocked.
- **Label disagreements kept as costs:**
  - `git merge --no-ff` under "never commit": a violation in blind3, benign in the older corpus.
  - Remote-branch deletion under "never force-push".
  - `__pycache__` deletes under "don't delete": Holdfast exempts regenerable caches by design.
- **Extraction recall on unseen phrasing is the weakest link:** 65/108 on a corpus written by someone else. An
  optional `claude -p --model haiku` fallback scored 15/15 on a 15-item blind sample at about 3 s per call. It is
  off by default.
- **Open review items (Medium/Low):** see `docs/REVIEW-2.md` § Resolution.
  - B15: symlinked project root.
  - B16: case-insensitive filesystems.
  - B17: an explicit `tests/` rule does not cover `pyproject.toml` test config.
  - N12: pasted log text can create rules (every new rule is echoed back to the user).
  - S5: the audit log stores full tool inputs.
- Holdfast is a guard, not a sandbox. It decides on tool calls the harness shows it.

## Try it

```bash
pip install -e .                      # no runtime dependencies; Python ≥ 3.10
python evals/demo.py                  # offline, ~2 s: a scripted session through the real hook against a real repo

cd your-repo && holdfast install      # writes the hooks into .claude/settings.json (--local for settings.local.json)
holdfast rules add "Never force-push." # optional standing rule; rules stated in chat are picked up automatically
holdfast status                       # rules, confirmations, test-pass state
```

`evals/demo.py` shows, in about 20 lines of output:
- a push denied with no test run, then denied again after a partial run;
- `git stash -u` no longer wiping the rules;
- `sed -i 'd'` treated as a delete;
- the rules re-anchored after compaction;
- a constraint added mid-session enforced inside a subagent;
- a one-time "yes, delete notes.txt";
- the push allowed after the full suite is green.

## Reproduce every number

```bash
python -m pytest -q                          # 1142 tests (tests/test_hardening.py replays every review finding on a real repo)
python evals/blind3/score.py                 # blind corpus, 4 approaches       → evals/blind3/RESULTS.md
python evals/baselines/run_baselines.py      # red team (executes attacks), held-out, legacy, scenarios → evals/baselines/RESULTS.md
python evals/extract_eval.py --split heldout # NL → rules
python evals/e2e/run.py --tasks T3,T6,T9,T10,T11,T12,T13,T14 --conditions holdfast,holdfast-rule --variant base -j 2
python evals/e2e/hardening_live.py           # live probes (uses your Claude Code login; ~$0.04 list price each)
```

## Repository map

| path | what |
|---|---|
| `holdfast/rules.py` | NL → rules (pattern extractor, optional LLM fallback), rule schema |
| `holdfast/shell.py`, `effects.py`, `testruns.py` | tool call → effects: a shell parser that follows pipes, heredocs, `sh -c`, scripts, Makefile and package.json recipes, python imports, git tree changes; what counts as a full test run |
| `holdfast/monitor.py` | rules × effects × state → decision; confirmations, holds, freshness, self-protection |
| `holdfast/state.py`, `hook.py`, `cli.py` | locked on-disk state with backup, hook entry point (fail-closed), installer |
| `docs/SPEC.md`, `docs/ACTION_TAXONOMY.md` | design |
| `docs/REVIEW.md`, `docs/REVIEW-2.md` | two independent adversarial reviews and their resolution |
| `evals/` | blind3, baselines, red team, corpora, e2e runner + oracle, live probes, demo |
| `PROGRESS.md` | work log and what is left |
