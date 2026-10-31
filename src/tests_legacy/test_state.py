"""Tests for holdfast.state: persistence, locking, predicates, confirmations, events."""
import fcntl
import json
import os
import threading

import pytest

from holdfast.rules import Rule
from holdfast.state import (
    State,
    Store,
    find_project_dir,
    grant_confirmation,
    is_test_command,
    record_tool_outcome,
    revoke_confirmations,
)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path))


def test_default_state_when_missing(store):
    s = store.load()
    assert isinstance(s, State) and s.rules == [] and s.predicates["tests_passed"] is False
    assert s.version == 1 and s.confirmations == []


def test_roundtrip_and_atomic(store, tmp_path):
    s = store.load()
    s.rules.append(Rule(id="r1", kind="forbid", action="delete"))
    s.predicates["tests_passed"] = True
    s.confirmations.append({"action": "delete", "target": None, "granted_turn": 2, "remaining": 1})
    s.meta["last_turn"] = 2
    store.save(s)
    raw = json.loads((tmp_path / ".holdfast" / "state.json").read_text())
    assert raw["rules"][0]["id"] == "r1" and raw["version"] == 1
    assert raw["events"].endswith("events.jsonl")
    s2 = store.load()
    assert s2.rules == s.rules and s2.predicates["tests_passed"] is True
    assert s2.confirmations == s.confirmations and s2.meta["last_turn"] == 2
    leftovers = [p for p in os.listdir(tmp_path / ".holdfast") if p.startswith("state") and p != "state.json"]
    assert leftovers == []


def test_corrupt_state_recovers_and_logs(store, tmp_path):
    d = tmp_path / ".holdfast"
    d.mkdir()
    (d / "state.json").write_text("{not json")
    s = store.load()
    assert s.rules == []
    assert (d / "errors.log").exists()


def test_transaction_saves_on_exit(store, tmp_path):
    with store.transaction() as s:
        s.predicates["task_started"] = True
    assert store.load().predicates["task_started"] is True


def test_transaction_holds_flock(store, tmp_path):
    seen = {}
    with store.transaction():
        fd = os.open(str(tmp_path / ".holdfast" / "lock"), os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            seen["locked"] = False
        except BlockingIOError:
            seen["locked"] = True
        finally:
            os.close(fd)
    assert seen["locked"] is True


def test_transaction_serialises_threads(store):
    def bump():
        with store.transaction() as s:
            s.meta["n"] = s.meta.get("n", 0) + 1
    ts = [threading.Thread(target=bump) for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert store.load().meta["n"] == 10


def test_events_append(store, tmp_path):
    store.append_event(tool="Bash", category="delete", decision="deny", rule_id="r1", agent_id=None)
    store.append_event(tool="Edit", category="edit_path", decision="allow", rule_id=None, agent_id="a1")
    lines = (tmp_path / ".holdfast" / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2
    e = json.loads(lines[0])
    assert e["tool"] == "Bash" and e["decision"] == "deny" and e["rule_id"] == "r1" and "ts" in e
    assert json.loads(lines[1])["agent_id"] == "a1"


def test_log_error(store, tmp_path):
    try:
        raise ValueError("boom")
    except ValueError as exc:
        store.log_error(exc, context="PreToolUse")
    txt = (tmp_path / ".holdfast" / "errors.log").read_text()
    assert "boom" in txt and "PreToolUse" in txt


# --- project dir discovery -----------------------------------------------------------

def test_find_project_dir_walks_up(tmp_path):
    (tmp_path / ".holdfast").mkdir()
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert find_project_dir(str(sub)) == str(tmp_path)


def test_find_project_dir_git_root(tmp_path):
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "a"
    sub.mkdir()
    assert find_project_dir(str(sub)) == str(tmp_path)


def test_find_project_dir_prefers_nearest_holdfast(tmp_path):
    (tmp_path / ".git").mkdir()
    inner = tmp_path / "pkg"
    (inner / ".holdfast").mkdir(parents=True)
    assert find_project_dir(str(inner / "x")) == str(inner) or find_project_dir(str(inner)) == str(inner)


def test_find_project_dir_fallback_cwd(tmp_path):
    sub = tmp_path / "lonely"
    sub.mkdir()
    assert find_project_dir(str(sub)) == str(sub)


# --- confirmations ----------------------------------------------------------------------

def test_grant_and_revoke(store):
    s = store.load()
    grant_confirmation(s, "delete", None, turn=4)
    assert s.confirmations == [{"action": "delete", "target": None, "granted_turn": 4, "remaining": 1}]
    grant_confirmation(s, "push", "main", turn=5, remaining=2)
    revoke_confirmations(s)
    assert all(c["remaining"] == 0 for c in s.confirmations)


def test_save_prunes_spent_confirmations(store):
    s = store.load()
    grant_confirmation(s, "delete", None, turn=4)
    s.confirmations[0]["remaining"] = 0
    store.save(s)
    assert store.load().confirmations == []


# --- predicates ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["pytest", "pytest -q tests/", "python -m pytest", "python3 -m pytest -x",
                                 "npm test", "npm run test", "pnpm test", "yarn test", "go test ./...",
                                 "cargo test", "make test", "tox", "jest", "npx jest", "vitest run", "npx vitest",
                                 "rspec", "mvn test", "gradle test", "./gradlew test", "uv run pytest",
                                 "poetry run pytest", "python -m unittest", "cd sub && pytest", "make check"])
def test_is_test_command(cmd):
    assert is_test_command(cmd), cmd


@pytest.mark.parametrize("cmd", ["ls tests", "cat test.py", "pytest --collect-only", "npm run build",
                                 "go build", "cargo build", "make", "echo pytest", "git push"])
def test_is_not_test_command(cmd):
    assert not is_test_command(cmd), cmd


def test_record_tool_outcome(tmp_path):
    cwd = str(tmp_path)
    s = State()
    record_tool_outcome(s, "Bash", {"command": "pytest -q"}, True, cwd)
    assert s.predicates["tests_passed"] is True
    record_tool_outcome(s, "Edit", {"file_path": "a.py"}, True, cwd)
    assert s.predicates["tests_passed"] is False
    record_tool_outcome(s, "Bash", {"command": "npm test"}, True, cwd)
    assert s.predicates["tests_passed"] is True
    record_tool_outcome(s, "Bash", {"command": "echo x > a.py"}, True, cwd)
    assert s.predicates["tests_passed"] is False
    record_tool_outcome(s, "Bash", {"command": "pytest"}, False, cwd)
    assert s.predicates["tests_passed"] is False
    record_tool_outcome(s, "Bash", {"command": "pytest"}, True, cwd)
    record_tool_outcome(s, "Bash", {"command": "ls"}, True, cwd)
    assert s.predicates["tests_passed"] is True
    record_tool_outcome(s, "Write", {"file_path": "a.py", "content": "x"}, False, cwd)
    assert s.predicates["tests_passed"] is True  # failed edit does not reset
    record_tool_outcome(s, "Bash", {"command": "rm a.py"}, True, cwd)
    assert s.predicates["tests_passed"] is False


def test_record_tool_outcome_test_run_with_log_redirect(tmp_path):
    s = State()
    record_tool_outcome(s, "Bash", {"command": "pytest -q > out.log 2>&1"}, True, str(tmp_path))
    assert s.predicates["tests_passed"] is True
