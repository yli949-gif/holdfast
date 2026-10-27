# Holdfast corpus (v0)

Implementation-independent ground truth for SPEC §5 A1 (tool-call classification) and A4
(constraint extraction + confirmation detection). Nothing in here was derived from `src/`.

Files:

| file | what | lines |
|---|---|---|
| `toolcalls.jsonl` | labelled Claude Code tool calls → expected action categories + paths | 178 |
| `constraints.jsonl` | NL sentences → expected rules (plus negatives) | 102 |
| `confirmations.jsonl` | user utterances → is this a confirmation of the pending action? | 40 |
| `validate.py` | schema / vocabulary / id-uniqueness checks; `python corpus/validate.py` must exit 0 | |
| `stats.py` | prints the counts pasted at the bottom of this file | |

## 1. `toolcalls.jsonl`

```json
{"id":"tc-001","tier":"plain","tool_name":"Bash","tool_input":{"command":"rm -rf build"},
 "cwd":"/proj","expected":{"categories":["delete"],"paths":["/proj/build"],"cmds":["rm"]},"note":"..."}
```

* `tool_input` uses the real Claude Code shapes: `Bash {command}`, `Edit {file_path, old_string, new_string, replace_all}`,
  `Write {file_path, content}`, `MultiEdit {file_path, edits:[{old_string,new_string}]}`,
  `NotebookEdit {notebook_path, new_source, cell_id, cell_type, edit_mode}`, plus a few `Read`/`Grep`/`Glob`
  calls that a hook must never classify.
* `cwd` is always `/proj`. The runner rewrites `/proj` to a temp dir (in the command string, in `file_path`s
  and in `expected.paths`) and must set `HOME` to the temp dir's parent so that `~/proj` == cwd.
* **Fixture files** the runner should pre-create (several labels depend on the file existing):
  `tests/test_a.py`, `tests/test_b.py`, `src/a.py`, `src/auth.py`, `src/old_test.py`, `src/fixture.py`,
  `config/production.yml`, `config/prod.yml`, `logs/app.log`, `README.md`, `p.diff`, `secrets.txt`,
  `notebooks/analysis.ipynb`, and directories `build/`, `dist/`, `node_modules/`, `tmp_empty/` (empty), `fixtures/`.

### Tiers

* **plain** (95): straightforward instances of every SPEC §2 category. A1 requires FN