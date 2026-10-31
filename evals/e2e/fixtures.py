"""Materialize a task fixture into a fresh run directory.

Layout produced under ``run_dir``::

    run_dir/repo/          the task's working tree (git-initialized, one commit, cwd for `claude -p`)
    run_dir/origin.git     bare remote (only for tasks with "needs_origin": true); repo's `origin`
    run_dir/fixture_tree.json   {relative path: sha1} of every fixture file at the start of the run

Fixtures are deterministic (fixed git author/date, seeded generators) so trials
start from byte-identical repositories.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS_DIR = HERE / "tasks"
TASK_IDS = [f"T{i}" for i in range(1, 9)]

# directories/files that belong to the harness, not to the fixture
HARNESS_TOP = {".git", ".claude", ".holdfast", "CLAUDE.md"}
NOISE_PARTS = {"__pycache__", ".pytest_cache"}

GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def load_task(tid: str) -> dict:
    return json.loads((TASKS_DIR / tid / "task.json").read_text())


def git(repo: str | Path, *args: str, check: bool = True) -> str:
    env = {**os.environ, **GIT_ENV}
    env.pop("GIT_DIR", None)
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}:\n{p.stderr}")
    return p.stdout.strip()


def file_tree(root: str | Path) -> list[str]:
    """Sorted relative paths of every fixture file under root (harness + noise dirs excluded)."""
    root = Path(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        parts = rel.parts
        if parts and (parts[0] in HARNESS_TOP or any(p in NOISE_PARTS for p in parts)):
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if not ((not parts and d in HARNESS_TOP) or d in NOISE_PARTS))
        for f in filenames:
            r = str(rel / f) if parts else f
            if r in HARNESS_TOP or r.endswith(".pyc"):
                continue
            out.append(r)
    return sorted(out)


def tree_hashes(root: str | Path) -> dict[str, str]:
    root = Path(root)
    return {rel: hashlib.sha1((root / rel).read_bytes()).hexdigest() for rel in file_tree(root)}


# ---------------------------------------------------------------- large file (compaction bait)

COMPONENTS = ["scheduler", "ingest worker", "report renderer", "auth middleware", "cache layer", "metrics exporter",
              "config loader", "CLI", "migration runner", "retry policy", "rate limiter", "webhook dispatcher",
              "catalog loader", "search index", "billing sync", "notification fan-out"]
VERBS = ["Fixed", "Refactored", "Hardened", "Documented", "Optimised", "Deprecated", "Re-enabled", "Tuned",
         "Rewrote", "Instrumented", "Removed", "Added"]
ISSUES = ["a race on shutdown", "duplicate log lines", "an off-by-one in pagination", "timezone drift in timestamps",
          "stale cache entries after restart", "unbounded memory growth", "a flaky integration test",
          "misleading error messages", "missing retries on 503", "config precedence surprises",
          "a path traversal in the export endpoint", "an unescaped query in the admin filter",
          "silent truncation of long names", "a deadlock under back-pressure", "incorrect rounding of totals"]
TAGS = ["", "", "", "", "[security]", "[breaking]", "[perf]", "[docs]", "[internal]"]


def generate_changelog(path: Path, approx_tokens: int = 45000, seed: int = 8) -> dict:
    """Write a plausible, boring CHANGELOG of roughly ``approx_tokens`` tokens (~4 chars/token).

    Returns the per-major-version tag counts so a grader could check a summary if desired.
    """
    rnd = random.Random(seed)
    target_chars = approx_tokens * 4
    lines = ["# Changelog", "", "All notable changes to this project. Entries are tagged [security], [breaking], [perf], [docs] or [internal].", ""]
    counts: dict[str, dict[str, int]] = {}
    chars = sum(len(l) + 1 for l in lines)
    major, minor, patch = 3, 9, 7
    while chars < target_chars:
        ver = f"{major}.{minor}.{patch}"
        lines.append(f"## {ver} ({rnd.randint(2019, 2026)}-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d})")
        lines.append("")
        chars += len(lines[-2]) + 2
        for _ in range(rnd.randint(4, 9)):
            tag = rnd.choice(TAGS)
            c = counts.setdefault(f"{major}.x", {"[security]": 0, "[breaking]": 0})
            if tag in c:
                c[tag] += 1
            ref = f"#{rnd.randint(100, 9999)}"
            line = (f"- {tag + ' ' if tag else ''}{rnd.choice(VERBS)} {rnd.choice(ISSUES)} in the {rnd.choice(COMPONENTS)}; "
                    f"affects the {rnd.choice(COMPONENTS)} when {rnd.choice(['running under load', 'started cold', 'configured with multiple workers', 'used from the CLI', 'behind the proxy'])} ({ref}, thanks @{rnd.choice(['mira', 'tomasz', 'ling', 'devon', 'ayo', 'kat'])}).")
            lines.append(line)
            chars += len(line) + 1
        lines.append("")
        chars += 1
        patch -= 1
        if patch < 0:
            patch, minor = rnd.randint(2, 7), minor - 1
            if minor < 0:
                minor, major = 9, major - 1
                if major < 1:
                    break
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"chars": chars, "lines": len(lines), "counts": counts}


# ---------------------------------------------------------------- materialize

def materialize(task: dict, run_dir: Path, force_large_file: bool = False) -> dict:
    """Copy the fixture, generate bait files, git-init, create origin. Returns run metadata."""
    run_dir.mkdir(parents=True, exist_ok=True)
    repo = run_dir / "repo"
    if repo.exists():
        shutil.rmtree(repo)
    shutil.copytree(TASKS_DIR / task["id"] / "fixture", repo, symlinks=False)
    for p in list(repo.rglob("__pycache__")):
        shutil.rmtree(p, ignore_errors=True)

    meta: dict = {"repo": str(repo), "origin": None}
    gen = task.get("generate")
    if gen is None and force_large_file:
        gen = {"path": "CHANGELOG.md", "approx_tokens": 45000, "seed": 8}
    if gen:
        meta["generated"] = {"path": gen["path"], **generate_changelog(repo / gen["path"], gen.get("approx_tokens", 45000), gen.get("seed", 8))}

    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "fixture")
    git(repo, "config", "user.email", "fixture@example.com")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture: initial state")
    meta["base_sha"] = git(repo, "rev-parse", "HEAD")

    if task.get("needs_origin"):
        origin = run_dir / "origin.git"
        if origin.exists():
            shutil.rmtree(origin)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "-u", "origin", "main")
        meta["origin"] = str(origin)
        meta["origin_refs"] = origin_refs(origin)

    tree = tree_hashes(repo)
    (run_dir / "fixture_tree.json").write_text(json.dumps(tree, indent=1))
    meta["fixture_files"] = len(tree)
    return meta


def origin_refs(origin: str | Path) -> dict[str, str]:
    out = git(origin, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
    refs = {}
    for line in out.splitlines():
        name, sha = line.split()
        refs[name] = sha
    return refs
