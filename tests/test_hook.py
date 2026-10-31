"""End-to-end tests for holdfast.hook.run()/main() with realistic hook payloads and on-disk state."""
import io
import json
import threading

import pytest

from holdfast import hook
from holdfast.monitor import Config
from holdfast.state import Store

SID = "sess-0001"


@pytest.fixture
def proj(tmp_path, monkeypatch):
    """A fake project with HOLDFAST_DIR and CLAUDE_PROJECT_DIR pointed at tmp_path."""
    (tmp_path / "data").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    monkeypatch.setenv("HOLDFAST_DIR", str(tmp_path / ".holdfast"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("HOLDFAST_FAIL_CLOSED", raising=False)
    return tmp_path


@pytest.fixture
def cfg():
    return Config(opaque_policy="ask", confirm_via="deny", inject_context=True, llm_extract=False, enforce=True)


def payload(event, proj, **kw):
    base = {"session_id": SID, "transcript_path": "/tmp/t.jsonl", "cwd": str(proj), "hook_event_name": event,
            "permission_mode": "default"}
    base.update(kw)
    return base


def pre(proj, tool, tool_input, tool_use_id="tu-1", **kw):
    return payload("PreToolUse", proj, tool_name=tool, tool_input=tool_input, tool_use_id=tool_use_id, **kw)


def post(proj, tool, tool_input, response):
    return payload("PostToolUse", proj, tool_name=tool, tool_input=tool_input, tool_response=response)


def prompt(proj, text):
    return payload("UserPromptSubmit", proj, prompt=text)


def bash_input(cmd):
    return {"command": cmd, "description": "test"}


def load_state(proj):
    return json.loads((proj / ".holdfast" / "sessions" / f"{SID}.json").read_text())


def audit(proj):
    p = proj / ".holdfast" / "audit.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


# ---------------------------------------------------------------- PreToolUse

class TestPreToolUse:
    def test_deny_shape(self, proj, cfg):
        assert hook.run("UserPromptSubmit", prompt(proj, "Never delete anything in data/"), cfg) is not None
        out = hook.run("PreToolUse", pre(proj, "Bash", bash_input("rm -rf data/old")), cfg)
        assert set(out) == {"hookSpecificOutput"}
        hso = out["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "deny"
        assert "Holdfast rule R-" in hso["permissionDecisionReason"] and "data/old" in hso["permissionDecisionReason"]
        assert json.loads(json.dumps(out)) == out

    def test_allow_returns_none(self, proj, cfg):
        hook.run("UserPromptSubmit", prompt(proj, "Never delete anything in data/"), cfg)
        assert hook.run("PreToolUse", pre(proj, "Bash", bash_input("ls data/")), cfg) is None
        assert hook.run("PreToolUse", pre(proj, "Edit", {"file_path": str(proj / "src/a.py"), "old_string": "a", "new_string": "b"}), cfg) is None

    def test_ask_shape(self, proj):
        c = Config(opaque_policy="ask", confirm_via="ask", inject_context=True, llm_extract=False, enforce=True)
        hook.run("UserPromptSubmit", prompt(proj, "Ask me before pushing"), c)
        out = hook.run("PreToolUse", pre(proj, "Bash", bash_input("git push origin main")), c)
        assert out["hookSpecificOutput"]["permissionDecision"] == "ask"

    def test_task_started_and_decisions_counted(self, proj, cfg):
        hook.run("UserPromptSubmit", prompt(proj, "Never delete anything in data/"), cfg)
        assert load_state(proj)["task_started"] is False
        hook.run("PreToolUse", pre(proj, "Bash", bash_input("ls")), cfg)
        hook.run("PreToolUse", pre(proj, "Bash", bash_input("rm -rf data")), cfg)
        st = load_state(proj)
        assert st["task_started"] is True and st["task_started_at"]
        assert st["decisions"] == {"allow": 1, "deny": 1, "ask": 0}

    def test_observe_only_mode(self, proj):
        c = Config(opaque_policy="ask", confirm_via="deny", inject_context=True, llm_extract=False, enforce=False)
        hook.run("UserPromptSubmit", prompt(proj, "Never delete anything in data/"), c)
        assert hook.run("PreToolUse", pre(proj, "Bash", bash_input("rm -rf data")), c) is None
        recs = [r for r in audit(proj) if r["event"] == "PreToolUse"]
        assert recs[-1]["decision"] == "observe:deny"

    def test_audit_record(self, proj, cfg):
        hook.run("UserPromptSubmit", prompt(proj, "Never delete anything in data/"), cfg)
        hook.run("PreToolUse", pre(proj, "Bash", bash_input("rm -rf data"), agent_id="agent-7"), cfg)
        rec = [r for r in audit(proj) if r["event"] == "PreToolUse"][-1]
        assert rec["decision"] == "deny" and rec["tool"] == "Bash" and rec["agent_id"] == "agent-7"
        assert rec["session_id"] == SID and rec["effects"][0]["action"] == "delete" and "latency_ms" in rec

    def test_cwd_relative_paths_resolve_against_project_root(self, proj, cfg):
        hook.run("UserPromptSubmit", prompt(proj, "Never delete anything in data/"), cfg)
        sub = proj / "src"
        # from a subdirectory, ../data/x is data/x relative to the project root
        out = hook.run("PreToolUse", pre(proj, "Bash", bash_input("rm ../data/x"), cwd=str(sub)), cfg)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert hook.run("PreToolUse", pre(proj, "Bash", bash_input("rm data/x"), cwd=str(sub)), cfg) is None


# ---------------------------------------------------------------- PostToolUse

class TestPostToolUse:
    def test_test_pass_recorded(self, proj, cfg):
        assert hook.run("PostToolUse", post(proj, "Bash", bash_input("pytest -q"),
                                            {"stdout": "3 passed in 0.1s", "stderr": "", "interrupted": False, "isImage": False}), cfg) is None
        st = load_state(proj)
        assert st["tests_passed_at"] and st["last_edit_at"] is None
        assert any(r["event"] == "PostToolUse" and "tests_passed_at" in r["state_changed"] for r in audit(proj))

    def test_edit_recorded(self, proj, cfg):
        hook.run("PostToolUse", post(proj, "Edit", {"file_path": str(proj / "src/a.py"), "old_string": "a", "new_string": "b"}, {"filePath": "x"}), cfg)
        assert load_state(proj)["last_edit_at"]

    def test_failure_recorded(self, proj, cfg):
        hook.run("PostToolUse", post(proj, "Bash", bash_input("pytest"), {"stdout": "1 failed, 2 passed", "stderr": ""}), cfg)
        st = load_state(proj)
        assert st["tests_passed_at"] is None and st["tests_failed_at"]

    def test_require_before_cycle_end_to_end(self, proj, cfg):
        hook.run("UserPromptSubmit", prompt(proj, "Only push after tests pass"), cfg)
        push = pre(proj, "Bash", bash_input("git push origin main"))
        assert hook.run("PreToolUse", push, cfg)["hookSpecificOutput"]["permissionDecision"] == "deny"
        hook.run("PostToolUse", post(proj, "Bash", bash_input("pytest"), {"stdout": "9 passed", "stderr": ""}), cfg)
        assert hook.run("PreToolUse", push, cfg) is None
        hook.run("PostToolUse", post(proj, "Write", {"file_path": str(proj / "src/b.py"), "content": "x"}, {}), cfg)
        assert hook.run("PreToolUse", push, cfg)["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------- UserPromptSubmit

class TestUserPromptSubmit:
    def test_additional_context_on_new_rules(self, proj, cfg):
        out = hook.run("UserPromptSubmit", prompt(proj, "Fix the bug. Never force push and don't touch the tests."), cfg)
        hso = out["hookSpecificOutput"]
        assert hso["hookEventName"] == "UserPromptSubmit"
        ctx = hso["additionalContext"]
        assert "registered 2 new constraint(s)" in ctx and "never force_push" in ctx and "never modify on paths tests/**" in ctx
        st = load_state(proj)
        assert len(st["rules"]) == 2 and st["turn"] == 1 and all(r["source"] == "user" for r in st["rules"])

    def test_no_rules_returns_none(self, proj, cfg):
        assert hook.run("UserPromptSubmit", prompt(proj, "Fix the bug in auth.py"), cfg) is None
        assert load_state(proj)["turn"] == 1

    def test_inject_disabled(self, proj):
        c = Config(opaque_policy="ask", confirm_via="deny", inject_context=False, llm_extract=False, enforce=True)
        assert hook.run("UserPromptSubmit", prompt(proj, "Never force push"), c) is None
        assert len(load_state(proj)["rules"]) == 1   # still registered

    def test_confirmation_cycle(self, proj, cfg):
        hook.run("UserPromptSubmit", prompt(proj, "Ask me before deleting anything"), cfg)
        rm = pre(proj, "Bash", bash_input("rm -rf build"), tool_use_id="tu-9")
        out = hook.run("PreToolUse", rm, cfg)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert load_state(proj)["pending_confirm"]["tool_use_id"] == "tu-9"
        out = hook.run("UserPromptSubmit", prompt(proj, "yes"), cfg)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "confirmation recorded" in ctx and "delete on build" in ctx
        assert hook.run("PreToolUse", rm, cfg) is None
        assert hook.run("PreToolUse", rm, cfg)["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_decline(self, proj, cfg):
        hook.run("UserPromptSubmit", prompt(proj, "Ask me before deleting anything"), cfg)
        hook.run("PreToolUse", pre(proj, "Bash", bash_input("rm -rf build")), cfg)
        out = hook.run("UserPromptSubmit", prompt(proj, "no"), cfg)
        assert "declined" in out["hookSpecificOutput"]["additionalContext"]
        assert load_state(proj)["pending_confirm"] is None

    def test_list_and_drop(self, proj, cfg):
        hook.run("UserPromptSubmit", prompt(proj, "Never force push"), cfg)
        rid = load_state(proj)["rules"][0]["id"]
        out = hook.run("UserPromptSubmit", prompt(proj, "holdfast: list"), cfg)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "Holdfast registered constraints" in ctx and rid in ctx
        out = hook.run("UserPromptSubmit", prompt(proj, f"holdfast: drop {rid}"), cfg)
        assert f"dropped rule(s) {rid}" in out["hookSpecificOutput"]["additionalContext"]
        assert load_state(proj)["rules"][0]["active"] is False
        assert hook.run("PreToolUse", pre(proj, "Bash", bash_input("git push -f")), cfg) is None


# ---------------------------------------------------------------- SessionStart / SubagentStart / compaction

class TestSessionEvents:
    def test_session_start_without_rules(self, proj, cfg):
        assert hook.run("SessionStart", payload("SessionStart", proj, source="startup"), cfg) is None

    def test_compact_reinjects_and_counts(self, proj, cfg):
        hook.run("UserPromptSubmit", prompt(proj, "Never delete anything in data/"), cfg)
        out = hook.run("SessionStart", payload("SessionStart", proj, source="compact"), cfg)
        hso = out["hookSpecificOutput"]
        assert hso["hookEventName"] == "SessionStart"
        assert "session start: compact" in hso["additionalContext"] and "never delete on paths data/**" in hso["additionalContext"]
        assert load_state(proj)["compactions"] == 1
        hook.run("SessionStart", payload("SessionStart", proj, source="compact"), cfg)
        assert load_state(proj)["compactions"] == 2
        hook.run("SessionStart", payload("SessionStart", proj, source="resume"), cfg)
        assert load_state(proj)["compactions"] == 2
        # rules still enforced after "compaction"
        assert hook.run("PreToolUse", pre(proj, "Bash", bash_input("rm -rf data")), cfg)["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_subagent_start(self, proj, cfg):
        hook.run("UserPromptSubmit", prompt(proj, "Never force push"), cfg)
        out = hook.run("SubagentStart", payload("SubagentStart", proj, agent_id="a1", agent_type="Explore"), cfg)
        hso = out["hookSpecificOutput"]
        assert hso["hookEventName"] == "SubagentStart" and "for this subagent" in hso["additionalContext"]
        assert load_state(proj)["subagents"] == 1
        # a subagent tool call (same session id, agent_id set) is governed by the same rules
        out = hook.run("PreToolUse", pre(proj, "Bash", bash_input("git push --force"), agent_id="a1"), cfg)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_post_compact_counts_but_returns_none(self, proj, cfg):
        hook.run("UserPromptSubmit", prompt(proj, "Never force push"), cfg)
        assert hook.run("PostCompact", payload("PostCompact", proj, trigger="auto"), cfg) is None
        assert load_state(proj)["compactions"] == 1

    def test_pre_compact(self, proj, cfg):
        assert hook.run("PreCompact", payload("PreCompact", proj, trigger="manual"), cfg) is None
        assert load_state(proj)["last_precompact_at"]

    def test_unknown_event_is_audited(self, proj, cfg):
        assert hook.run("Stop", payload("Stop", proj), cfg) is None
        assert audit(proj)[-1]["event"] == "Stop"


# ---------------------------------------------------------------- persistence / standing rules / concurrency

class TestPersistence:
    def test_state_survives_separate_runs(self, proj, cfg):
        hook.run("UserPromptSubmit", prompt(proj, "Never delete anything in data/"), cfg)
        # fresh Store objects each call simulate separate hook processes; state must come from disk
        for _ in range(3):
            out = hook.run("PreToolUse", pre(proj, "Bash", bash_input("rm -rf data")), cfg)
            assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        st = load_state(proj)
        assert st["decisions"]["deny"] == 3 and len(st["rules"]) == 1

    def test_sessions_are_isolated(self, proj, cfg):
        hook.run("UserPromptSubmit", prompt(proj, "Never delete anything in data/"), cfg)
        other = pre(proj, "Bash", bash_input("rm -rf data"))
        other["session_id"] = "sess-0002"
        assert hook.run("PreToolUse", other, cfg) is None

    def test_standing_rules_from_rules_json(self, proj, cfg):
        d = proj / ".holdfast"
        d.mkdir(exist_ok=True)
        (d / "rules.json").write_text(json.dumps({"rules": [{"kind": "forbid", "action": "force_push", "text": "team policy"}]}))
        out = hook.run("PreToolUse", pre(proj, "Bash", bash_input("git push -f origin main")), cfg)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "team policy" in out["hookSpecificOutput"]["permissionDecisionReason"]
        st = load_state(proj)
        assert st["rules"][0]["source"] == "config" and st["rules"][0]["id"].startswith("R-")

    def test_corrupt_state_file_recovers(self, proj, cfg):
        hook.run("UserPromptSubmit", prompt(proj, "Never force push"), cfg)
        (proj / ".holdfast" / "sessions" / f"{SID}.json").write_text("{not json")
        assert hook.run("PreToolUse", pre(proj, "Bash", bash_input("ls")), cfg) is None
        assert load_state(proj)["decisions"]["allow"] == 1

    def test_concurrent_pretooluse(self, proj, cfg):
        hook.run("UserPromptSubmit", prompt(proj, "Never delete anything in data/"), cfg)
        results = [None] * 8
        errors = []

        def worker(i):
            try:
                cmd = "rm -rf data" if i % 2 == 0 else "ls"
                results[i] = hook.run("PreToolUse", pre(proj, "Bash", bash_input(cmd), tool_use_id=f"tu-{i}"), cfg)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        denies = sum(1 for r in results if r and r["hookSpecificOutput"]["permissionDecision"] == "deny")
        allows = sum(1 for r in results if r is None)
        assert denies == 4 and allows == 4
        st = load_state(proj)   # must be valid JSON
        assert st["decisions"] == {"allow": 4, "deny": 4, "ask": 0}
        assert len([r for r in audit(proj) if r["event"] == "PreToolUse"]) == 8
        assert not list((proj / ".holdfast" / "sessions").glob("*.tmp"))


# ---------------------------------------------------------------- main(): stdin/stdout contract, fail-open / fail-closed

class TestMain:
    def test_main_emits_json(self, proj, monkeypatch, capsys):
        hook.run("UserPromptSubmit", prompt(proj, "Never delete anything in data/"))
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(pre(proj, "Bash", bash_input("rm -rf data")))))
        assert hook.main(["PreToolUse"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_main_event_from_payload(self, proj, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(prompt(proj, "Never force push"))))
        assert hook.main([]) == 0
        assert "registered 1 new constraint" in capsys.readouterr().out

    def test_main_allow_prints_nothing(self, proj, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(pre(proj, "Bash", bash_input("ls")))))
        assert hook.main(["PreToolUse"]) == 0
        assert capsys.readouterr().out == ""

    def test_empty_stdin(self, proj, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        assert hook.main(["PreToolUse"]) == 0
        assert capsys.readouterr().out == ""

    def test_garbage_stdin_fails_closed_by_default(self, proj, monkeypatch, capsys):
        # a guard that allows on its own errors is not a guard (REVIEW.md): PreToolUse denies with a reason
        monkeypatch.setattr("sys.stdin", io.StringIO("this is not json {"))
        assert hook.main(["PreToolUse"]) == 0
        captured = capsys.readouterr()
        out = json.loads(captured.out)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "fail-closed" in out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "holdfast hook error" in captured.err

    def test_fail_open_opt_in(self, proj, monkeypatch, capsys):
        monkeypatch.setenv("HOLDFAST_FAIL_CLOSED", "0")
        monkeypatch.setattr("sys.stdin", io.StringIO("this is not json {"))
        assert hook.main(["PreToolUse"]) == 0
        captured = capsys.readouterr()
        assert captured.out == "" and "holdfast hook error" in captured.err

    def test_fail_closed_only_applies_to_pretooluse(self, proj, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
        assert hook.main(["PostToolUse"]) == 0 and capsys.readouterr().out == ""

    def test_internal_error_denies_then_allows_when_opted_out(self, proj, monkeypatch, capsys):
        monkeypatch.setattr(hook, "evaluate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(pre(proj, "Bash", bash_input("ls")))))
        assert hook.main(["PreToolUse"]) == 0
        assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"
        monkeypatch.setenv("HOLDFAST_FAIL_CLOSED", "0")
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(pre(proj, "Bash", bash_input("ls")))))
        assert hook.main(["PreToolUse"]) == 0
        assert capsys.readouterr().out == ""
