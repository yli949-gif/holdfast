"""Red-team harness for Holdfast.

Builds a throwaway, self-contained git project (its own ``.git`` and
``.holdfast/`` so Holdfast's project discovery stops inside it) plus a bare
remote, primes state via the real hooks, drives the ``PreToolUse`` hook through
``holdfast/hook.py`` (the v2 entrypoint), and (optionally) *executes* the tool
call for real to confirm whether a forbidden effect actually happens.

Nothing here touches the Holdfast source tree; every project lives under a
fresh ``tempfile.mkdtemp()`` and is self-contained.
"""
from __future__ import annotations
import json, os, shutil, subprocess, tempfile

import sys
HOOK_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "holdfast", "hook.py")
PY = sys.executable


def _run(args, cwd=None, inp=None, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith("HOLDFAST_") and k != "CLAUDE_PROJECT_DIR"}
    if env:
        e.update(env)
    return subprocess.run(args, cwd=cwd, input=inp, capture_output=True, text=True, env=e, timeout=90)


def make_project():
    """Fresh git repo + bare remote with a realistic file tree. Returns (path, remote)."""
    p = tempfile.mkdtemp(prefix="hf_rt_")
    for d in ("tests", "src", "build", "config", "k8s/production", "node_modules/x", "dist"):
        os.makedirs(os.path.join(p, d), exist_ok=True)
    files = {
        "tests/test_a.py": "def test_a():\n    assert True\n",
        "tests/README.md": "# tests\n",
        "src/a.py": "VALUE = 1\n",
        "src/production_utils.py": "x = 1\n",
        "config/prod.yml": "env: prod\nreplicas: 3\n",
        "config/production.yaml": "env: production\n",
        "config/dev.yml": "env: dev\n",
        ".env.production": "SECRET=prod-abc\n",
        "Makefile": "clean:\n\trm -rf build dist src/a.py\n\ntest:\n\t@echo ok\n",
        "package.json": json.dumps({"name": "x", "scripts": {"clean": "rm -rf build dist", "test": "echo ok"}}),
        "important.txt": "do not lose me\n",
        "data.txt": "payload\n",
        "conftest.py": "x = 1\n",
    }
    for rel, content in files.items():
        fp = os.path.join(p, rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w") as fh:
            fh.write(content)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=p, check=True)
    subprocess.run(["git", "add", "-A"], cwd=p, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=p, check=True)
    subprocess.run(["git", "branch", "-q", "feature"], cwd=p, check=True)
    remote = tempfile.mkdtemp(prefix="hf_rt_rem_") + "/remote.git"
    subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=p, check=True)
    subprocess.run(["git", "-c", "protocol.version=0", "push", "-q", "origin", "main", "feature"],
                   cwd=p, capture_output=True)
    return p, remote


def cleanup(*paths):
    for p in paths:
        shutil.rmtree(os.path.dirname(p) if p.endswith(".git") else p, ignore_errors=True)


def add_rule(p, text):
    return _run([PY, "-m", "holdfast.cli", "rules", "add", text], cwd=p)


def _hook(p, event, **fields):
    payload = {"session_id": "s", "cwd": p, "hook_event_name": event, "permission_mode": "default", **fields}
    r = _run([PY, HOOK_PY, event], cwd=p, inp=json.dumps(payload))
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {"_raw": r.stdout, "_err": r.stderr}


def prompt(p, text):
    return _hook(p, "UserPromptSubmit", prompt=text)


def pass_tests(p):
    return _hook(p, "PostToolUse", tool_name="Bash", tool_input={"command": "pytest -q"},
                 tool_response={"stdout": "3 passed in 0.02s", "stderr": "", "interrupted": False})


def pre(p, tool, tool_input):
    out = _hook(p, "PreToolUse", tool_name=tool, tool_input=tool_input)
    h = out.get("hookSpecificOutput", {})
    return h.get("permissionDecision", "allow" if out == {} else "?"), h.get("permissionDecisionReason", "")


def apply_edit(p, tool, ti):
    path = ti.get("file_path") or ti.get("notebook_path")
    ap = path if os.path.isabs(path) else os.path.join(p, path)
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    if tool == "Write":
        open(ap, "w").write(ti.get("content", ""))
    elif tool == "Edit":
        s = open(ap).read()
        s = s.replace(ti["old_string"], ti["new_string"], 1 if not ti.get("replace_all") else -1)
        open(ap, "w").write(s)
    elif tool == "MultiEdit":
        s = open(ap).read()
        for e in ti["edits"]:
            s = s.replace(e["old_string"], e["new_string"])
        open(ap, "w").write(s)
    elif tool == "NotebookEdit":
        open(ap, "w").write(ti.get("new_source", ""))


def run_bash(p, cmd):
    return subprocess.run(["bash", "-c", cmd], cwd=p, capture_output=True, text=True, timeout=90)


def remote_branches(remote):
    r = subprocess.run(["git", "-C", remote, "branch", "--format=%(refname:short)"],
                       capture_output=True, text=True)
    return set(x.strip() for x in r.stdout.split() if x.strip())


def do_effect(p, remote, tool, ti):
    if tool == "Bash":
        r = run_bash(p, ti["command"])
        return {"rc": r.returncode, "out": r.stdout[:200], "err": r.stderr[:200]}
    apply_edit(p, tool, ti)
    return {"rc": 0}
