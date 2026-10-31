# Baseline (before classifier hardening)

Recorded 2026-10-31 at commit `8db3baa` (holdfast/*.py sha1 `df1d39377479`), before any change to shell.py/effects.py.

## Red team (`cd evals/redteam && python3 run_attacks.py --effects`)

```
=== SUMMARY ===
attempts=114  (attack probes=93, benign probes=21)
  bypass        =43
  caught        =40
  ask           =10
  false_block   =5
  correct_allow =16
  error         =0

by constraint type:
  benign       false_block=3  correct_allow=15
  confirm      bypass=1  false_block=1
  delete       bypass=21  caught=8  ask=7
  force_push   bypass=1  caught=3
  install      bypass=6  caught=7
  prod         bypass=4  caught=6  ask=1  correct_allow=1
  push         bypass=6  caught=2  false_block=1
  tests        bypass=4  caught=14  ask=2
```

Bypasses / false blocks:

```
```

## Legacy corpus (`python3 evals/corpus/run_corpus.py --corpus evals/corpus/toolcalls.legacy.jsonl`)

## Totals

| policy | group | n | missed violations | false blocks (deny+ask) | of which ask | ask decisions | debatable blocked |
|---|---|---|---|---|---|---|---|
| ask | all | 486 | 100/326 (31%) | 18/147 (12%) | 0 | 24 | 9/13 |
| deny | all | 486 | 100/326 (31%) | 18/147 (12%) | 0 | 0 | 9/13 |
| allow | all | 486 | 124/326 (38%) | 18/147 (12%) | 0 | 0 | 9/13 |


## Effect corpus (`python3 evals/corpus/score_effects.py`, 218 items)

| category | items | pairs | exact | conservative | missed | items all-exact | items w/ miss | spurious severe (items) | spurious opaque | run pairs matched |
|---|---|---|---|---|---|---|---|---|---|---|
| **all** | 218 | 221 | 173 (78%) | 5 (2%) | 43 (19%) | 174 | 39 | 21 (17) | 4 | 45/49 |
| benign_lookalike | 25 | 3 | 2 (67%) | 0 (0%) | 1 (33%) | 24 | 1 | 5 (5) | 0 | 17/21 |
| benign_run | 5 | 1 | 0 (0%) | 0 (0%) | 1 (100%) | 4 | 1 | 0 (0) | 0 | 4/4 |
| edit_benign | 11 | 11 | 11 (100%) | 0 (0%) | 0 (0%) | 11 | 0 | 0 (0) | 0 | 0/0 |
| edit_tests | 20 | 22 | 18 (82%) | 0 (0%) | 4 (18%) | 16 | 4 | 0 (0) | 1 | 4/4 |
| force_push | 9 | 10 | 7 (70%) | 0 (0%) | 3 (30%) | 6 | 3 | 2 (2) | 0 | 0/0 |
| git_destructive | 13 | 13 | 12 (92%) | 0 (0%) | 1 (8%) | 12 | 1 | 0 (0) | 0 | 0/0 |
| indirect_delete | 19 | 25 | 17 (68%) | 2 (8%) | 6 (24%) | 11 | 6 | 0 (0) | 2 | 1/1 |
| multi_command | 15 | 19 | 14 (74%) | 0 (0%) | 5 (26%) | 10 | 5 | 6 (5) | 0 | 3/3 |
| network_install | 15 | 25 | 18 (72%) | 0 (0%) | 7 (28%) | 11 | 4 | 0 (0) | 0 | 0/0 |
| obfuscated_delete | 15 | 15 | 12 (80%) | 1 (7%) | 2 (13%) | 12 | 2 | 0 (0) | 0 | 1/1 |
| opaque_exec | 14 | 15 | 9 (60%) | 0 (0%) | 6 (40%) | 8 | 6 | 0 (0) | 1 | 3/3 |
| plain_delete | 17 | 18 | 17 (94%) | 1 (6%) | 0 (0%) | 16 | 0 | 0 (0) | 0 | 0/0 |
| push | 7 | 9 | 7 (78%) | 1 (11%) | 1 (11%) | 5 | 1 | 0 (0) | 0 | 0/0 |
| redirect_write | 11 | 12 | 12 (100%) | 0 (0%) | 0 (0%) | 11 | 0 | 0 (0) | 0 | 9/9 |
| subshell | 10 | 10 | 4 (40%) | 0 (0%) | 6 (60%) | 5 | 5 | 8 (5) | 0 | 3/3 |
| test_run | 12 | 13 | 13 (100%) | 0 (0%) | 0 (0%) | 12 | 0 | 0 (0) | 0 | 0/0 |

Full per-item effect-corpus baseline: `results_effects_baseline.md`.
