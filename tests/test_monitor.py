"""Tests for holdfast.monitor: evaluate / observe_post / handle_prompt / register_rules / context_block."""
import time

import pytest

from holdfast.monitor import Config, context_block, evaluate, handle_prompt, observe_post, register_rules
from holdfast.rules import Rule, Scope
from holdfast.state import default_state

CWD = "/proj"
ROOT = "/proj"


def cfg(**kw):
    base = dict(opaque_policy="ask", confirm_via="deny", inject_context=True, llm_extract=False, enforce=True)
    base.update(kw)
    return Config(**base)


def state_with(*rules, **extra):
    st = default_state()
    st["rules"] = [r.as_dict() for r in rules]
    st.update(extra)
    return st


def ev(tool, tin, st, c=None, tool_use_id="tu-1"):
    return evaluate(tool, tin, st, CWD, ROOT, c or cfg(), tool_use_id)


def bash(cmd):
    return "Bash", {"command": cmd}


FORBID_DELETE_DATA = Rule(id="R-del001", kind="forbid", action="delete", scope=Scope(paths=["data/**"]), text="never delete data")
CONFIRM_PUSH = Rule(id="R-cnf001", kind="confirm", action="push", text="ask before pushing")
REQ_TESTS_PUSH = Rule(id="R-req001", kind="require_before", action="push", prerequisite="tests_pass", text="push only after tests pass")
FREEZE_TESTS = Rule(id="R-frz001", kind="freeze", action="modify", scope=Scope(paths=["tests/**"]), text="don't modify tests after you start")


# ---------------------------------------------------------------- forbid

class TestForbid:
    def test_deny_in_scope(self):
        st = state_with(FORBID_DELETE_DATA)
        d = ev(*bash("rm -rf data/x"), st)
        assert d.decision == "deny" and d.rule_id == "R-del001" and not d.uncertain
        assert "R-del001" in d.reason and "never delete data" in d.reason and "data/x" in d.reason
        assert d.matched and d.matched[0]["rule_id"] == "R-del001" and d.matched[0]["decision"] == "deny"

    def test_allow_out_of_scope(self):
        st = state_with(FORBID_DELETE_DATA)
        d = ev(*bash("rm -rf build/"), st)
        assert d.decision == "allow" and d.rule_id == "" and d.matched == []
        assert [e.action for e in d.effects] == ["delete"]

    def test_allow_other_action_same_path(self):
        d = ev("Read", {"file_path": "/proj/data/x"}, state_with(FORBID_DELETE_DATA))
        assert d.decision == "allow"

    def test_deny_via_bypass_paths(self):
        st = state_with(FORBID_DELETE_DATA)
        for cmd in ("bash -c 'rm data/x'", "rm -rf .", "git clean -fdx", "eval rm data/x", "ls\nrm data/x",
                    "sudo -u root rm data/x", "python -c \"import os; os.remove('data/x')\"", "find data -delete"):
            d = ev(*bash(cmd), st)
            assert d.decision in ("deny", "ask") and d.rule_id == "R-del001", cmd
        # certain bypasses are hard denies
        for cmd in ("bash -c 'rm data/x'", "rm -rf .", "git clean -fdx", "eval rm data/x", "ls\nrm data/x", "sudo -u root rm data/x"):
            assert ev(*bash(cmd), st).decision == "deny", cmd

    def test_everything_target_denied_with_certainty(self):
        d = ev(*bash("rm -rf /"), state_with(FORBID_DELETE_DATA))
        assert d.decision == "deny" and not d.uncertain

    def test_inactive_rule_is_ignored(self):
        r = Rule.from_dict({**FORBID_DELETE_DATA.as_dict(), "active": False})
        assert ev(*bash("rm -rf data/x"), state_with(r)).decision == "allow"

    def test_branch_scoped_forbid(self):
        r = Rule(id="R-p", kind="forbid", action="push", scope=Scope(branches=["main", "*/main"]), text="no push to main")
        st = state_with(r)
        assert ev(*bash("git push origin main"), st).decision == "deny"
        assert ev(*bash("git push -f origin main"), st).decision == "deny"     # force_push is a child of push
        assert ev(*bash("git push origin feature"), st).decision == "allow"

    def test_no_rules_allows_everything(self):
        assert ev(*bash("rm -rf /"), default_state()).decision == "allow"


# ---------------------------------------------------------------- confirm

class TestConfirm:
    def test_full_cycle(self):
        st = state_with(CONFIRM_PUSH)
        c = cfg()
        # 1. first attempt: deny + pending
        d = ev(*bash("git push origin main"), st, c, tool_use_id="tu-1")
        assert d.decision == "deny" and d.rule_id == "R-cnf001"
        assert "confirmation" in d.reason
        p = st["pending_confirm"]
        assert p["rule_id"] == "R-cnf001" and p["tool_use_id"] == "tu-1" and p["effect"]["action"] == "push"
        assert p["tool_name"] == "Bash" and p["tool_input"] == {"command": "git push origin main"}
        # 2. user says yes
        res = handle_prompt("yes", st, c)
        assert res["granted"]["rule_id"] == "R-cnf001" and st["pending_confirm"] is None
        grant = st["confirmations"]["R-cnf001"]
        assert grant["consumed"] is False and grant["mode"] == "once"
        # 3. retry: allowed, grant consumed
        d = ev(*bash("git push origin main"), st, c, tool_use_id="tu-2")
        assert d.decision == "allow"
        assert st["confirmations"]["R-cnf001"]["consumed"] is True
        assert st["confirmations"]["R-cnf001"]["consumed_by"] == "tu-2"
        # 4. next push: denied again
        d = ev(*bash("git push origin main"), st, c, tool_use_id="tu-3")
        assert d.decision == "deny" and st["pending_confirm"]["tool_use_id"] == "tu-3"

    def test_confirm_via_ask(self):
        d = ev(*bash("git push"), state_with(CONFIRM_PUSH), cfg(confirm_via="ask"))
        assert d.decision == "ask"

    def test_user_declines(self):
        st = state_with(CONFIRM_PUSH)
        ev(*bash("git push"), st)
        res = handle_prompt("no, don't", st, cfg())
        assert res["denied"]["rule_id"] == "R-cnf001" and res["granted"] is None
        assert st["pending_confirm"] is None and "R-cnf001" not in st["confirmations"]
        assert ev(*bash("git push"), st).decision == "deny"

    @pytest.mark.parametrize("answer", ["yes", "Yes.", "y", "ok", "go ahead", "yes, push it", "Confirmed", "sure", "do it", "proceed!"])
    def test_affirmations(self, answer):
        st = state_with(CONFIRM_PUSH)
        ev(*bash("git push"), st)
        assert handle_prompt(answer, st, cfg())["granted"]

    @pytest.mark.parametrize("answer", ["Fix the bug first", "What does this do?", "yesterday's build broke", "You should fix it", "nothing else"])
    def test_non_answers_leave_pending(self, answer):
        st = state_with(CONFIRM_PUSH)
        ev(*bash("git push"), st)
        res = handle_prompt(answer, st, cfg())
        assert res["granted"] is None and res["denied"] is None and st["pending_confirm"] is not None

    def test_grant_mode_always_is_not_consumed(self):
        st = state_with(CONFIRM_PUSH, confirmations={"R-cnf001": {"granted_at": time.time(), "consumed": False, "mode": "always"}})
        assert ev(*bash("git push"), st).decision == "allow"
        assert ev(*bash("git push"), st).decision == "allow"
        assert st["confirmations"]["R-cnf001"]["consumed"] is False

    def test_no_prompt_without_pending(self):
        st = state_with(CONFIRM_PUSH)
        res = handle_prompt("yes", st, cfg())
        assert res["granted"] is None and st["confirmations"] == {}

    def test_uncertain_confirm_uses_opaque_policy(self):
        r = Rule(id="R-c2", kind="confirm", action="delete", scope=Scope(paths=["data/**"]), text="ask before deleting data")
        st = state_with(r)
        d = ev(*bash("rm -rf $DIR"), st, cfg(opaque_policy="ask"))
        assert d.decision == "ask" and d.uncertain and st["pending_confirm"] is None

    def test_require_before_user_confirm_behaves_like_confirm(self):
        r = Rule(id="R-uc", kind="require_before", action="push", prerequisite="user_confirm", text="confirm before push")
        st = state_with(r)
        d = ev(*bash("git push"), st)
        assert d.decision == "deny" and st["pending_confirm"]["rule_id"] == "R-uc"


# ---------------------------------------------------------------- require_before tests_pass

class TestRequireBefore:
    def test_cycle(self):
        st = state_with(REQ_TESTS_PUSH)
        d = ev(*bash("git push origin main"), st)
        assert d.decision == "deny" and "No passing test run" in d.reason
        # tests pass
        changed = observe_post("Bash", {"command": "pytest -q"}, {"stdout": "12 passed in 0.3s", "stderr": ""}, st, CWD, ROOT)
        assert "tests_passed_at" in changed and st["tests_passed_at"]
        assert ev(*bash("git push origin main"), st).decision == "allow"
        # an edit makes the tests stale
        time.sleep(0.002)
        changed = observe_post("Edit", {"file_path": "/proj/src/a.py", "old_string": "a", "new_string": "b"}, {}, st, CWD, ROOT)
        assert "last_edit_at" in changed and st["last_edit_at"] > st["tests_passed_at"]
        d = ev(*bash("git push origin main"), st)
        assert d.decision == "deny" and "since the last edit" in d.reason
        # tests pass again -> allowed
        time.sleep(0.002)
        observe_post("Bash", {"command": "python -m pytest"}, {"stdout": "12 passed", "stderr": ""}, st, CWD, ROOT)
        assert ev(*bash("git push origin main"), st).decision == "allow"

    def test_failed_tests_do_not_unlock(self):
        st = state_with(REQ_TESTS_PUSH)
        observe_post("Bash", {"command": "pytest"}, {"stdout": "1 failed, 3 passed", "stderr": ""}, st, CWD, ROOT)
        assert st["tests_passed_at"] is None and st["tests_failed_at"]
        assert ev(*bash("git push"), st).decision == "deny"

    def test_interrupted_run_does_not_unlock(self):
        st = state_with(REQ_TESTS_PUSH)
        observe_post("Bash", {"command": "pytest"}, {"stdout": "3 passed", "stderr": "", "interrupted": True}, st, CWD, ROOT)
        assert st["tests_passed_at"] is None

    def test_non_test_command_does_not_unlock(self):
        st = state_with(REQ_TESTS_PUSH)
        observe_post("Bash", {"command": "echo '5 passed'"}, {"stdout": "5 passed", "stderr": ""}, st, CWD, ROOT)
        assert st["tests_passed_at"] is None

    def test_string_tool_response(self):
        st = state_with(REQ_TESTS_PUSH)
        observe_post("Bash", {"command": "pytest"}, "5 passed in 1s", st, CWD, ROOT)
        assert st["tests_passed_at"]

    def test_bash_edit_marks_last_edit(self):
        st = default_state()
        for cmd in ("rm x", "echo hi > f", "sed -i s/a/b/ f", "mv a b", "git checkout -- a"):
            st["last_edit_at"] = None
            observe_post("Bash", {"command": cmd}, {"stdout": ""}, st, CWD, ROOT)
            assert st["last_edit_at"], cmd
        st["last_edit_at"] = None
        observe_post("Bash", {"command": "ls"}, {"stdout": ""}, st, CWD, ROOT)
        assert st["last_edit_at"] is None

    def test_commit_marks_last_commit(self):
        st = default_state()
        observe_post("Bash", {"command": "git commit -m x"}, {"stdout": ""}, st, CWD, ROOT)
        assert st["last_commit_at"]

    def test_same_tick_edit_is_stale(self):
        st = state_with(REQ_TESTS_PUSH, tests_passed_at=100.0, last_edit_at=100.0)
        assert ev(*bash("git push"), st).decision == "deny"
        st["tests_passed_at"] = 100.001
        assert ev(*bash("git push"), st).decision == "allow"

    def test_other_actions_unaffected(self):
        st = state_with(REQ_TESTS_PUSH)
        assert ev(*bash("git commit -m x"), st).decision == "allow"
        assert ev("Edit", {"file_path": "/proj/a.py"}, st).decision == "allow"


# ---------------------------------------------------------------- freeze

class TestFreeze:
    def test_allowed_before_task_started(self):
        st = state_with(FREEZE_TESTS, task_started=False)
        assert ev("Edit", {"file_path": "/proj/tests/test_a.py"}, st).decision == "allow"
        assert ev(*bash("rm tests/test_a.py"), st).decision == "allow"

    def test_denied_after_task_started(self):
        st = state_with(FREEZE_TESTS, task_started=True)
        d = ev("Edit", {"file_path": "/proj/tests/test_a.py"}, st)
        assert d.decision == "deny" and d.rule_id == "R-frz001" and "frozen" in d.reason
        assert ev("Write", {"file_path": "/proj/tests/test_new.py", "content": ""}, st).decision == "deny"
        assert ev(*bash("rm -rf tests"), st).decision == "deny"
        assert ev(*bash("sed -i 's/a/b/' tests/test_a.py"), st).decision == "deny"

    def test_out_of_scope_after_start(self):
        st = state_with(FREEZE_TESTS, task_started=True)
        assert ev("Edit", {"file_path": "/proj/src/a.py"}, st).decision == "allow"
        assert ev("Read", {"file_path": "/proj/tests/test_a.py"}, st).decision == "allow"


# ---------------------------------------------------------------- opaque policy

class TestOpaquePolicy:
    @pytest.mark.parametrize("cmd", ["rm -rf $DIR", "ls | xargs rm", "cat list | xargs -n1 rm -f", "python -c 'import shutil; shutil.rmtree(d)'"])
    def test_uncertain_effect_follows_policy(self, cmd):
        st = state_with(FORBID_DELETE_DATA)
        for pol in ("ask", "deny"):
            d = ev(*bash(cmd), st, cfg(opaque_policy=pol))
            assert d.decision == pol and d.uncertain and d.rule_id == "R-del001", (cmd, pol)
            assert "could violate" in d.reason
        d = ev(*bash(cmd), st, cfg(opaque_policy="allow"))
        assert d.decision == "allow" and not d.uncertain and d.matched == []

    def test_opaque_script_outside_scope_is_allowed_by_delete_rule(self):
        # a script's path is known; it only matches `run`-family rules, so a delete rule on data/** does not fire
        assert ev(*bash("./cleanup.sh"), state_with(FORBID_DELETE_DATA), cfg(opaque_policy="deny")).decision == "allow"

    def test_opaque_script_in_scope_is_uncertain(self):
        # exec_opaque kind "opaque" is matched by path scopes, and is never certain
        r = Rule(id="R-run", kind="forbid", action="run", scope=Scope(paths=["scripts/**"]), text="never run scripts")
        d = ev(*bash("./scripts/deploy.sh"), state_with(r), cfg(opaque_policy="ask"))
        assert d.decision == "ask" and d.uncertain

    def test_mcp_tool_is_opaque_run(self):
        r = Rule(id="R-run", kind="forbid", action="run", text="never run anything")
        d = ev("mcp__github__create_pr", {"title": "x"}, state_with(r), cfg(opaque_policy="deny"))
        assert d.decision == "deny" and d.uncertain

    def test_unknown_target_does_not_trigger_universal_forbid_uncertainty(self):
        # universal scope -> matches True regardless of target; certainty comes from the effect
        r = Rule(id="R-d", kind="forbid", action="delete", text="never delete")
        d = ev(*bash("rm -rf $DIR"), state_with(r), cfg(opaque_policy="allow"))
        assert d.decision == "deny" and not d.uncertain


# ---------------------------------------------------------------- priority / hierarchy / multi-rule

class TestPriorityAndHierarchy:
    def test_deny_beats_ask_beats_allow(self):
        forbid = Rule(id="R-f", kind="forbid", action="force_push", text="never force push")
        confirm = Rule(id="R-c", kind="confirm", action="push", text="ask before push")
        st = state_with(confirm, forbid)
        d = ev(*bash("git push -f origin main"), st, cfg(confirm_via="ask"))
        assert d.decision == "deny" and d.rule_id == "R-f"
        assert {m["rule_id"] for m in d.matched} == {"R-c", "R-f"}
        d = ev(*bash("git push origin main"), st, cfg(confirm_via="ask"))
        assert d.decision == "ask" and d.rule_id == "R-c"

    def test_ask_beats_allow_from_opaque(self):
        st = state_with(FORBID_DELETE_DATA)
        d = ev(*bash("ls && rm -rf $DIR"), st, cfg(opaque_policy="ask"))
        assert d.decision == "ask"

    def test_certain_deny_beats_uncertain_ask_in_same_command(self):
        st = state_with(FORBID_DELETE_DATA)
        d = ev(*bash("rm -rf $DIR && rm -rf data/x"), st, cfg(opaque_policy="ask"))
        assert d.decision == "deny" and not d.uncertain

    def test_modify_rule_covers_edit_write_delete(self):
        r = Rule(id="R-m", kind="forbid", action="modify", scope=Scope(paths=["src/legacy/**"]), text="hands off legacy")
        st = state_with(r)
        assert ev("Edit", {"file_path": "/proj/src/legacy/a.py"}, st).decision == "deny"
        assert ev("MultiEdit", {"file_path": "/proj/src/legacy/a.py"}, st).decision == "deny"
        assert ev("Write", {"file_path": "/proj/src/legacy/new.py", "content": ""}, st).decision == "deny"
        assert ev("NotebookEdit", {"notebook_path": "/proj/src/legacy/n.ipynb"}, st).decision == "deny"
        assert ev(*bash("rm src/legacy/a.py"), st).decision == "deny"
        assert ev(*bash("mv src/legacy/a.py /tmp/"), st).decision == "deny"
        assert ev(*bash("git checkout -- src/legacy/a.py"), st).decision in ("deny", "ask")
        assert ev("Read", {"file_path": "/proj/src/legacy/a.py"}, st).decision == "allow"
        assert ev("Edit", {"file_path": "/proj/src/new/a.py"}, st).decision == "allow"

    def test_destructive_rule(self):
        r = Rule(id="R-x", kind="forbid", action="destructive", text="nothing destructive")
        st = state_with(r)
        for cmd in ("rm x", "git reset --hard", "git push -f", "git rebase main", "terraform destroy"):
            assert ev(*bash(cmd), st).decision == "deny", cmd
        for cmd in ("git push", "git commit -m x", "ls"):
            assert ev(*bash(cmd), st).decision == "allow", cmd
        assert ev("Edit", {"file_path": "/proj/a.py"}, st).decision == "allow"

    def test_relative_path_in_tool_input(self):
        r = Rule(id="R-m", kind="forbid", action="modify", scope=Scope(paths=["tests/**"]), text="no")
        assert ev("Edit", {"file_path": "tests/test_a.py"}, state_with(r)).decision == "deny"


# ---------------------------------------------------------------- register_rules / handle_prompt / context_block

class TestRegisterAndPrompt:
    def test_register_dedups(self):
        st = default_state()
        a = Rule(id="R-1", kind="forbid", action="push")
        b = Rule(id="R-2", kind="forbid", action="commit")
        assert [r.id for r in register_rules(st, [a, b, a])] == ["R-1", "R-2"]
        assert register_rules(st, [a]) == []
        assert [r["id"] for r in st["rules"]] == ["R-1", "R-2"]

    def test_handle_prompt_extracts_and_counts_turns(self):
        st = default_state()
        res = handle_prompt("Never force push. Fix the bug.", st, cfg())
        assert st["turn"] == 1
        assert [(r.kind, r.action) for r in res["added"]] == [("forbid", "force_push")]
        assert res["added"][0].source == "user" and res["added"][0].created_turn == 1
        res = handle_prompt("Never force push.", st, cfg())
        assert st["turn"] == 2 and res["added"] == []

    def test_handle_prompt_drop(self):
        st = default_state()
        handle_prompt("Never force push", st, cfg())
        rid = st["rules"][0]["id"]
        res = handle_prompt(f"holdfast: drop {rid}", st, cfg())
        assert res["dropped"] == [rid] and st["rules"][0]["active"] is False
        assert ev(*bash("git push -f"), st).decision == "allow"
        assert context_block(st) == ""

    def test_handle_prompt_drop_case_insensitive_and_unknown(self):
        st = default_state()
        handle_prompt("Never force push", st, cfg())
        rid = st["rules"][0]["id"]
        assert handle_prompt(f"HOLDFAST drop rule {rid.upper()}", st, cfg())["dropped"] == [rid.upper()]
        assert handle_prompt("holdfast: drop R-000000", st, cfg())["dropped"] == []

    def test_handle_prompt_list(self):
        st = default_state()
        assert handle_prompt("holdfast: list", st, cfg())["listed"] is True
        assert handle_prompt("holdfast status", st, cfg())["listed"] is True
        assert handle_prompt("hello", st, cfg())["listed"] is False

    def test_context_block(self):
        st = state_with(FORBID_DELETE_DATA, CONFIRM_PUSH)
        ctx = context_block(st)
        assert ctx.startswith("Holdfast constraint guard is active.")
        assert "R-del001: never delete on paths data/**  (from: \"never delete data\")" in ctx
        assert "R-cnf001" in ctx and "survive compaction" in ctx
        assert context_block(st, "Custom header").startswith("Custom header.")
        assert context_block(default_state()) == ""

    def test_context_block_skips_inactive(self):
        r = Rule.from_dict({**CONFIRM_PUSH.as_dict(), "active": False})
        assert context_block(state_with(FORBID_DELETE_DATA, r)).count("R-cnf001") == 0


class TestDecisionDict:
    def test_as_dict(self):
        d = ev(*bash("rm -rf data/x"), state_with(FORBID_DELETE_DATA))
        dd = d.as_dict()
        assert dd["decision"] == "deny" and dd["rule_id"] == "R-del001" and dd["uncertain"] is False
        assert dd["effects"][0] == {"action": "delete", "target": "data/x", "kind": "path", "detail": "rm -rf data/x", "certain": True}
        assert dd["matched"][0]["effect"]["target"] == "data/x"
