# Holdfast action taxonomy (v0)

Every tool call a coding agent makes is normalized into zero or more
`(action, target)` pairs. Rules are written over these pairs, never over raw
strings, so one rule covers `rm`, `find -delete`, `python -c "os.remove(...)"`
and `git clean -fdx` alike.

## Actions

| action        | meaning                                                   | examples                                                                  |
|---------------|-----------------------------------------------------------|---------------------------------------------------------------------------|
| `delete`      | removes or irreversibly destroys file/dir/git content     | `rm`, `rmdir`, `unlink`, `shred`, `find -delete`, `git clean`, `git checkout -- f`, `git reset --hard`, `git branch -D`, `git stash drop`, `mv f /dev/null`, `truncate -s0`, `: > f`, `shutil.rmtree`, `os.remove`, `fs.rmSync` |
| `edit`        | creates or modifies file content                          | Edit/Write/MultiEdit/NotebookEdit tools, `sed -i`, `> f`, `>> f`, `tee f`, `cp src f`, `mv src f`, `dd of=f`, `touch f`, `patch`, `git apply` |
| `push`        | publishes git history to a remote                         | `git push`                                                                |
| `force_push`  | rewrites remote history                                   | `git push --force`, `-f`, `--force-with-lease`, `+branch` refspec         |
| `commit`      | records git history locally                               | `git commit`, `git commit --amend`                                        |
| `network`     | outbound network I/O                                      | `curl`, `wget`, `pip install`, `npm install`, `git fetch/pull/clone`, `ssh`, `scp`, `nc` |
| `install`     | installs packages/software                                | `pip install`, `npm i`, `apt-get install`, `brew install`, `cargo add`    |
| `exec_opaque` | runs code the guard cannot inspect                        | `eval`, `bash -c "$VAR"`, `base64 -d \| sh`, `curl ... \| sh`, `python script.py` (unknown contents), `xargs` with non-literal input, `sudo`, `env`/`command`/`nohup` wrappers around unknown |
| `run`         | any other command execution (tests, builds, reads)        | `pytest`, `ls`, `cat`, `grep`, `make`                                     |
| `test`        | a test runner invocation (subset of `run`, tracked for state) | `pytest`, `npm test`, `go test`, `cargo test`, `make test`, `jest`, `vitest`, `python -m pytest`, `unittest` |
| `spawn`       | spawns a subagent                                         | Agent / Task tool                                                         |

A single Bash command may yield many pairs (`rm a && git push -f` → `delete a`,
`force_push origin`).

## Targets

Targets are paths normalized relative to the project root (`tests/test_x.py`),
git refs (`origin/main`), hosts (`pypi.org`), or `*` when unknown/global.
Rules match targets with globs (`tests/**`, `*.env`, `prod*`).

## Rule kinds (what a constraint compiles to)

| kind        | semantics                                                            | example constraint                                         |
|-------------|----------------------------------------------------------------------|------------------------------------------------------------|
| `forbid`    | deny `(action, target)` always                                        | "never delete files", "don't touch anything under prod/"   |
| `confirm`   | deny until the user grants a one-shot confirmation for that action    | "ask me before deleting anything", "confirm before pushing"|
| `require`   | deny until a tracked event has happened and is still fresh            | "only push after tests pass", "commit only after lint"     |
| `freeze`    | deny edits to a scope once a trigger has happened                     | "don't modify tests/ once you start implementing"          |

Tracked events (state flags): `tests_pass` (set by PostToolUse on a `test`
action exiting 0; invalidated by any later `edit`), `user_confirm:<action>`
(one-shot ticket granted by UserPromptSubmit), `task_started` (first tool use
after a task prompt), `lint_pass`.

## Decision outcomes

`allow`, `deny` (with reason fed back to the model), `ask` (interactive only).
Opaque execution (`exec_opaque`) is denied only when at least one rule governs
`delete`, `force_push` or has a `*` target — this is the over-blocking knob and
is measured, not assumed.
