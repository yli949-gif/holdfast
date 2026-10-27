"""Tests for holdfast.engine: rule semantics, confirmations, order, scope, opaque, aggregation."""
from types import SimpleNamespace

import pytest

from holdfast.classify import Action
from holdfast.engine import Decision, evaluate, glob_match
from holdfast.rules import Rule

CWD = "/proj"


def st(predicates=None, confirmations=None):
    return SimpleNamespace(predicates=dict(predicates or {}), confirmations=list(confirmations or []))


def R(**kw):
    kw.setdefault("id", "r1")
    kw.setdefault("source", {"turn": 3, "text": "some instruction", "via": "pattern"})
    return Rule(**kw)


def delete(*paths, conf=0.95, opaque=False):
    return Action("delete", [f"{CWD}/{p}" if not p.startswith("/") else p for p in paths], conf, opaque, "rm -rf " + " ".join(paths))


def edit(path, conf=1.0):
    return Action("edit_path", [f"{CWD}/{path}"], conf, False, f"Edit {path}")


# --- glob matching ---------------------------------------------------------------------

@pytest.mark.parametrize("pat,path,exp", [
    ("tests/**", "/proj/tests/a.py", True), ("tests/**", "/proj/tests/sub/b.py", True),
    ("tests/**", "/proj/tests", True), ("tests/", "/proj/tests/a.py", True), ("tests", "/proj/tests/a.py", True),
    ("tests/**", "/proj/src/a.py", False), ("tests/**", "/proj/testsx/a.py", False),
    ("src/**", "/tmp/x", False), ("*.py", "/proj/src/a.py", True), ("*.md", "/proj/src/a.py", False),
    ("src/foo", "/proj/src/foo/bar.py", True), ("src/foo", "/proj/src/foobar.py", False),
    ("**/*.test.js", "/proj/a/b/c.test.js", True), ("config/production*", "/proj/config/production.yaml", True),
    ("/etc/**", "/etc/hosts", True), ("tests/**", "/proj", True), ("tests/**", "/proj/.", True),
])
def test_glob_match(pat, path, exp):
    assert glob_match(pat, path, CWD) is exp


# --- forbid --------------------------------------------------------------------------------

def test_forbid_denies_with_actionable_reason():
    d = evaluate([R(kind="forbid", action="delete")], st(), [delete("build")], CWD)
    assert d.decision == "deny" and d.rule_id == "r1"
    assert "r1" in d.reason and "build" in d.reason and "/proj/build" not in d.reason
    assert "turn 3" in d.reason


def test_forbid_other_action_allows():
    d = evaluate([R(kind="forbid", action="delete")], st(), [Action("push", [], 1.0)], CWD)
    assert d.decision == "allow" and d.rule_id is None


def test_forbid_with_target_only_matches_inside():
    rules = [R(kind="forbid", action="edit_path", target="tests/**")]
    assert evaluate(rules, st(), [edit("tests/test_a.py")], CWD).decision == "deny"
    assert evaluate(rules, st(), [edit("src/a.py")], CWD).decision == "allow"
    # deleting the parent dir of the target also matches
    assert evaluate([R(kind="forbid", action="delete", target="tests/**")], st(), [delete(".")], CWD).decision == "deny"


def test_edit_path_rules_cover_deletes_on_matching_paths():
    rules = [R(kind="forbid", action="edit_path", target="tests/**")]
    assert evaluate(rules, st(), [delete("tests/test_a.py")], CWD).decision == "deny"
    assert evaluate(rules, st(), [delete("src/a.py")], CWD).decision == "allow"
    # but an untargeted edit_path rule does not swallow every delete
    assert evaluate([R(kind="forbid", action="edit_path")], st(), [delete("x")], CWD).decision == "allow"


def test_scope_covers_deletes_outside_target():
    rules = [R(kind="scope", action="edit_path", target="src/**")]
    assert evaluate(rules, st(), [delete("tests/a.py")], CWD).decision == "deny"
    assert evaluate(rules, st(), [delete("src/a.py")], CWD).decision == "allow"


def test_forbid_any():
    assert evaluate([R(kind="forbid", action="any")], st(), [Action("push", [], 1.0)], CWD).decision == "deny"


def test_inactive_rule_ignored():
    assert evaluate([R(kind="forbid", action="delete", active=False)], st(), [delete("x")], CWD).decision == "allow"


def test_run_cmd_target_matches_command_name():
    rules = [R(kind="forbid", action="run_cmd", target="docker")]
    assert evaluate(rules, st(), [Action("run_cmd", ["docker", "docker ps"], 1.0)], CWD).decision == "deny"
    assert evaluate(rules, st(), [Action("run_cmd", ["ls", "ls -la"], 1.0)], CWD).decision == "allow"
    rules = [R(kind="forbid", action="run_cmd", target="kubectl delete*")]
    assert evaluate(rules, st(), [Action("run_cmd", ["kubectl", "kubectl delete pod x"], 1.0)], CWD).decision == "deny"
    assert evaluate(rules, st(), [Action("run_cmd", ["kubectl", "kubectl get pod x"], 1.0)], CWD).decision == "allow"


def test_push_target_matches_branch():
    rules = [R(kind="forbid", action="push", target="main")]
    assert evaluate(rules, st(), [Action("push", ["main", "origin/main"], 1.0)], CWD).decision == "deny"
    assert evaluate(rules, st(), [Action("push", ["feature", "origin/feature"], 1.0)], CWD).decision == "allow"


def test_touch_prod_weak_needs_target():
    weak = Action("touch_prod", ["main"], 0.5)
    assert evaluate([R(kind="forbid", action="touch_prod")], st(), [weak], CWD).decision == "allow"
    assert evaluate([R(kind="forbid", action="touch_prod", target="main")], st(), [weak], CWD).decision == "deny"
    strong = Action("touch_prod", ["--prod"], 0.9)
    assert evaluate([R(kind="forbid", action="touch_prod")], st(), [strong], CWD).decision == "deny"


# --- confirm ---------------------------------------------------------------------------------

def test_confirm_denies_then_allows_once_then_denies():
    rules = [R(id="r2", kind="confirm", action="delete")]
    s = st()
    d = evaluate(rules, s, [delete("build")], CWD)
    assert d.decision == "deny" and "confirm" in d.reason.lower() and "Ask the user" in d.reason
    s.confirmations.append({"action": "delete", "target": None, "granted_turn": 5, "remaining": 1})
    d = evaluate(rules, s, [delete("build")], CWD)
    assert d.decision == "allow" and d.rule_id == "r2" and s.confirmations[0]["remaining"] == 0
    assert evaluate(rules, s, [delete("build")], CWD).decision == "deny"


def test_confirm_not_consumed_when_call_denied_by_other_rule():
    rules = [R(id="r1", kind="confirm", action="delete"), R(id="r2", kind="forbid", action="push")]
    s = st(confirmations=[{"action": "delete", "target": None, "granted_turn": 5, "remaining": 1}])
    d = evaluate(rules, s, [delete("build"), Action("push", [], 1.0)], CWD)
    assert d.decision == "deny" and d.rule_id == "r2"
    assert s.confirmations[0]["remaining"] == 1


def test_confirm_once_false_never_consumed():
    rules = [R(kind="confirm", action="delete", once=False)]
    s = st(confirmations=[{"action": "delete", "target": None, "granted_turn": 5, "remaining": 1}])
    evaluate(rules, s, [delete("a")], CWD)
    assert evaluate(rules, s, [delete("b")], CWD).decision == "allow"


def test_confirm_scoped_target():
    rules = [R(kind="confirm", action="delete")]
    s = st(confirmations=[{"action": "delete", "target": "build/**", "granted_turn": 5, "remaining": 1}])
    assert evaluate(rules, s, [delete("src")], CWD).decision == "deny"
    assert evaluate(rules, s, [delete("build")], CWD).decision == "allow"


def test_confirm_any_action_token_matches():
    rules = [R(kind="confirm", action="delete")]
    s = st(confirmations=[{"action": "any", "target": None, "granted_turn": 5, "remaining": 1}])
    assert evaluate(rules, s, [delete("x")], CWD).decision == "allow"


def test_one_confirmation_consumed_once_per_call():
    rules = [R(kind="confirm", action="delete")]
    s = st(confirmations=[{"action": "delete", "target": None, "granted_turn": 5, "remaining": 1}])
    d = evaluate(rules, s, [delete("a"), delete("b")], CWD)
    assert d.decision == "allow" and s.confirmations[0]["remaining"] == 0


# --- order -------------------------------------------------------------------------------------

def test_order_tests_passed():
    rules = [R(kind="order", action="push", requires="tests_passed")]
    d = evaluate(rules, st({"tests_passed": False}), [Action("push", [], 1.0)], CWD)
    assert d.decision == "deny" and "test" in d.reason.lower()
    assert evaluate(rules, st({"tests_passed": True}), [Action("push", [], 1.0)], CWD).decision == "allow"
    assert evaluate(rules, st(), [Action("push", [], 1.0)], CWD).decision == "deny"


def test_order_user_confirmed_uses_confirmations():
    rules = [R(kind="order", action="delete", requires="user_confirmed")]
    assert evaluate(rules, st(), [delete("x")], CWD).decision == "deny"
    s = st(confirmations=[{"action": "delete", "target": None, "granted_turn": 1, "remaining": 1}])
    assert evaluate(rules, s, [delete("x")], CWD).decision == "allow"


# --- scope ----------------------------------------------------------------------------------------

def test_scope_only_under_target():
    rules = [R(kind="scope", action="edit_path", target="src/**")]
    assert evaluate(rules, st(), [edit("src/a.py")], CWD).decision == "allow"
    d = evaluate(rules, st(), [edit("tests/a.py")], CWD)
    assert d.decision == "deny" and "src/**" in d.reason
    assert evaluate(rules, st(), [Action("edit_path", ["/tmp/x"], 1.0)], CWD).decision == "deny"
    # scope does not restrict non-edit actions
    assert evaluate(rules, st(), [Action("push", [], 1.0)], CWD).decision == "allow"


# --- opaque ----------------------------------------------------------------------------------------

def test_opaque_asks_when_rule_could_apply():
    rules = [R(kind="confirm", action="delete")]
    d = evaluate(rules, st(), [delete("x", conf=0.5, opaque=True)], CWD)
    assert d.decision == "ask" and "r1" in d.reason and "cannot inspect" in d.reason


def test_opaque_allows_when_no_rule_applies():
    rules = [R(kind="forbid", action="push")]
    assert evaluate(rules, st(), [delete("x", conf=0.5, opaque=True)], CWD).decision == "allow"


def test_opaque_with_confirmation_allows():
    rules = [R(kind="confirm", action="delete")]
    s = st(confirmations=[{"action": "delete", "target": None, "granted_turn": 1, "remaining": 1}])
    assert evaluate(rules, s, [delete("x", conf=0.5, opaque=True)], CWD).decision == "allow"


# --- aggregation -------------------------------------------------------------------------------------

def test_most_restrictive_wins():
    rules = [R(id="r1", kind="confirm", action="delete"), R(id="r2", kind="forbid", action="push")]
    acts = [delete("x", conf=0.5, opaque=True), Action("push", [], 1.0), Action("commit", [], 1.0)]
    d = evaluate(rules, st(), acts, CWD)
    assert d.decision == "deny" and d.rule_id == "r2"
    d = evaluate(rules, st(), acts[:1] + acts[2:], CWD)
    assert d.decision == "ask"


def test_no_rules_or_actions():
    assert evaluate([], st(), [delete("x")], CWD).decision == "allow"
    assert evaluate([R(kind="forbid", action="delete")], st(), [], CWD).decision == "allow"


def test_decision_fields():
    d = Decision("allow", "", None, None)
    assert d.decision == "allow" and d.rule_id is None and d.action is None


def test_reason_paths_relative_only_when_under_cwd():
    rules = [R(kind="forbid", action="delete")]
    d = evaluate(rules, st(), [Action("delete", ["/etc/hosts"], 0.95)], CWD)
    assert "/etc/hosts" in d.reason
