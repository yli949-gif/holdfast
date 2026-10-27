"""In-process tests for holdfast.hooks (event handlers) and holdfast.cli."""
import io
import json
import os
import time

import pytest

from holdfast import cli, hooks
from holdfast.state import Store


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("x\n")
    (tmp_path / "src" / "a.py").write_text("y\n")
    return str(tmp_path)


def payload(event, cwd, **kw):
    base = {"session_id": "s1", "transcript_path": "/tmp/t.jsonl", "cwd": cwd, "hook_event_name": event,
            "permission_mode": "default"}
    base.update(kw)
    return base


def pre(cwd, tool, inp, **kw):
    return hooks.handle("PreToolUse", payload("PreToolUse", cwd, tool_name=tool, tool_input=inp, **kw))


def decision(out):
    return out.get("hookSpecificOutput", {}).get("permissionDecision", "allow")


def reason(out):
    return out.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")


def prompt(cwd, text):
    return hooks.handle("UserPromptSubmit", payload("UserPromptSubmit", cwd, prompt=text))


# --- UserPromptSubmit -------------------------------------------------------------------

def test_prompt_adds_rule_and_returns_context(proj):
    out = prompt(proj, "ask me before deleting anything")
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "r1" in ctx and "delete" in ctx
    st = Store(proj).load()
    assert len(st.rules) == 1 and st.rules[0].source["turn"] == 1 and st.meta["last_turn"] == 1


def test_prompt_without_constraint_is_noop(proj):
    assert prompt(proj, "fix the failing test") == {}
    assert Store(proj).load().rules == []


def test_prompt_dedupes_rules(proj):
    prompt(proj, "never force push")
    prompt(proj, "don't force-push")
    assert len(Store(proj).load().rules) == 1


def test_prompt_confirmation_and_revocation(proj):
    prompt(proj, "ask me before deleting anything")
    prompt(proj, "yes go ahead and delete build")
    st = Store(proj).load()
    assert st.confirmations and st.confirmations[0]["action"] == "delete" and st.confirmations[0]["target"] == "build"
    prompt(proj, "actually never mind")
    assert Store(proj).load().confirmations == []


# --- PreToolUse ---------------------------------------------------------------------------

def test_pretooluse_no_rules_allows(proj):
    out = pre(proj, "Bash", {"command": "rm -rf build"})
    assert decision(out) == "allow"


def test_pretooluse_deny_shape_and_reason(proj):
    prompt(proj, "ask me before deleting anything")
    out = pre(proj, "Bash", {"command": "rm -rf build"})
    h = out["hookSpecificOutput"]
    assert h["hookEventName"] == "PreToolUse" and h["permissionDecision"] == "deny"
    assert "r1" in h["permissionDecisionReason"] and "build" in h["permissionDecisionReason"]
    assert "Ask the user" in h["permissionDecisionReason"]


def test_pretooluse_emit_allow_env(proj, monkeypatch):
    monkeypatch.setenv("HOLDFAST_EMIT_ALLOW", "1")
    out = pre(proj, "Bash", {"command": "ls"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_pretooluse_ask_for_opaque(proj):
    prompt(proj, "never delete anything")
    out = pre(proj, "Bash", {"command": "python - <<EOF\nimport shutil\nshutil.rmtree('x')\nEOF"})
    assert decision(out) == "ask"


def test_pretooluse_logs_event(proj):
    prompt(proj, "never delete anything")
    pre(proj, "Bash", {"command": "rm -rf build"}, agent_id="agent-7")
    lines = open(os.path.join(proj, ".holdfast", "events.jsonl")).read().splitlines()
    ev = json.loads(lines[-1])
    assert ev["tool"] == "Bash" and ev["decision"] == "deny" and ev["rule_id"] == "r1"
    assert ev["agent_id"] == "agent-7" and "delete" in ev["category"] and ev["event"] == "PreToolUse"


def test_pretooluse_missing_cwd_uses_process_cwd(proj, monkeypatch):
    monkeypatch.chdir(proj)
    out = hooks.handle("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert decision(out) == "allow"


# --- PostToolUse / failure / compact / session ---------------------------------------------

def test_post_tool_use_flips_predicate(proj):
    prompt(proj, "only push after tests pass")
    assert decision(pre(proj, "Bash", {"command": "git push"})) == "deny"
    assert hooks.handle("PostToolUse", payload("PostToolUse", proj, tool_name="Bash",
                                               tool_input={"command": "pytest -q"},
                                               tool_response={"stdout": "ok", "stderr": "", "interrupted": False})) == {}
    assert decision(pre(proj, "Bash", {"command": "git push"})) == "allow"
    hooks.handle("PostToolUse", payload("PostToolUse", proj, tool_name="Edit",
                                        tool_input={"file_path": "src/a.py", "old_string": "a", "new_string": "b"},
                                        tool_response={}))
    assert decision(pre(proj, "Bash", {"command": "git push"})) == "deny"


def test_post_tool_use_interrupted_test_run_does_not_pass(proj):
    prompt(proj, "only push after tests pass")
    hooks.handle("PostToolUse", payload("PostToolUse", proj, tool_name="Bash", tool_input={"command": "pytest"},
                                        tool_response={"stdout": "", "stderr": "", "interrupted": True}))
    assert decision(pre(proj, "Bash", {"command": "git push"})) == "deny"


def test_post_tool_use_failure(proj):
    prompt(proj, "only push after tests pass")
    hooks.handle("PostToolUse", payload("PostToolUse", proj, tool_name="Bash", tool_input={"command": "pytest"},
                                        tool_response={"stdout": "", "stderr": "", "interrupted": False}))
    assert decision(pre(proj, "Bash", {"command": "git push"})) == "allow"
    out = hooks.handle("PostToolUseFailure", payload("PostToolUseFailure", proj, tool_name="Bash",
                                                     tool_input={"command": "pytest"}, error="Exit code 1\nFAILED",
                                                     is_interrupt=False))
    assert out == {}
    assert decision(pre(proj, "Bash", {"command": "git push"})) == "deny"


def test_precompact_and_sessionstart(proj):
    prompt(proj, "don't touch tests/")
    assert hooks.handle("PreCompact", payload("PreCompact", proj, trigger="auto")) == {}
    assert Store(proj).load().meta["compact_count"] == 1
    out = hooks.handle("SessionStart", payload("SessionStart", proj, source="compact"))
    h = out["hookSpecificOutput"]
    assert h["hookEventName"] == "SessionStart" and "r1" in h["additionalContext"] and "tests" in h["additionalContext"]
    out = hooks.handle("SubagentStart", payload("SubagentStart", proj, agent_id="a1", agent_type="general"))
    assert out["hookSpecificOutput"]["hookEventName"] == "SubagentStart" and "r1" in out["hookSpecificOutput"]["additionalContext"]
    assert hooks.handle("Stop", payload("Stop", proj)) == {}


def test_sessionstart_without_rules(proj):
    assert hooks.handle("SessionStart", payload("SessionStart", proj, source="startup")) == {}


def test_unknown_event(proj):
    assert hooks.handle("Weird", payload("Weird", proj)) == {}


# --- run (stdin/stdout, exit codes) ---------------------------------------------------------

def test_run_reads_stdin_writes_stdout(proj):
    prompt(proj, "never force push")
    inp = io.StringIO(json.dumps(payload("PreToolUse", proj, tool_name="Bash", tool_input={"command": "git push -f"})))
    out = io.StringIO()
    code = hooks.run("PreToolUse", inp, out)
    assert code == 0
    assert decision(json.loads(out.getvalue())) == "deny"


def test_run_bad_json_fails_open(proj, monkeypatch):
    monkeypatch.chdir(proj)
    out = io.StringIO()
    code = hooks.run("PreToolUse", io.StringIO("{not json"), out, err=io.StringIO())
    assert code == 0 and json.loads(out.getvalue()) == {}


def test_run_internal_error_logs_and_allows(proj, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(hooks, "classify", boom)
    inp = json.dumps(payload("PreToolUse", proj, tool_name="Bash", tool_input={"command": "ls"}))
    out = io.StringIO()
    assert hooks.run("PreToolUse", io.StringIO(inp), out, err=io.StringIO()) == 0
    assert json.loads(out.getvalue()) == {}
    assert "kaboom" in open(os.path.join(proj, ".holdfast", "errors.log")).read()
    monkeypatch.setenv("HOLDFAST_FAIL_CLOSED", "1")
    err = io.StringIO()
    assert hooks.run("PreToolUse", io.StringIO(inp), io.StringIO(), err=err) == 2
    assert "kaboom" in err.getvalue()


def test_pretooluse_latency_in_process(proj):
    prompt(proj, "ask me before deleting anything")
    prompt(proj, "only push after tests pass")
    prompt(proj, "don't touch tests/")
    times = []
    for i in range(20):
        t0 = time.perf_counter()
        pre(proj, "Bash", {"command": f"sudo env A=B sh -c 'cd src && sed -i s/a/b/ a.py; rm -rf build{i}' | tee out.log"})
        times.append(time.perf_counter() - t0)
    times.sort()
    assert times[len(times) // 2] < 0.050, f"median {times[len(times)//2]*1000:.1f}ms"


# --- CLI -------------------------------------------------------------------------------------

def run_cli(args, cwd):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["--project", cwd] + args, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_cli_add_list_remove_status(proj):
    code, out, _ = run_cli(["add", "never force push"], proj)
    assert code == 0 and "r1" in out
    code, out, _ = run_cli(["list"], proj)
    assert code == 0 and "r1" in out and "force_push" in out
    code, out, _ = run_cli(["status"], proj)
    assert code == 0 and "tests_passed" in out and "r1" in out
    code, out, _ = run_cli(["remove", "r1"], proj)
    assert code == 0
    code, out, _ = run_cli(["list"], proj)
    assert "r1" not in out
    code, out, err = run_cli(["remove", "r9"], proj)
    assert code == 1


def test_cli_add_manual_and_unparseable(proj):
    code, out, err = run_cli(["add", "make it fast"], proj)
    assert code == 1 and "No constraint" in err
    code, out, _ = run_cli(["add", "--kind", "forbid", "--action", "run_cmd", "--target", "docker", "docker is banned"], proj)
    assert code == 0
    st = Store(proj).load()
    assert st.rules[0].action == "run_cmd" and st.rules[0].source["via"] == "manual"


def test_cli_list_json(proj):
    run_cli(["add", "don't commit"], proj)
    code, out, _ = run_cli(["list", "--json"], proj)
    assert json.loads(out)[0]["action"] == "commit"


def test_cli_install_merges_settings(proj):
    settings = os.path.join(proj, ".claude", "settings.json")
    os.makedirs(os.path.dirname(settings))
    with open(settings, "w") as fh:
        json.dump({"permissions": {"allow": ["Bash(ls:*)"]},
                   "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}]}}, fh)
    code, out, _ = run_cli(["install"], proj)
    assert code == 0
    data = json.load(open(settings))
    assert data["permissions"]["allow"] == ["Bash(ls:*)"]
    pre_groups = data["hooks"]["PreToolUse"]
    assert pre_groups[0]["hooks"][0]["command"] == "echo hi"
    hf = [g for g in pre_groups if any("holdfast hook PreToolUse" in h["command"] for h in g["hooks"])]
    assert len(hf) == 1 and hf[0]["matcher"] == "Bash|Edit|Write|MultiEdit|NotebookEdit"
    assert hf[0]["hooks"][0]["timeout"] == 10 and hf[0]["hooks"][0]["onFailure"] == "continue"
    for ev in ("PostToolUse", "PostToolUseFailure", "UserPromptSubmit", "SessionStart", "SubagentStart", "PreCompact"):
        assert any("holdfast hook " + ev in h["command"] for g in data["hooks"][ev] for h in g["hooks"]), ev
    assert [g["matcher"] for g in data["hooks"]["PostToolUseFailure"]] == ["Bash"]
    assert "matcher" not in data["hooks"]["UserPromptSubmit"][0]
    # idempotent
    run_cli(["install"], proj)
    assert len(json.load(open(settings))["hooks"]["PreToolUse"]) == 2
    assert os.path.isdir(os.path.join(proj, ".holdfast"))


def test_cli_install_custom_path(proj, tmp_path):
    p = str(tmp_path / "elsewhere" / "settings.json")
    code, out, _ = run_cli(["install", "--settings-path", p], proj)
    assert code == 0 and os.path.exists(p)


def test_snippet_matches_install():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    snippet = json.load(open(os.path.join(here, "hooks", "settings.snippet.json")))
    assert snippet["hooks"] == cli.hook_settings()["hooks"]


def test_cli_hook_subcommand(proj, monkeypatch):
    inp = io.StringIO(json.dumps(payload("UserPromptSubmit", proj, prompt="don't install packages")))
    monkeypatch.setattr("sys.stdin", inp)
    out = io.StringIO()
    code = cli.main(["hook", "UserPromptSubmit"], stdout=out)
    assert code == 0 and "r1" in json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
