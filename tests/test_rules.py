"""Tests for holdfast.rules: schema, validation, (de)serialisation."""
import pytest

from holdfast.rules import (
    ACTIONS,
    KINDS,
    Rule,
    RuleError,
    next_rule_id,
    rule_from_dict,
    rules_from_list,
)


def test_actions_and_kinds_vocabulary():
    assert {"delete", "force_push", "push", "commit", "edit_path", "run_cmd",
            "network", "install", "touch_prod", "any"} == set(ACTIONS)
    assert {"forbid", "confirm", "order", "scope"} == set(KINDS)


def test_roundtrip_dict():
    r = Rule(id="r1", kind="confirm", action="delete",
             source={"turn": 3, "text": "ask before deleting", "via": "pattern"})
    d = r.to_dict()
    assert d["id"] == "r1" and d["kind"] == "confirm" and d["action"] == "delete"
    assert d["once"] is True and d["active"] is True
    assert d["created_at"]
    r2 = rule_from_dict(d)
    assert r2 == r


def test_defaults():
    r = Rule(id="r9", kind="forbid", action="force_push")
    assert r.target is None and r.requires is None and r.once is True and r.active is True
    assert r.source == {}


def test_invalid_kind_and_action():
    with pytest.raises(RuleError):
        Rule(id="r1", kind="ban", action="delete")
    with pytest.raises(RuleError):
        Rule(id="r1", kind="forbid", action="explode")


def test_order_requires_predicate():
    with pytest.raises(RuleError):
        Rule(id="r1", kind="order", action="push")
    with pytest.raises(RuleError):
        Rule(id="r1", kind="order", action="push", requires="moon_phase")
    Rule(id="r1", kind="order", action="push", requires="tests_passed")


def test_scope_requires_target_and_edit_path():
    with pytest.raises(RuleError):
        Rule(id="r1", kind="scope", action="edit_path")
    with pytest.raises(RuleError):
        Rule(id="r1", kind="scope", action="delete", target="src/**")
    Rule(id="r1", kind="scope", action="edit_path", target="src/**")


def test_run_cmd_requires_target():
    with pytest.raises(RuleError):
        Rule(id="r1", kind="forbid", action="run_cmd")
    Rule(id="r1", kind="forbid", action="run_cmd", target="docker")


def test_from_dict_ignores_unknown_keys_and_missing_id():
    d = {"kind": "forbid", "action": "delete", "extra": 1}
    r = rule_from_dict(d, default_id="r4")
    assert r.id == "r4"
    with pytest.raises(RuleError):
        rule_from_dict({"kind": "forbid"})


def test_next_rule_id():
    rs = rules_from_list([{"id": "r1", "kind": "forbid", "action": "delete"},
                          {"id": "r7", "kind": "forbid", "action": "push"}])
    assert next_rule_id(rs) == "r8"
    assert next_rule_id([]) == "r1"


def test_describe_is_short_and_human():
    r = Rule(id="r2", kind="confirm", action="delete", source={"turn": 3, "text": "ask me before deleting"})
    s = r.describe()
    assert "r2" in s and "confirm" in s and "delete" in s
    r = Rule(id="r3", kind="scope", action="edit_path", target="src/**")
    assert "src/**" in r.describe()
