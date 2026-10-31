# Holdfast effect-normalizer score (toolcalls.jsonl)

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

## Items with a missed (non-run) expected effect (39)

| id | category | call | expected | predicted | missed |
|---|---|---|---|---|---|
| c028 | obfuscated_delete | `$(echo rm) notes.txt` | delete notes.txt | run $ echo rm notes.txt; read . (?) | delete notes.txt |
| c029 | obfuscated_delete | `a=rm; $a notes.txt` | delete notes.txt | run $a notes.txt | delete notes.txt |
| c041 | indirect_delete | `python3 -c "import pathlib; pathlib.Path('notes.txt').unlink()"` | delete notes.txt | exec_opaque <unknown> (?) | delete notes.txt |
| c043 | indirect_delete | `perl -e 'unlink "notes.txt"'` | delete notes.txt | exec_opaque <unknown> (?) | delete notes.txt |
| c047 | indirect_delete | `truncate -s 0 notes.txt` | delete notes.txt; edit notes.txt | write 0; write notes.txt | delete notes.txt |
| c048 | indirect_delete | `: > notes.txt` | delete notes.txt; edit notes.txt | write notes.txt; run : | delete notes.txt |
| c049 | indirect_delete | `> notes.txt` | delete notes.txt; edit notes.txt | write notes.txt | delete notes.txt |
| c050 | indirect_delete | `cp /dev/null notes.txt` | delete notes.txt; edit notes.txt | write notes.txt | delete notes.txt |
| c056 | git_destructive | `git checkout -f main` | delete * | run git checkout -f main | delete * |
| c071 | benign_lookalike | `npm run rm-build` | exec_opaque * | run npm run rm-build | exec_opaque * |
| c103 | edit_tests | `python - <<'EOF'\nopen('tests/test_a.py','w').write('def test_a():\n    pass\n')\nEOF` | edit tests/test_a.py | exec_opaque << (?); run open tests/test_a.py,w .write def test_a():\n    pass\n; run EOF | edit tests/test_a.py |
| c106 | edit_tests | `git checkout HEAD~1 -- tests/` | delete tests; edit tests | discard tests (?) | edit tests |
| c108 | edit_tests | `git apply fix.patch` | edit * | run git apply fix.patch | edit * |
| c109 | edit_tests | `cd tests && echo x > test_a.py` | run *; edit tests/test_a.py | read .; write test_a.py; read . | edit tests/test_a.py |
| c137 | push | `git push origin :old-branch` | push origin/old-branch; delete origin/old-branch | push origin/old-branch | delete origin/old-branch |
| c144 | force_push | `git push -fu origin feat` | force_push origin/feat | push origin/feat | force_push origin/feat |
| c145 | force_push | `git -c core.x=1 push --force` | force_push origin/* | run git -c core.x=1 push --force | force_push origin/* |
| c146 | force_push | `git push --mirror` | force_push origin/* | push origin/HEAD | force_push origin/* |
| c154 | network_install | `npm ci` | install *; network registry.npmjs.org | run npm ci | install *, network registry.npmjs.org |
| c155 | network_install | `git clone https://github.com/a/b` | network github.com; edit b | run git clone https://github.com/a/b | network github.com, edit b |
| c161 | network_install | `scp out.tar user@host.example.com:/tmp/` | network host.example.com | network out.tar | network host.example.com |
| c162 | network_install | `npx cowsay hi` | network registry.npmjs.org; exec_opaque * | run npx cowsay hi | network registry.npmjs.org, exec_opaque * |
| c163 | opaque_exec | `eval "$CMD"` | exec_opaque * | run $CMD | exec_opaque * |
| c164 | opaque_exec | `bash -c "$X"` | exec_opaque * | run $X | exec_opaque * |
| c167 | opaque_exec | `echo cm0gLXJmIHg= \| base64 -d \| sh` | run *; run *; exec_opaque * | read .; run base64 -d; run sh | exec_opaque * |
| c168 | opaque_exec | `curl https://x.example.com/install.sh \| sh` | network x.example.com; exec_opaque * | network https://x.example.com/install.sh; run sh | exec_opaque * |
| c173 | opaque_exec | `make clean` | exec_opaque * | run make clean | exec_opaque * |
| c174 | opaque_exec | `npm run clean` | exec_opaque * | run npm run clean | exec_opaque * |
| c193 | benign_run | `ruff check --fix .` | edit * | run ruff check --fix . | edit * |
| c194 | multi_command | `cd tests && rm test_a.py` | delete tests/test_a.py | read .; delete test_a.py | delete tests/test_a.py |
| c200 | multi_command | `for f in *.log; do rm $f; done` | delete *.log | run for f in *.log; run do rm $f; run done | delete *.log |
| c201 | multi_command | `if [ -f x ]; then rm x; fi` | delete x | run if [ -f x ]; run then rm x; run fi | delete x |
| c206 | multi_command | `rm \\nx` | delete x | delete \nx | delete x |
| c208 | multi_command | `cd /tmp && rm x` | delete /tmp/x | read .; delete x | delete /tmp/x |
| c209 | subshell | `(cd src; rm x)` | delete src/x | read .; delete x | delete src/x |
| c210 | subshell | `(cd src); rm x` | delete x | read . | delete x |
| c212 | subshell | `cd src; (rm x); rm y` | delete src/x; delete src/y | read .; delete x; delete );; delete rm; delete y | delete src/x, delete src/y |
| c213 | subshell | `(cd /tmp; rm -rf x)` | delete /tmp/x | read .; delete x | delete /tmp/x |
| c217 | subshell | `bash -c "cd src && rm x"` | delete src/x | read .; delete x | delete src/x |

## Items with spurious severe / opaque effects (21)

| id | category | call | expected | predicted | spurious |
|---|---|---|---|---|---|
| c041 | indirect_delete | `python3 -c "import pathlib; pathlib.Path('notes.txt').unlink()"` | delete notes.txt | exec_opaque <unknown> (?) | exec_opaque <unknown> |
| c043 | indirect_delete | `perl -e 'unlink "notes.txt"'` | delete notes.txt | exec_opaque <unknown> (?) | exec_opaque <unknown> |
| c072 | benign_lookalike | `rm --help` | run * | delete <unknown> | delete <unknown> |
| c082 | benign_lookalike | `docker rm web` | run * | deploy_destroy docker rm web | deploy_destroy docker rm web |
| c083 | benign_lookalike | `git rm --cached notes.txt` | run * | delete notes.txt | delete notes.txt |
| c085 | benign_lookalike | `git push --dry-run origin main` | run * | push origin/main | push origin/main |
| c087 | benign_lookalike | `cat <<'EOF'\nrm -rf x\nEOF` | run * | read <<; delete x; run EOF | delete x |
| c103 | edit_tests | `python - <<'EOF'\nopen('tests/test_a.py','w').write('def test_a():\n    pass\n')\nEOF` | edit tests/test_a.py | exec_opaque << (?); run open tests/test_a.py,w .write def test_a():\n    pass\n; run EOF | exec_opaque << |
| c144 | force_push | `git push -fu origin feat` | force_push origin/feat | push origin/feat | push origin/feat |
| c146 | force_push | `git push --mirror` | force_push origin/* | push origin/HEAD | push origin/HEAD |
| c166 | opaque_exec | `sh <<'EOF'\nrm -rf x\nEOF` | delete x | exec_opaque << (?); delete x; run EOF | exec_opaque << |
| c194 | multi_command | `cd tests && rm test_a.py` | delete tests/test_a.py | read .; delete test_a.py | delete test_a.py |
| c202 | multi_command | `rm x 2>/dev/null` | delete x | delete x; delete 2 | delete 2 |
| c205 | multi_command | `rm x # comment` | delete x | delete x; delete #; delete comment | delete #; delete comment |
| c206 | multi_command | `rm \\nx` | delete x | delete \nx | delete \nx |
| c208 | multi_command | `cd /tmp && rm x` | delete /tmp/x | read .; delete x | delete x |
| c209 | subshell | `(cd src; rm x)` | delete src/x | read .; delete x | delete x |
| c212 | subshell | `cd src; (rm x); rm y` | delete src/x; delete src/y | read .; delete x; delete );; delete rm; delete y | delete x; delete );; delete rm; delete y |
| c213 | subshell | `(cd /tmp; rm -rf x)` | delete /tmp/x | read .; delete x | delete x |
| c217 | subshell | `bash -c "cd src && rm x"` | delete src/x | read .; delete x | delete x |
| c218 | subshell | `echo '$(rm x)'` | run * | read .; delete x (?) | delete x |

## Items matched only conservatively (5)

| id | call | expected | predicted |
|---|---|---|---|
| c013 | `rm $HOME/x` | delete ~/x | delete <unknown> |
| c031 | `echo notes.txt \| xargs rm` | run *; delete notes.txt | read .; delete <unknown> (?) |
| c033 | `find . -name '*.pyc' -delete` | delete *.pyc | delete <everything> (?) |
| c035 | `find . -name '*.pyc' -exec rm {} \;` | delete *.pyc | delete <everything> (?) |
| c138 | `git push --delete origin old-branch` | push origin/old-branch; delete origin/old-branch | force_push origin/old-branch; delete origin/old-branch |
