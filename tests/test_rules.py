"""Tests for holdfast.rules: Scope matching, action hierarchy, Rule (de)serialisation and description."""
import pytest

from holdfast.effects import EVERYTHING, UNKNOWN, Effect, action_matches
from holdfast.rules import Rule, Scope, _path_in_scope, make_id


# ---------------------------------------------------------------- _path_in_scope

class TestPathInScope:
    @pytest.mark.parametrize("target,glob", [
        ("data/x.txt", "data/**"), ("data/a/b/c.bin", "data/**"), ("data", "data/**"), ("data/", "data/**"),
        ("tests/test_a.py", "tests/**"), ("tests/unit/test_a.py", "tests/**"),
        ("src/test_a.py", "**/test_*.py"), ("test_a.py", "**/test_*.py"), ("a/b/c/test_x.py", "**/test_*.py"),
        ("a/x/y/b.py", "a/**/b.py"), ("a/b.py", "a/**/b.py"),
        ("foo.lock", "*.lock"), ("sub/foo.lock", "*.lock"),
        ("config.yaml", "config.yaml"), ("src/legacy/x.py", "src/legacy/**"),
        (".env", ".env*"), (".env.local", ".env*"),
    ])
    def test_in_scope(self, target, glob):
        assert _path_in_scope(target, glob)

    @pytest.mark.parametrize("target,glob", [
        ("src/x.py", "data/**"), ("datastore/x", "data/**"), ("dat", "data/**"), ("mydata/x", "data/**"),
        ("src/a.py", "**/test_*.py"), ("tests_backup/x", "tests/**"), ("/etc/passwd", "data/**"),
        ("config.yml", "config.yaml"), ("a/b/x.py", "a/**/b.py"),
    ])
    def test_out_of_scope(self, target, glob):
        assert not _path_in_scope(target, glob)

    def test_parent_directory_deletion_covers_scope(self):
        # deleting "." or a parent dir of the protected subtree is in scope
        assert _path_in_scope(".", "data/**")
        assert _path_in_scope("", "data/**")
        assert _path_in_scope("src", "src/legacy/**")
        assert _path_in_scope("src/legacy", "src/legacy/**")
        assert _path_in_scope("a", "a/**/b.py")

    def test_parent_of_unrelated_dir_is_not_in_scope(self):
        assert not _path_in_scope("src", "data/**")
        assert not _path_in_scope("src/legacy2", "src/legacy/**")

    def test_dot_with_pure_wildcard_glob(self):
        # "*.lock" has no directory prefix; "." is not treated as a parent of it
        assert not _path_in_scope(".", "*.lock")

    def test_absolute_scope(self):
        assert _path_in_scope("/etc/passwd", "/etc/**")


# ---------------------------------------------------------------- Scope.matches

class TestScopeMatches:
    def test_universal_matches_anything(self):
        s = Scope()
        assert s.is_universal()
        assert s.matches(Effect("delete", UNKNOWN)) is True
        assert s.matches(Effect("push", "origin/main", kind="branch")) is True
        assert s.matches(Effect("exec_opaque", "x", kind="opaque")) is True

    def test_paths(self):
        s = Scope(paths=["data/**"])
        assert s.matches(Effect("delete", "data/x")) is True
        assert s.matches(Effect("delete", "src/x")) is False
        assert s.matches(Effect("delete", EVERYTHING)) is True
        assert s.matches(Effect("delete", UNKNOWN)) is None

    def test_paths_apply_to_opaque_kind(self):
        s = Scope(paths=["scripts/**"])
        assert s.matches(Effect("exec_opaque", "scripts/run.sh", kind="opaque")) is True
        assert s.matches(Effect("exec_opaque", "other.sh", kind="opaque")) is False

    def test_paths_do_not_match_branch_effects(self):
        s = Scope(paths=["data/**"])
        assert s.matches(Effect("delete", "branch/feat", kind="branch")) is False
        assert s.matches(Effect("push", "origin/main", kind="branch")) is False

    def test_paths_with_unknown_non_path_kind(self):
        s = Scope(paths=["data/**"])
        assert s.matches(Effect("network", UNKNOWN, kind="url")) is None

    def test_branches(self):
        s = Scope(branches=["main", "*/main"])
        assert s.matches(Effect("push", "origin/main", kind="branch")) is True
        assert s.matches(Effect("push", "upstream/main", kind="branch")) is True
        assert s.matches(Effect("push", "origin/feat", kind="branch")) is False
        assert s.matches(Effect("force_push", "origin/HEAD", kind="branch")) is None   # unresolved ref: could be main

    def test_branches_only_match_branch_kind(self):
        s = Scope(branches=["main"])
        assert s.matches(Effect("delete", "main", kind="path")) is False
        assert s.matches(Effect("delete", UNKNOWN, kind="path")) is None

    def test_branch_glob_with_single_name(self):
        s = Scope(branches=["release/*"])
        assert s.matches(Effect("push", "origin/release/1.0", kind="branch")) is True

    def test_pattern(self):
        s = Scope(pattern=r"prod")
        assert s.matches(Effect("deploy", "terraform apply -var env=PROD", kind="command")) is True
        assert s.matches(Effect("deploy", "terraform apply staging", kind="command")) is False
        assert s.matches(Effect("network", "https://api.prod.example.com", kind="url")) is True
        assert s.matches(Effect("deploy", UNKNOWN, kind="command")) is None

    def test_command_matches_detail(self):
        s = Scope(command=r"\bprod(uction)?\b")
        assert s.matches(Effect("run", "x", kind="command", detail="make deploy ENV=production")) is True
        assert s.matches(Effect("run", "x", kind="command", detail="make build")) is False

    def test_command_falls_back_to_target(self):
        s = Scope(command=r"flushall")
        assert s.matches(Effect("run", "redis-cli FLUSHALL", kind="command")) is True


# ---------------------------------------------------------------- action hierarchy

class TestActionMatches:
    @pytest.mark.parametrize("rule,effect", [
        ("modify", "edit"), ("modify", "write"), ("modify", "delete"), ("modify", "discard"), ("modify", "move"),
        ("delete", "discard"), ("push", "force_push"), ("git_history", "force_push"), ("git_history", "history_rewrite"),
        ("run", "exec_opaque"), ("destructive", "delete"), ("destructive", "discard"), ("destructive", "force_push"),
        ("destructive", "history_rewrite"), ("destructive", "deploy_destroy"),
        ("*", "read"), ("any", "network"), ("delete", "delete"),
    ])
    def test_matches(self, rule, effect):
        assert action_matches(rule, effect)

    @pytest.mark.parametrize("rule,effect", [
        ("delete", "edit"), ("edit", "modify"), ("edit", "delete"), ("modify", "read"), ("modify", "push"),
        ("push", "commit"), ("force_push", "push"), ("commit", "push"), ("destructive", "edit"), ("destructive", "push"),
        ("network", "install"), ("run", "test"), ("test", "run"),
    ])
    def test_does_not_match(self, rule, effect):
        assert not action_matches(rule, effect)


# ---------------------------------------------------------------- Rule

class TestRule:
    def test_round_trip(self):
        r = Rule(id="R-abc123", kind="confirm", action="push", scope=Scope(branches=["main", "*/main"], pattern="x"),
                 text="ask before pushing to main", source="user", mode="always", prerequisite="", created_turn=3, active=False)
        d = r.as_dict()
        assert d["scope"] == {"paths": [], "branches": ["main", "*/main"], "pattern": "x", "command": ""}
        r2 = Rule.from_dict(d)
        assert r2 == r
        assert r2.as_dict() == d

    def test_from_dict_string_scope(self):
        r = Rule.from_dict({"id": "R-1", "kind": "forbid", "action": "delete", "scope": "data/**"})
        assert r.scope.paths == ["data/**"]

    def test_from_dict_missing_scope_and_extra_keys(self):
        r = Rule.from_dict({"id": "R-1", "kind": "forbid", "action": "delete", "bogus": 1, "scope": {"paths": ["a"], "junk": 2}})
        assert r.scope == Scope(paths=["a"])
        assert r.active and r.mode == "once"

    def test_from_dict_rejects_unknown_kind(self):
        with pytest.raises(ValueError):
            Rule.from_dict({"id": "R-1", "kind": "block", "action": "delete"})

    def test_matches_requires_action_and_scope(self):
        r = Rule(id="R-1", kind="forbid", action="modify", scope=Scope(paths=["tests/**"]))
        assert r.matches(Effect("edit", "tests/test_a.py")) is True
        assert r.matches(Effect("delete", "tests/test_a.py")) is True
        assert r.matches(Effect("edit", "src/a.py")) is False
        assert r.matches(Effect("read", "tests/test_a.py")) is False
        assert r.matches(Effect("edit", UNKNOWN)) is None

    def test_inactive_rule_never_matches(self):
        r = Rule(id="R-1", kind="forbid", action="delete", active=False)
        assert r.matches(Effect("delete", "x")) is False
        assert r.matches(Effect("delete", UNKNOWN)) is False

    def test_make_id_is_stable_and_distinct(self):
        a = make_id("never delete data", "forbid", "delete")
        assert a == make_id("never delete data", "forbid", "delete")
        assert a.startswith("R-") and len(a) == 8
        assert a != make_id("never delete data", "confirm", "delete")
        assert a != make_id("never delete data", "forbid", "modify")

    def test_describe_forbid(self):
        r = Rule(id="R-1", kind="forbid", action="delete", scope=Scope(paths=["data/**", "db/**"]))
        assert r.describe() == "never delete on paths data/**, db/**"
        assert Rule(id="R-1", kind="forbid", action="push").describe() == "never push"

    def test_describe_confirm_modes(self):
        once = Rule(id="R-1", kind="confirm", action="push", scope=Scope(branches=["main"]))
        assert once.describe() == "ask the user before every push on branches main (one confirmation per action)"
        always = Rule(id="R-1", kind="confirm", action="push", mode="always")
        assert always.describe() == "ask the user once before push"

    def test_describe_require_before(self):
        r = Rule(id="R-1", kind="require_before", action="push", prerequisite="tests_pass")
        assert r.describe() == "push only after tests pass"

    def test_describe_freeze(self):
        r = Rule(id="R-1", kind="freeze", action="modify", scope=Scope(paths=["tests/**"]))
        assert r.describe() == "no modify on paths tests/** after the task started (frozen)"

    def test_describe_pattern_and_command(self):
        r = Rule(id="R-1", kind="forbid", action="run", scope=Scope(pattern="prod", command=r"\bprod\b"))
        assert r.describe() == r"never run on matching /prod/; commands /\bprod\b/"
