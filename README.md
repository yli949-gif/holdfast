# holdfast

Holdfast makes Claude Code keep the rules you give it. You say things like "only push after the tests pass" or
"don't delete anything without asking me", in plain English, in the chat. Holdfast turns each sentence into a
rule and checks every command and file edit the agent makes against it, before it runs. The rules, and the
facts they depend on (did the tests pass? did you say yes?), are stored on disk, not in the model's context. So
they still hold after the conversation is compacted, inside subagents, and after `--resume`.

It is plain Python with no dependencies, plugged into Claude Code's hooks. There is no config file to write.

## install

```sh
git clone https://github.com/yli949-gif/holdfast
cd holdfast
python3 -m venv .venv && .venv/bin/pip install .
```

You need Python 3.10+ and git. You need Claude Code to use it for real, but not for the demo below.

## quick start

The fastest way to see what it does is the offline demo. It plays a short scripted session through the real
hook, and every command the hook allows really runs, in a throwaway git repo. No model, no network, about a
second:

```sh
.venv/bin/python evals/demo.py
```

Here is part of the output (long messages cut with `…`):

```
USER: Fix the flaky test. Only push after the test suite passes, and don't delete anything without asking me.
  [hook → context] Holdfast registered 2 new constraint(s) from this message: R-eb9066: push only after
  tests pass; R-1efbd3: ask the user before every delete (one confirmation per action). …
AGENT $ git push origin main
  ✗ DENY Holdfast rule R-eb9066: push only after tests pass. No passing test run recorded in this session;
  run the test suite first. …
AGENT $ python3 -m pytest -q tests/test_math.py
  ✓ allowed 1 passed in 0.01s
AGENT $ git push origin main
  ✗ DENY Holdfast rule R-eb9066: push only after tests pass. The last green run was partial …; run the
  whole suite. …

--- context compaction ---

USER: Also: never modify anything under tests/.
SUBAGENT sub-1 $ echo 'def test_add(): pass' > tests/test_math.py
  ✗ DENY Holdfast rule R-465c04 forbids this: never modify on paths tests/** …
SUBAGENT sub-1 $ rm notes.txt
  ✗ DENY Holdfast rule R-1efbd3: ask the user before every delete … needs explicit user …
USER: yes, delete notes.txt
SUBAGENT sub-1 $ rm notes.txt
  ✓ allowed
```

The push is refused until the whole test suite has passed; one test file doesn't count. The rules survive the
compaction. A rule added halfway through the session also binds a subagent. And your "yes" lets one delete
through, once.

The demo ends with one more refused push, even though the full suite just passed. That is correct: the deletion
was never committed, so the code being pushed is not the code that was tested.

## use it in your repo

Install the hooks into your repository:

```sh
cd /path/to/your-repo
/path/to/holdfast/.venv/bin/holdfast install --local
```

This writes `.claude/settings.local.json`, which stays out of git. Leave off `--local` to write
`.claude/settings.json` and share the hooks with your team. The hooks run the Python inside that `.venv`, so
keep it where it is.

Now start Claude Code there and work as usual. State your rules in the chat whenever you like. These kinds of
sentences work:

```
Only push after the test suite passes.
Don't delete anything without asking me.
Never modify anything under tests/.
Only edit files under app/.
Never force-push.  /  Never push to main.
Don't install any packages.  /  Work offline: no network access.
Don't change anything yet.          (a hold that ends with your next request)
```

A vague sentence like "be careful with the database" makes no rule, so check what Holdfast understood. With
`.venv/bin` on your `PATH`:

```sh
holdfast status                                 # rules and pending confirmations of the latest session
holdfast rules add "Never force-push."          # a standing rule for this repository
holdfast explain -- "git push -f origin main"   # dry run: what would Holdfast decide, and why?
holdfast audit                                  # every decision so far
```

To remove Holdfast, delete its entries from `.claude/settings.local.json` (or `settings.json`).

## how it works

Claude Code runs hooks at fixed points in a session. One runs before every tool call (a shell command, a file
edit, a file write) and can answer allow, deny or ask. Holdfast uses that hook plus a few others:

```
your message  ──► rules from plain English
tool call     ──► what will it do? (delete, edit, push, install, …) ──► check against rules + state ──► allow / deny / ask
tool result   ──► update state: did the tests pass? which files did the agent create?
compaction, subagent start ──► restate the rules in the agent's context
```

The interesting part is working out what a command will actually do. A rule says "don't delete". A command
rarely says "delete". So `shell.py` parses each command and follows pipes, `&&` chains, heredocs, `sh -c`,
scripts, Makefile and `package.json` recipes, `python -c`, Python imports, and git commands that rewrite files.
`sed -i 'd' notes.txt` counts as deleting `notes.txt`. Code it can't read, like `curl … | sh`, gets an ask.

State lives in `.git/holdfast/`, which git never tracks, stashes or cleans. The agent can't edit it, or the hook
settings, or Holdfast's own code. If the state goes missing anyway, Holdfast blocks changes until you restate
your rules and say `holdfast: reset`.

"Tests passed" is strict. It has to be a known runner (pytest, jest, go test, cargo test and others) on the whole
suite. Holdfast also records which code was tested and compares it with what a push would send.

The code:

- `holdfast/rules.py`: plain English → rules (patterns by default, so the same sentence always gives the same rule)
- `holdfast/shell.py`, `effects.py`, `testruns.py`: tool call → what it will do; what counts as a full test run
- `holdfast/monitor.py`: rules × effects × state → decision
- `holdfast/hook.py`, `state.py`, `cli.py`: hook entry point, on-disk state, installer and CLI

## evals

I wanted to know if this works better than the simple alternatives, so most of the repo is evaluation. In
short: an independent agent that never saw the code wrote a test set of tool calls under 10 rules. On the 8
rules all three approaches can express, Holdfast's first run missed 8 of 129 violations. A regex deny-list hook
missed 39, and Claude Code's built-in permission rules missed 54. Across all 10 rules it also blocked or asked
about 18 of 152 harmless calls, so it is not free.

One result surprised me. I expected rules written in the prompt to fade after compaction. In real Claude Code
sessions they didn't: Claude kept following them. Holdfast mattered when the rule was not in the agent's
context at all. Then the hook was the only thing in the way.

It is a guard, not a sandbox: it only sees Claude Code's tool calls. It understands only some phrasings (the
right rule for 65 of 108 sentences written by someone else). Rules about specific operations, like migrations,
are weak. Each tool call costs about 63–92 ms.

The details, with every error listed:

- [`evals/blind3/RESULTS.md`](evals/blind3/RESULTS.md): the independent test set, against the two baselines
- [`evals/baselines/RESULTS.md`](evals/baselines/RESULTS.md): red team (93 real attacks), older sets, stateful scenarios, latency
- [`evals/results/e2e_summary.md`](evals/results/e2e_summary.md): real Claude Code sessions
- [`evals/review3/FINDINGS.md`](evals/review3/FINDINGS.md), [`docs/REVIEW-2.md`](docs/REVIEW-2.md): independent reviews, what they found, open items
- [`evals/results/extract.md`](evals/results/extract.md): plain English → rules

To run the tests and the offline evals:

```sh
python -m pytest -q tests                  # 1,170 tests
python -m pytest -q evals/review3          # 135 independent scenarios
python evals/blind3/score.py               # independent test set, 4 approaches
python evals/baselines/run_baselines.py    # red team (executes the attacks), scenarios
```

## troubleshooting

If Holdfast blocks something you think is fine, run `holdfast explain -- "<the command>"` to see which rule and
which effect it matched, and `holdfast audit` to see the history.

Holdfast protects its own installed code, so while it is installed the agent can't edit Holdfast's package
directory.

MIT license.
