# Holdfast

Holdfast makes a coding agent keep the rules you give it.

You tell Claude Code, in plain English: *"Only push after the test suite passes."* An hour later the agent's
context has been compacted (summarised to save space), or a subagent is doing the work, and it runs
`git push`. Holdfast sees the command before it runs and stops it:

```
agent $ git push origin main
  ✗ DENY Holdfast rule R-eb9066: push only after tests pass. No passing test run recorded in this
         session; run the test suite first.
```

The agent gets the reason, so it can run the tests and try again. You wrote no config file. The rule came from
your sentence.

## Why

Coding agents break the rules developers give them. In one study of 20,574 real coding-agent sessions, violating
the developer's constraints was the most common failure (38.3%, arXiv 2605.29442).

There are two common defences today, and each has a gap:

- **Put the rule in the prompt.** It lives in the model's context, so the model can ignore it, or it can be lost
  when the context is summarised or handed to a subagent.
- **A deny-list hook** (a script that blocks commands matching a pattern, like `rm -rf`). It has no memory, so it
  can't express "only *after* the tests pass", "*until* I say go" or "*unless* I said yes".

Holdfast is built for both gaps. It keeps the rule **and its state** (have the tests passed? did the user say yes?)
outside the model, on disk, and checks them on every tool call.

## Try it

You need Python 3.10+, git, and Claude Code. Holdfast has no runtime dependencies.

```bash
git clone https://github.com/yli949-gif/holdfast
cd holdfast
python3 -m venv .venv && .venv/bin/pip install .
.venv/bin/python evals/demo.py      # offline, about a second, no model calls
```

The demo plays a short scripted session through the real hook. Each allowed command really runs in a throwaway
repo. You will see:

- a push denied before any test run, then denied again after a run of one test file (not the whole suite);
- the rules surviving a context compaction;
- a rule added mid-session ("never modify anything under tests/") stopping a subagent;
- `rm notes.txt` held until the user says "yes, delete notes.txt", then allowed once;
- a final push denied even though the full suite passed, because the deletion was never committed: the code
  being pushed is not the code that was tested.

## Use it

Install the hooks in your own repository:

```bash
cd /path/to/your-repo
/path/to/holdfast/.venv/bin/holdfast install --local
```

`--local` writes to `.claude/settings.local.json`, which stays out of git. Leave it off to write
`.claude/settings.json` and share the hooks with your team. The hooks call the Python inside that `.venv`, so
keep it where it is.

Then start Claude Code in the repository and state your rules as you normally would. Holdfast tells the agent
which rules it registered, and `holdfast status` shows them to you. Sentences it understands include:

| you say | Holdfast enforces |
|---|---|
| Only push after the test suite passes. | push only after a full, passing test run of the code being pushed |
| Don't delete anything without asking me. | each delete waits for your "yes", which counts once |
| Never modify anything under tests/. | no edits, writes, moves or deletes under `tests/` |
| Only edit files under app/. | edits outside `app/` are blocked |
| Never force-push. / Never push to main. | blocked outright |
| Don't install any packages. / Work offline. | installs / network access blocked |
| Don't change anything yet. | a temporary hold that ends with your next request |

A vague sentence like "be careful with the database" produces no rule. Check `holdfast status` if a rule
matters to you.

The command line covers the rest (put `.venv/bin` on your `PATH`, or use the full path):

```bash
holdfast rules add "Never force-push."           # a standing rule for this repository
holdfast rules list
holdfast explain -- "git push -f origin main"    # dry run: what would Holdfast decide, and why?
holdfast status                                  # rules and pending confirmations of the latest session
holdfast audit                                   # every decision so far
```

To remove Holdfast, delete its entries from `.claude/settings.local.json` (or `settings.json`).

## How it works

Claude Code has **hooks**: commands it runs at fixed moments in a session. One runs before every **tool call**
(each shell command, file edit or file write the agent makes) and can answer *allow*, *deny* or *ask the user*.
Holdfast plugs into these hooks:

```
your message ──► extract rules from plain English ─────────────────────────────┐
                                                                                ▼
tool call ─────► work out its effects ──► check effects against rules + state ──► allow / deny / ask
                 (what will it delete,                                          (with a reason the
                  edit, push, install?)                                          agent can act on)
tool result ───► update state: did the tests pass? which files did the agent create?
compaction, subagent start ──► restate the active rules in the agent's context
```

Three pieces do the work.

**Rules.** A small fixed vocabulary: *forbid* an action, *confirm* it with the user first, allow it only *after*
something is true (the tests passed), or limit edits to a *scope* (a set of paths). By default rules come from
a pattern extractor, not a model, so the same sentence always gives the same rule.

**Effects.** A rule says "don't delete". A command rarely says "delete". So Holdfast parses each shell command and
works out what it will actually do. It follows pipes, `&&` chains, heredocs, `sh -c`, scripts, Makefile and
`package.json` recipes, `python -c` and Python imports, and git commands that rewrite files. `sed -i 'd' notes.txt`
counts as deleting `notes.txt`. Code it can't read, like `curl … | sh`, gets an *ask*.

**State.** Rules, confirmations and test results live in `.git/holdfast/` in your repository. Git never tracks,
stashes, cleans or resets that folder. The agent can't edit it either: Holdfast denies changes to its own state,
its hook settings, `.git/hooks` and its installed code. A copy outside the repository records which sessions
had rules. If the state disappears anyway, Holdfast **fails closed**: it blocks changes until you restate your
rules and say `holdfast: reset`.

"The tests passed" has a strict meaning. The run must be a known test runner (pytest, jest, go test, cargo test
and others), cover the whole suite, and pass. Holdfast also records exactly which code was tested and compares
it with what a push would send, so editing after the run, or pushing a different branch, does not count.

Files the agent created itself in the session can be deleted without asking, since removing them loses nothing
of yours.

## What we found

Everything below was measured, and every result has its denominator. Some evaluations were written by
independent agents that never saw the code. Only the *first* run on such a set counts as unseen. After we fix the
bugs it finds, re-running it gives better but optimistic numbers, so we report both.

- **Better than the alternatives on an unseen test set.** An independent agent, with no access to the code,
  wrote and labelled a set of tool calls under 10 rules. On the 8 rules every approach can express, Holdfast's
  first run missed 8 of 129 violations.
  A strict deny-list hook missed 39, and a model of Claude Code's built-in permission rules missed 54.
- **It also blocks harmless work.** On the same first run, across all 10 rules, Holdfast blocked or asked about
  18 of 152 harmless calls. On a larger, older test set it has more false blocks than the simpler guards
  (64 of 1,698, mostly asks on code it can't read).
- **Hard to get around.** In 93 bypass attempts executed for real (scripts, interpreters, quoting tricks, git
  options), none got through. This set was used during development, so it is not unseen.
- **The original idea was only half right.** We expected rules in the prompt to fade after compaction. In real
  Claude Code sessions they held: 0 of 8 runs broke a prompt rule across one to three automatic compactions.
  Holdfast mattered when the
  rule was *not* in the agent's context. Then the hook was the only thing in the way: 0 of 16 runs broke the rule,
  against 12 of 16 on similar tasks with no rule at all.

Full results, methods and every error, listed one by one:

| what | where |
|---|---|
| unseen test set vs. deny-list and built-in permissions | [`evals/blind3/RESULTS.md`](evals/blind3/RESULTS.md) |
| independent 135-scenario suite (first run 128/135; every failure fixed) | [`evals/review3/FINDINGS.md`](evals/review3/FINDINGS.md) |
| red team, older test sets, stateful scenarios, latency | [`evals/baselines/RESULTS.md`](evals/baselines/RESULTS.md), [`evals/redteam/REPORT.md`](evals/redteam/REPORT.md) |
| real Claude Code sessions | [`evals/results/e2e_summary.md`](evals/results/e2e_summary.md) |
| rule extraction from plain English | [`evals/results/extract.md`](evals/results/extract.md) |
| two adversarial code reviews and their fixes | [`docs/REVIEW.md`](docs/REVIEW.md), [`docs/REVIEW-2.md`](docs/REVIEW-2.md) |
| work log | [`PROGRESS.md`](PROGRESS.md) |

## Limitations

- **It is a guard, not a sandbox.** It decides on the tool calls Claude Code shows it. It does not isolate the
  process or watch the network, and it cannot stop anything that happens outside those tool calls.
- **It understands fewer phrasings than you might hope.** On sentences written by someone else it extracted the
  right rule for 65 of 108. Almost every miss produces no rule at all, so check `holdfast status`.
- **Rules about specific operations are weak.** "Don't run database migrations" is matched mostly by command
  text, so some indirect migrations slip through, and some harmless look-alikes are blocked. This was the largest
  error source on the unseen set.
- **Unreadable code gets an ask.** `curl … | sh`, `npx` tools and some project scripts make Holdfast ask the
  user under every active rule.
- **It adds 63–92 ms to each tool call,** mostly Python start-up.
- **"Tests passed" compares code, not environments.** Untracked files and anything outside the repository are not
  part of the comparison.
- **It protects its own installed code,** so developing Holdfast with Holdfast installed blocks edits to it.

Open review items are listed in [`docs/REVIEW-2.md`](docs/REVIEW-2.md).

## Reproduce

```bash
python -m pytest -q tests                 # 1,170 tests
python -m pytest -q evals/review3         # 135 independent scenarios
python evals/blind3/score.py              # unseen test set, 4 approaches
python evals/baselines/run_baselines.py   # red team (runs the attacks), older sets, scenarios (~45 s)
python evals/extract_eval.py --split heldout
```

These run offline with no model calls. The real-session runs (`evals/e2e/run.py`) and live probes
(`evals/e2e/hardening_live.py`) use your Claude Code login. Their raw transcripts are not published; the runners
regenerate them locally.

## Code

| file | what |
|---|---|
| `holdfast/rules.py` | plain English → rules |
| `holdfast/shell.py`, `effects.py`, `testruns.py` | tool call → effects; what counts as a full test run |
| `holdfast/monitor.py` | rules × effects × state → decision |
| `holdfast/state.py`, `hook.py`, `cli.py` | on-disk state, hook entry point, installer and CLI |
| `docs/SPEC.md` | the design |

MIT license.
