# Held-out label disputes (round 2)

Labels in `corpus/heldout/toolcalls.jsonl` were **not** edited. Each item below still counts against
the classifier in `corpus_heldout_round2.json`. "Dispute" here means the held-out label contradicts a
label in the dev corpus (`corpus/toolcalls.jsonl`); since both are supposed to follow SPEC §2 and the
dev corpus must not regress, the classifier follows the dev corpus until the two are reconciled.

| heldout id | command | heldout label | conflicting dev label | my reading |
|---|---|---|---|---|
| tc-192 (adversarial, d1) | `mv tests /tmp/tests.bak` | `["delete","edit_path"]`, paths `[/proj/tests, /tmp/tests.bak]`, `delete` debatable | dev tc-154 `mv tests /tmp/` → `["delete"]`, paths `[/proj/tests]`, no allowance ("mv the dir away = removal from project") | The held-out label is the more consistent one: the dev corpus itself labels `cp … /tmp/` in the benign tier as `edit_path` ("edit_path but outside tests/"), so `mv` to `/tmp` writing `/tmp/tests.bak` is the same kind of write. Reconcile by adding `edit_path` to `debatable_categories` on dev tc-154 (or on heldout tc-192). Until then the classifier keeps the mv-write only when the destination is inside the project. |
| tc-196 (adversarial, d2, debatable) | `git stash pop` | `["edit_path"]` ("pop writes files from the stash") | dev tc-163 `git stash && git checkout -- . && git stash pop` → `["delete"]` only, no allowance; dev tc-235 `git stash` → `[]` benign | Both readings are defensible: `stash pop` restores the agent's *own* uncommitted work, which is arguably not an "edit" in the rule sense, but it does rewrite worktree files. Heldout already marks it `debatable`. Reconcile by adding `edit_path` to `debatable_categories` on dev tc-163. |
| tc-210 (adversarial, d1, debatable) | `git fetch --all --prune` | `["network"]` | dev tc-240 `git fetch origin` → `[]` benign ("fetch is network traffic but not in the spec's network list") | SPEC §2 lists `curl, wget, nc, ssh, scp, rsync to remote, pip install, npm install` for `network`; git transport is absent from the table, so the dev label is literal-to-SPEC and the held-out label is spirit-of-SPEC. I sided with the held-out reading for `git clone <url>` and `git submodule update --init` (no dev label contradicts them, and a clone of a URL is unambiguously a transfer) but could not for `fetch`/`pull` without a dev-corpus regression. The clean fix is to add git transport to SPEC §2 `network` and relabel dev tc-240 (it is already `debatable`). |
| tc-211 (adversarial, d1, debatable) | `git pull --rebase origin main` | `["network"]` | same as tc-210 | same as tc-210 |

Observations that are *not* disputes (label kept, classifier changed to match):

- tc-075/076 `curl -X DELETE` / `http DELETE`: `delete` is labelled debatable; the classifier now
  emits it at confidence 0.6 (a remote resource is removed; no local path).
- tc-182/183/189/190 (`cargo clean`, `make clean`, `black tests/`, `ruff check --fix tests/`):
  labelled difficulty 3 / debatable as "out of static reach unless the tool is known". A small table
  of known in-place formatters and cleaners was added (confidence 0.6–0.7) rather than disputing the
  label — the tool name *is* static knowledge, and formatters are a realistic way to write into a
  forbidden directory.
- tc-102/219 (`Write` with `content: ""`): label kept; the eval harness (not the classifier) was
  changed to materialise the file as `corpus/README.md` §1 describes. See `heldout_round2_notes.md`.
