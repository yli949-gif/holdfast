# Holdfast — Specification v0.1 (Day 0)

Holdfast is a stateful constraint guard for coding agents. It captures constraints a user states in natural language (at session start **or mid-session**), compiles them into small state machines, enforces them through Claude Code hooks at every tool call, and keeps rule + state **outside the model context** so context compaction and subagent handoffs cannot erase them.

The thesis under test: *prompt-level constraints decay (compaction, handoff, long sessions); hook-level stateful constraints do not.*

## 1. Non-goals (v0.1)
- Not a general policy engine (no Cedar/OPA). Rules are a small closed vocabulary.
- Not a prompt-injection classifier.
- Not a sandbox. Holdfast decides allow/deny/ask for tool calls; it does not isolate the process.
- Not "perfect Bash parsing". Known-hard cases are documented as limitations and measured, not hidden.

## 2. Rule schema (`holdfast/rules.py`)

```json
{
  "id": "r1",
  "kind": "forbid | confirm | order | scope",
  "action": "delete | force_push | push | commit | edit_path | run_cmd | network | install | touch_prod | any",
  "target": "<optional glob or category argument, e.g. 'tests/**' or 'prod'>",
  "requires": "<for order: 'tests_passed' | 'user_confirmed'>",
  "once": true,
  "source": {"turn": 3, "text": "don't delete anything without asking me", "via": "pattern|llm|manual"},
  "created_at": "ISO8601",
  "active": true
}
```

Kinds:
- **forbid**: action (optionally restricted to target) is always denied.
- **confirm**: action is denied until the user has explicitly confirmed; confirmation is a one-shot token (consumed by the first matching allowed call) unless `once: false`.
- **order**: action is denied unless the `requires` predicate currently holds (e.g. `tests_passed`). Predicates are invalidated by relevant events (any Edit/Write/Bash-write resets `tests_passed`).
- **scope**: edit_path outside `target` glob (or inside, for `forbid`+`edit_path`) is denied. `scope` with target means "edits allowed ONLY under target".

Action categories and what triggers them (minimum set; the corpus tests these):
| action | Bash triggers | Edit/Write/MultiEdit/NotebookEdit triggers |
|---|---|---|
| delete | `rm`, `rmdir`, `unlink`, `shred`, `find … -delete`, `find … -exec rm`, `git clean`, `git rm`, `git checkout -- .`/`git restore` (discard), `git reset --hard`, `git branch -D`, `trash`, `truncate -s 0`, `: >` / `> file` (truncating redirect of existing file), `xargs rm`, `mv … /dev/null`, `python -c "...os.remove/shutil.rmtree..."`, `perl/node -e unlink` | Write with empty content over existing file (truncate) |
| force_push | `git push -f/--force/--force-with-lease`, `git push … +ref` | — |
| push | `git push` | — |
| commit | `git commit` | — |
| edit_path | writes via redirection `>`, `>>`, `tee`, `sed -i`, `cp/mv` *to* path, `touch`, `install`, `dd of=`, `patch`, `git apply`, `python -c open(...,'w')`, heredoc `cat > path <<EOF`, `echo … > path` | file_path of Edit/Write/MultiEdit/NotebookEdit |
| run_cmd | any Bash whose first word matches target (e.g. `docker`, `kubectl`, `terraform`) | — |
| network | `curl`, `wget`, `nc`, `ssh`, `scp`, `rsync` to remote, `pip install` (also install), `npm install` | — |
| install | `pip/pip3/uv pip install`, `npm/pnpm/yarn add|install`, `apt-get install`, `brew install`, `cargo add` | — |
| touch_prod | any Bash containing a prod marker: `--prod`, `production`, `prod` as env/namespace/host/branch token (`-n prod`, `NODE_ENV=production`, `main`/`master` push when rule says prod==main), target-defined markers | Edit/Write on paths matching prod target glob (e.g. `config/production*`) |

Bash **decomposition** requirements (so bypasses are caught): split on `;`, `&&`, `||`, `|`, newlines; look inside `sh -c "…"`, `bash -c`, `eval "…"`, `$(…)`, backticks, heredocs; strip `sudo`, `env VAR=…`, `time`, `nohup`, `xargs`; resolve `~`, `$HOME`, `$PWD`, relative paths against `cwd`; detect quoting/escaping tricks (`r""m`, `\rm`, `'r'm`) by shell-unquoting before classification; treat unknown opaque shells (`python - <<EOF` with file ops, `base64 -d | sh`) as **suspicious** → `ask` (not allow) when any confirm/forbid rule on delete/edit_path exists.

## 3. State (`.holdfast/state.json`, project-scoped, atomic write)
```json
{
  "version": 1,
  "rules": [...],
  "predicates": {"tests_passed": false, "task_started": true},
  "confirmations": [{"action": "delete", "target": null, "granted_turn": 7, "remaining": 1}],
  "events": "append-only JSONL path .holdfast/events.jsonl",
  "meta": {"created_by_session": "…", "last_turn": 7}
}
```
Rules and state never live only in context. Every hook invocation loads the file, decides, mutates, and writes atomically (tempfile + rename). File lock via `fcntl` to survive parallel subagents.

## 4. Hook contract (`holdfast hook <event>` reads stdin JSON, writes stdout JSON)
- **UserPromptSubmit**: extract constraints (pattern extractor first; optional `--llm` extractor via `claude -p --model haiku` returning JSON; off by default in tests). Also detect **confirmations** ("yes, delete it", "go ahead", "approved: rm the cache dir") → grant one-shot confirmation for the pending action. Output `additionalContext` listing active rules (short) so the model also knows — but enforcement never depends on this text.
- **PreToolUse** (matcher `Bash|Edit|Write|MultiEdit|NotebookEdit`): classify the call into actions+paths; evaluate rules → `allow` / `deny` (with `permissionDecisionReason` that tells the agent exactly what to do: "Holdfast: rule r2 'confirm before delete'. Ask the user for explicit confirmation, then retry.") / `ask` for suspicious-opaque. Log decision event.
- **PostToolUse** (Bash): if command is a test runner (`pytest`, `npm test`, `go test`, `cargo test`, `make test`, …) → `tests_passed = true`. If Edit/Write/MultiEdit succeeded, or Bash classified edit_path → `tests_passed = false`.
- **PostToolUseFailure** (Bash): test runner failed → `tests_passed = false`.
- **PreCompact**: snapshot state (already on disk; write `compact_count += 1`), never block.
- **SessionStart** (`startup|resume|compact|fork`): reload state; emit `additionalContext` with active rules (re-anchoring is a *bonus*, not the mechanism).
- **SubagentStart**: emit `additionalContext` with active rules; state file is shared so enforcement is automatic.
- **Stop**: no-op (optionally summarise decisions).
Exit code 0 always with JSON; exit 2 only on internal failure when `HOLDFAST_FAIL_CLOSED=1`.

## 5. Acceptance criteria (measurable)
A1. Corpus: on `corpus/*.jsonl` (labelled tool calls), missed-violation rate (FN) ≤ 5% and false-block rate (FP) ≤ 5% for the **plain** tier; FN ≤ 20% on the **adversarial** tier, with every miss documented.
A2. Persistence: rules created at turn t are enforced after (i) simulated compaction (SessionStart compact), (ii) `claude -p --resume`, (iii) inside a subagent. Tested with real `claude -p`.
A3. Statefulness: `confirm` rule denies before confirmation, allows exactly once after, denies again. `order` rule denies push before tests pass, allows after, denies again after a new edit.
A4. Extraction: pattern extractor recovers ≥ 80% of rules on `corpus/constraints.jsonl` (NL sentences → expected rule); zero rules extracted from non-constraint sentences in the negative set.
A5. Overhead: median PreToolUse hook latency < 50 ms (no LLM in the hot path).
A6. E2E baseline comparison on ≥ 6 tasks × 3 conditions (prompt-only, CLAUDE.md, holdfast) with real `claude -p`: report violation rate, false-block count, completion.

## 6. Package layout
```
src/holdfast/
  __init__.py
  rules.py        # dataclasses, (de)serialisation, validation
  bashparse.py    # command decomposition + unquoting + path resolution
  classify.py     # ToolCall -> set[Action(category, paths, confidence)]
  engine.py       # evaluate(rules, state, actions) -> Decision(allow|deny|ask, reason, rule_id)
  state.py        # load/save/lock, predicates, confirmations, events log
  extract.py      # NL -> rules (pattern); extract_llm.py optional
  hooks.py        # event handlers; CLI entry `holdfast hook <event>`
  cli.py          # `holdfast add/list/remove/status/install`
tests/            # pytest, one file per module + test_hooks_e2e.py (subprocess with fake stdin)
corpus/           # labelled jsonl, independent of implementation
evals/            # runner for real claude -p experiments + results/
hooks/            # settings.json snippet + plugin manifest
```
