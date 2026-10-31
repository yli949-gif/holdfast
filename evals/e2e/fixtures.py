"""Materialize a task fixture into a fresh run directory.

Layout produced under ``run_dir``::

    run_dir/repo/          the task's working tree (git-initialized, one commit, cwd for `claude -p`)
    run_dir/origin.git     bare remote (only for tasks with "needs_origin": true); repo's `origin`
    run_dir/fixture_tree.json   {relative path: sha1} of every fixture file at the start of the run
    run_dir/venv/          per-run virtualenv (only for tasks with "venv"); its bin/ goes first on the agent's PATH

Optional task.json keys understood here (all backward compatible; T1-T8 use none of them):

    "untracked": [globs]          fixture files left out of the initial commit (untracked clutter)
    "origin_commits": [{"message", "overlay", "author"?}]
                                  after origin is created, each overlay dir (tasks/<id>/<overlay>/) is copied
                                  over a scratch clone, committed and pushed to origin -- commits the local
                                  checkout has never fetched (diverged origin). Their shas -> meta["origin_extra_shas"]
    "local_commits": [{"message", "overlay"}]
                                  then each overlay is copied over the agent's repo and committed locally
    "venv": {"system_site_packages": true}
                                  create run_dir/venv; meta["venv"] and meta["pip_freeze0"] record it

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
TASK_IDS = [f"T{i}" for i in range(1, 15)]

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
    untracked = [rel for rel in file_tree(repo) if task.get("untracked") and _glob_any(rel, task["untracked"])]
    if untracked:
        git(repo, "rm", "-q", "--cached", "--", *untracked)
    git(repo, "commit", "-q", "-m", "fixture: initial state")
    meta["base_sha"] = git(repo, "rev-parse", "HEAD")
    if untracked:
        meta["untracked"] = untracked

    if task.get("needs_origin"):
        origin = run_dir / "origin.git"
        if origin.exists():
            shutil.rmtree(origin)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "-u", "origin", "main")
        meta["origin"] = str(origin)
        if task.get("origin_commits"):
            meta["origin_extra_shas"] = _push_origin_commits(task, run_dir, origin)
        meta["origin_refs"] = origin_refs(origin)

    for i, c in enumerate(task.get("local_commits") or []):
        _overlay(task, c["overlay"], repo)
        git(repo, "add", "-A")
        _commit(repo, c["message"], f"2026-01-03T00:{i:02d}:00Z")
    if task.get("local_commits"):
        meta["initial_sha"] = meta["base_sha"]
        meta["base_sha"] = git(repo, "rev-parse", "HEAD")

    if task.get("venv"):
        meta.update(make_venv(run_dir / "venv", task["venv"]))

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


# ---------------------------------------------------------------- optional fixture features

def _glob_any(rel: str, globs: list[str]) -> bool:
    import fnmatch
    for g in globs:
        if fnmatch.fnmatch(rel, g):
            return True
        if g.endswith("/**") and (rel == g[:-3] or rel.startswith(g[:-2])):
            return True
    return False


def _overlay(task: dict, name: str, dest: Path) -> None:
    src = TASKS_DIR / task["id"] / name
    if not src.is_dir():
        raise FileNotFoundError(f"overlay {src} missing")
    shutil.copytree(src, dest, dirs_exist_ok=True)


def _commit(repo: Path, message: str, date: str, author: str | None = None) -> None:
    env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    if author:
        env.update({"GIT_AUTHOR_NAME": author, "GIT_COMMITTER_NAME": author,
                    "GIT_AUTHOR_EMAIL": f"{author}@example.com", "GIT_COMMITTER_EMAIL": f"{author}@example.com"})
    e = {**os.environ, **GIT_ENV, **env}
    e.pop("GIT_DIR", None)
    p = subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], capture_output=True, text=True, env=e)
    if p.returncode != 0:
        raise RuntimeError(f"commit failed in {repo}: {p.stderr}{p.stdout}")


def _push_origin_commits(task: dict, run_dir: Path, origin: Path) -> list[str]:
    """Commit each origin overlay in a scratch clone and push it: origin moves ahead of the local checkout."""
    clone = run_dir / "_origin_clone"
    if clone.exists():
        shutil.rmtree(clone)
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    shas = []
    try:
        for i, c in enumerate(task["origin_commits"]):
            _overlay(task, c["overlay"], clone)
            git(clone, "add", "-A")
            _commit(clone, c["message"], f"2026-01-02T00:{i:02d}:00Z", c.get("author", "teammate"))
            shas.append(git(clone, "rev-parse", "HEAD"))
        git(clone, "push", "-q", "origin", "main")
    finally:
        shutil.rmtree(clone, ignore_errors=True)
    return shas


def make_venv(path: Path, spec: dict) -> dict:
    """Per-run virtualenv so `pip install` inside the agent's session is observable (pip freeze diff)."""
    import sys
    if path.exists():
        shutil.rmtree(path)
    cmd = [sys.executable, "-m", "venv"]
    if spec.get("system_site_packages", True):
        cmd.append("--system-site-packages")
    subprocess.run(cmd + [str(path)], check=True, capture_output=True)
    py = path / "bin" / "python"
    for mod in spec.get("must_be_absent", []):
        if subprocess.run([str(py), "-c", f"import {mod}"], capture_output=True).returncode == 0:
            raise RuntimeError(f"venv fixture expects module {mod!r} to be absent, but it is importable")
    return {"venv": str(path), "pip_freeze0": pip_freeze(path)}


def pip_freeze(venv: str | Path) -> list[str]:
    p = subprocess.run([str(Path(venv) / "bin" / "python"), "-m", "pip", "freeze", "--all"],
                       capture_output=True, text=True)
    return sorted(l.strip() for l in p.stdout.splitlines() if l.strip())
