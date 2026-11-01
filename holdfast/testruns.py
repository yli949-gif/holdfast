"""Which shell commands count as "the test suite passed" for `require_before tests_pass` rules.

REVIEW-2 N4/N10/T1: any command containing a test-runner effect used to count, so a single file, `-k`, a
`pytest --version; cat notes.txt` or `pytest | sed s/failed/passed/` satisfied "only push after tests pass".
Now only a *single* runner invocation counts, optionally behind `cd <dir> &&`, env assignments or `timeout N`,
with `2>&1` and at most one trailing `| tail|head|cat`. Runner arguments that select a subset make it `partial`.
`npm test` / `make test` count only when the script or recipe they run is itself a known runner.
"""
from __future__ import annotations

import json
import os
import re
import shlex

FULL, PARTIAL = "full", "partial"

_PIPE_TAIL = re.compile(r"^\s*(?:tail|head|cat)(?:\s+(?:-n\s*\d+|-\d+|-n\d+|-c\s*\d+|-q))*\s*$")
_LEAD_CD = re.compile(r"^\s*cd\s+([^\s;&|<>`$()]+)\s*&&\s*")
_RUNNER_TOKEN = re.compile(r"(?:^|[\s/])(?:pytest|py\.test|jest|vitest|mocha|ava|tap|karma|playwright test|cypress run|"
                           r"node --test|go test|cargo test|rspec|phpunit|tox|nox|react-scripts test|ng test|unittest)\b")
_PROBE = {"--version", "-V", "--help", "-h", "--co", "--collect-only", "--fixtures", "--markers", "--setup-plan",
          "--listTests", "--list", "--showConfig", "--dry-run"}
# pytest options that take a value (so the value is not a positional test selection)
_PYTEST_VAL = {"-c", "--rootdir", "-p", "-n", "--numprocesses", "--maxfail", "-W", "--tb", "-o", "--override-ini",
               "--durations", "--color", "--junitxml", "--junit-xml", "--cov", "--cov-report", "--log-level", "-r",
               "--basetemp", "--confcutdir", "--import-mode", "--capture", "--dist", "--timeout", "--reruns", "--count"}
_PYTEST_PARTIAL = {"-k", "-m", "--deselect", "--lf", "--last-failed", "--sw", "--stepwise", "--sw-skip", "--ignore",
                   "--ignore-glob", "--only-rerun", "--nf"}
_JS_PARTIAL = {"-t", "--testNamePattern", "--grep", "-g", "--testPathPattern", "--findRelatedTests", "--onlyChanged", "-o",
               "--changedSince", "--onlyFailures", "-f", "--related", "--project", "--shard"}


def _strip(cmd: str) -> tuple[str, str | None] | None:
    """-> (single runner command, cd dir) or None when the command is anything more than one runner invocation."""
    s = cmd.strip()
    if "\n" in s or "`" in s or "$(" in s:
        return None
    s = re.sub(r"\s+2>&1\b", " ", s)
    parts = s.split("|")
    if len(parts) > 2 or "||" in s:
        return None
    if len(parts) == 2 and not _PIPE_TAIL.match(parts[1]):
        return None
    s = parts[0]
    cd = None
    m = _LEAD_CD.match(s)
    if m:
        cd, s = m.group(1), s[m.end():]
    if re.search(r"[;&<>()]", s):
        return None
    return s.strip(), cd


def _argv(s: str) -> list[str] | None:
    try:
        argv = shlex.split(s)
    except ValueError:
        return None
    while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        argv = argv[1:]
    if argv[:1] == ["timeout"] and len(argv) > 2:
        argv = argv[2:]
    if argv[:2] in (["uv", "run"], ["poetry", "run"], ["pipenv", "run"], ["hatch", "run"], ["pdm", "run"]):
        argv = argv[2:]
    if argv[:1] == ["npx"] and len(argv) > 1:
        argv = argv[1:]
    return argv


def _pytest_kind(args: list[str], base: str) -> str:
    i = 0
    while i < len(args):
        a = args[i]
        if a in _PROBE:
            return ""
        if a in _PYTEST_PARTIAL or any(a.startswith(f + "=") for f in _PYTEST_PARTIAL if f.startswith("--")) or \
                (a.startswith("-k") and len(a) > 2) or (a.startswith("-m") and len(a) > 2 and not a.startswith("-m=")):
            return PARTIAL
        if a in _PYTEST_VAL:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        # positional: only the whole tree or a top-level tests directory counts as the suite
        p = a.rstrip("/")
        if "::" in p or p not in (".", "tests", "test", "./tests", "./test", base.rstrip("/")):
            return PARTIAL
        i += 1
    return FULL


def _js_kind(args: list[str]) -> str:
    for a in args:
        if a in _PROBE:
            return ""
        if a in _JS_PARTIAL or any(a.startswith(f + "=") for f in _JS_PARTIAL if f.startswith("--")):
            return PARTIAL
        if not a.startswith("-") and a not in ("run", "test", "--"):
            return PARTIAL
    return FULL


def _script_body(kind: str, name: str, root: str) -> str | None:
    try:
        if kind == "npm":
            data = json.load(open(os.path.join(root, "package.json")))
            return (data.get("scripts") or {}).get(name)
        text = open(os.path.join(root, "Makefile")).read().replace("\\\n", " ")
    except (OSError, ValueError):
        return None
    m = re.search(rf"^{re.escape(name)}\s*:[^=\n]*\n((?:\t.*\n?)+)", text, re.M)
    return m.group(1) if m else None


def test_run_kind(cmd: str, cwd: str | None = None, root: str | None = None) -> str:
    """'full' | 'partial' | '' (not an acceptable test-suite run)."""
    st = _strip(cmd)
    if st is None:
        return ""
    s, cd = st
    argv = _argv(s)
    if not argv:
        return ""
    base = os.path.join(cwd or root or ".", cd) if cd else (cwd or root or ".")
    proj = root or base
    head = os.path.basename(argv[0])
    args = argv[1:]
    if re.match(r"^python[\d.]*$|^pypy3?$", head):
        if args[:2] == ["-m", "pytest"] or args[:2] == ["-m", "py.test"]:
            return _pytest_kind(args[2:], "")
        if args[:2] == ["-m", "unittest"]:
            rest = [a for a in args[2:] if not a.startswith("-")]
            return FULL if not rest or rest[:1] == ["discover"] else PARTIAL
        if args[:2] in (["-m", "tox"], ["-m", "nox"]):
            return FULL
        return ""
    if head in ("pytest", "py.test"):
        return _pytest_kind(args, "")
    if head in ("jest", "vitest", "mocha", "ava", "karma"):
        return _js_kind(args)
    if head == "go" and args[:1] == ["test"]:
        rest = args[1:]
        if "-run" in rest or any(a.startswith("-run=") for a in rest):
            return PARTIAL
        pkgs = [a for a in rest if not a.startswith("-")]
        return FULL if pkgs == ["./..."] else PARTIAL
    if head == "cargo" and args[:1] == ["test"]:
        rest = args[1:]
        if "--" in rest:
            rest = rest[:rest.index("--")]
        return PARTIAL if any(not a.startswith("-") for a in rest) else FULL
    if head in ("tox", "nox", "rspec", "phpunit"):
        return FULL if not any(not a.startswith("-") for a in args) or head in ("tox", "nox") else PARTIAL
    if head in ("npm", "yarn", "pnpm", "bun"):
        rest = args[1:] if args[:1] == ["run"] else args
        if rest[:1] != ["test"]:
            return ""
        extra = rest[1:]
        if "--" in extra:
            extra = extra[extra.index("--") + 1:]
        body = _script_body("npm", "test", proj)
        if not body or not _RUNNER_TOKEN.search(" " + body):
            return ""                       # `"test": "echo ok"` is not a test run (REVIEW-2 N10)
        return _js_kind(extra) if extra else FULL
    if head == "make" and args and args[0] in ("test", "tests", "check"):
        body = _script_body("make", args[0], proj)
        if not body or not _RUNNER_TOKEN.search(" " + body):
            return ""
        return FULL if len(args) == 1 else PARTIAL
    return ""


def tree_changing_git(cmd: str) -> bool:
    """git subcommands that rewrite the working tree: a green run before them says nothing about the tree after."""
    return bool(re.search(r"\bgit\b(?:\s+-[cC]\s+\S+)*\s+(?:pull|merge|rebase|cherry-pick|revert|am|apply|reset|restore|"
                          r"checkout|switch|read-tree|checkout-index|bisect|stash\s+(?:pop|apply)|worktree\s+add|submodule\s+update)\b",
                          cmd))
