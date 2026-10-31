"""End-to-end: drive the real `holdfast` CLI through subprocess with fake hook payloads.

Scenarios (SPEC A3, A5 and the hook contract in §4):
  (a) confirm-before-delete: deny -> confirm -> allow once -> deny again
  (b) order push-after-tests: deny -> tests pass -> allow -> edit -> deny
  (c) forbid edits under tests/ via Edit and Bash write paths
  (d) SessionStart source=compact re-anchors with the rules intact
  (e) latency: subprocess PreToolUse < 300 ms median; in-process < 50 ms (see test_hooks.py)
"""
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

HOLDFAST = shutil.which("holdfast")
pytestmark = pytest.mark.skipif(HOLDFAST is None, reason="holdfast console script not installed")


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("x\n")
    (tmp_path / "src" / "a.py").write_text("y\n")
    (tmp_path / "build").mkdir()
    return str(tmp_path)


def hook(event, cwd, **fields):
    payload = {"session_id": "sess-1", "transcript_path": "/tmp/t.jsonl", "cwd": cwd,
               "hook_event_name": event, "permission_mode": "default", **fields}
    env = {**os.environ, "HOLDFAST_EMIT_ALLOW": "1"}
    proc = subprocess.run([HOLDFAST, "hook", event], input=json.dumps(payload), capture_output=True,
                          text=True, cwd=cwd, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "hook must always print JSON"
    return json.loads(proc.stdout)


def pre(cwd, tool, inp):
    out = hook("PreToolUse", cwd, tool_name=tool, tool_input=inp)
    h = out.get("hookSpecificOutput", {})
    assert h.get("hookEventName") == "PreToolUse"
    return h["permissionDecision"], h.get("permissionDecisionReason", "")


def prompt(cwd, text):
    return hook("UserPromptSubmit", cwd, prompt=text)


def post(cwd, tool, inp, ok=True):
    if ok:
        return hook("PostToolUse", cwd, tool_name=tool, tool_input=inp,
                    tool_response={"stdout": "", "stderr": "", "interrupted": False, "isImage": False})
    return hook("PostToolUseFailure", cwd, tool_name=tool, tool_input=inp, error="Exit code 1\nFAILED",
                is_interrupt=False)


# (a) ---------------------------------------------------------------------------------------

def test_a_confirm_before_delete(proj):
    out = prompt(proj, "ask me before deleting anything")
    assert "r1" in out["hookSpecificOutput"]["additionalContext"]
    d, reason = pre(proj, "Bash", {"command": "rm -rf build"})
    assert d == "deny"
    assert "r1" in reason and "build" in reason and "Ask the user" in reason and "turn 1" in reason
    prompt(proj, "yes go ahead and delete build")
    d, reason = pre(proj, "Bash", {"command": "rm -rf build"})
    assert d == "allow", reason
    d, _ = pre(proj, "Bash", {"command": "rm -rf build"})
    assert d == "deny"
    # the rule is still there and in the state file
    st = json.load(open(os.path.join(proj, ".holdfast", "state.json")))
    assert st["rules"][0]["kind"] == "confirm" and st["confirmations"] == []


# (b) ---------------------------------------------------------------------------------------

def test_b_push_only_after_tests(proj):
    prompt(proj, "only push after tests pass")
    d, reason = pre(proj, "Bash", {"command": "git push origin main"})
    assert d == "deny" and "tests" in reason.lower()
    assert post(proj, "Bash", {"command": "pytest -q"}) == {}
    d, _ = pre(proj, "Bash", {"command": "git push origin main"})
    assert d == "allow"
    post(proj, "Edit", {"file_path": "src/a.py", "old_string": "y", "new_string": "z", "replace_all": False})
    d, _ = pre(proj, "Bash", {"command": "git push origin main"})
    assert d == "deny"
    post(proj, "Bash", {"command": "pytest -q"}, ok=False)
    d, _ = pre(proj, "Bash", {"command": "git push"})
    assert d == "deny"


# (c) ---------------------------------------------------------------------------------------

def test_c_forbid_edits_under_tests(proj):
    prompt(proj, "don't touch tests/")
    d, reason = pre(proj, "Edit", {"file_path": os.path.join(proj, "tests/test_a.py"), "old_string": "x", "new_string": "y"})
    assert d == "deny" and "tests/test_a.py" in reason
    d, _ = pre(proj, "Edit", {"file_path": os.path.join(proj, "src/a.py"), "old_string": "y", "new_string": "z"})
    assert d == "allow"
    for cmd in ["echo x > tests/test_a.py", "sed -i s/a/b/ tests/test_a.py", "cp src/a.py tests/b.py",
                "cat > tests/test_a.py <<EOF\nnew\nEOF", "cd tests && echo x > test_a.py",
                "sh -c 'echo x > tests/test_a.py'", "echo x | tee tests/test_a.py", "rm tests/test_a.py",
                "mv src/a.py tests/a.py", "touch tests/new.py", "git checkout -- tests/test_a.py"]:
        d, _ = pre(proj, "Bash", {"command": cmd})
        assert d == "deny", cmd
    for cmd in ["echo x > src/a.py", "cat tests/test_a.py", "pytest tests/", "ls tests", "sed s/a/b/ tests/test_a.py"]:
        d, _ = pre(proj, "Bash", {"command": cmd})
        assert d == "allow", cmd


# (d) ---------------------------------------------------------------------------------------

def test_d_session_start_after_compact(proj):
    prompt(proj, "never force push")
    prompt(proj, "don't install packages")
    assert hook("PreCompact", proj, trigger="auto") == {}
    out = hook("SessionStart", proj, source="compact")
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "r1" in ctx and "r2" in ctx and "force" in ctx and "install" in ctx
    d, _ = pre(proj, "Bash", {"command": "git push --force"})
    assert d == "deny"
    out = hook("SubagentStart", proj, agent_id="agent-1", agent_type="general-purpose")
    assert "r1" in out["hookSpecificOutput"]["additionalContext"]
    # subagent is governed by the same state
    out = hook("PreToolUse", proj, tool_name="Bash", tool_input={"command": "pip install requests"}, agent_id="agent-1")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert hook("Stop", proj, stop_hook_active=False) == {}


# (e) ---------------------------------------------------------------------------------------

def test_e_subprocess_latency(proj):
    prompt(proj, "ask me before deleting anything")
    prompt(proj, "only push after tests pass")
    times = []
    for _ in range(20):
        t0 = time.perf_counter()
        pre(proj, "Bash", {"command": "sed -i s/a/b/ src/a.py && rm -rf build/x"})
        times.append(time.perf_counter() - t0)
    times.sort()
    assert times[len(times) // 2] < 0.300, f"median {times[len(times)//2]*1000:.0f}ms"


# misc -----------------------------------------------------------------------------------------

def test_fail_open_on_garbage_and_fail_closed_env(proj):
    proc = subprocess.run([HOLDFAST, "hook", "PreToolUse"], input="not json", capture_output=True, text=True, cwd=proj)
    assert proc.returncode == 0 and json.loads(proc.stdout) == {}
    env = {**os.environ, "HOLDFAST_FAIL_CLOSED": "1"}
    proc = subprocess.run([HOLDFAST, "hook", "PreToolUse"], input="not json", capture_output=True, text=True,
                          cwd=proj, env=env)
    assert proc.returncode == 2


def test_cli_install_and_status(proj):
    proc = subprocess.run([HOLDFAST, "--project", proj, "install"], capture_output=True, text=True)
    assert proc.returncode == 0
    data = json.load(open(os.path.join(proj, ".claude", "settings.json")))
    assert "PreToolUse" in data["hooks"] and data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "holdfast hook PreToolUse"
    proc = subprocess.run([HOLDFAST, "--project", proj, "add", "never force push"], capture_output=True, text=True)
    assert proc.returncode == 0 and "r1" in proc.stdout
    proc = subprocess.run([HOLDFAST, "--project", proj, "status"], capture_output=True, text=True)
    assert "r1" in proc.stdout and "tests_passed" in proc.stdout


def test_python_module_invocation(proj):
    payload = json.dumps({"cwd": proj, "prompt": "no network"})
    proc = subprocess.run([sys.executable, "-m", "holdfast.cli", "hook", "UserPromptSubmit"], input=payload,
                          capture_output=True, text=True, cwd=proj)
    assert proc.returncode == 0 and "network" in proc.stdout
