"""Project-scoped persistent state for Holdfast (SPEC §3).

Design
------
Everything Holdfast knows lives in ``<project>/.holdfast/``:

* ``state.json``   rules, predicates, confirmations, meta (atomic writes:
                   tempfile in the same directory + ``os.replace``).
* ``events.jsonl`` append-only log of every decision.
* ``errors.log``   internal exceptions (hooks never crash the agent).
* ``lock``         ``fcntl.flock`` target so parallel subagents serialise
                   their load-modify-save cycles (``Store.transaction``).

The in-memory :class:`State` is a plain dataclass; mutation helpers below
(``grant_confirmation``, ``record_tool_outcome``, ...) are pure functions on
it so hooks and CLI share one code path.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import json
import os
import tempfile
import traceback
from dataclasses import dataclass, field
from typing import Any, Iterator

from .classify import classify
from .bashparse import decompose
from .rules import Rule, rule_from_dict

STATE_DIR = ".holdfast"
TEST_RUNNERS = {"pytest", "py.test", "tox", "jest", "vitest", "rspec", "mocha", "nose2", "phpunit"}
RUN_PREFIXES = {"uv": {"run"}, "poetry": {"run"}, "npx": set(), "pnpm": {"exec", "dlx"}, "bunx": set(),
                "pipenv": {"run"}, "hatch": {"run"}, "pdm": {"run"}}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class State:
    version: int = 1
    rules: list[Rule] = field(default_factory=list)
    predicates: dict[str, Any] = field(default_factory=lambda: {"tests_passed": False, "task_started": False})
    confirmations: list[dict[str, Any]] = field(default_factory=list)
    events: str = os.path.join(STATE_DIR, "events.jsonl")
    meta: dict[str, Any] = field(default_factory=lambda: {"created_by_session": None, "last_turn": 0,
                                                          "compact_count": 0})

    @property
    def active_rules(self) -> list[Rule]:
        return [r for r in self.rules if r.active]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "rules": [r.to_dict() for r in self.rules],
            "predicates": self.predicates,
            "confirmations": [c for c in self.confirmations if c.get("remaining", 0) > 0],
            "events": self.events,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "State":
        s = cls()
        s.version = int(d.get("version", 1))
        s.rules = [rule_from_dict(r) for r in d.get("rules", [])]
        s.predicates.update(d.get("predicates") or {})
        s.confirmations = list(d.get("confirmations") or [])
        s.events = d.get("events") or s.events
        s.meta.update(d.get("meta") or {})
        return s


# ------------------------------------------------------------------ store

class Store:
    """Filesystem access for one project's ``.holdfast/`` directory."""

    def __init__(self, project_dir: str):
        self.project_dir = os.path.abspath(project_dir)
        self.dir = os.path.join(self.project_dir, STATE_DIR)
        self.state_path = os.path.join(self.dir, "state.json")
        self.events_path = os.path.join(self.dir, "events.jsonl")
        self.errors_path = os.path.join(self.dir, "errors.log")
        self.lock_path = os.path.join(self.dir, "lock")

    def _ensure_dir(self) -> None:
        os.makedirs(self.dir, exist_ok=True)

    def exists(self) -> bool:
        return os.path.isfile(self.state_path)

    def load(self) -> State:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                return State.from_dict(json.load(fh))
        except FileNotFoundError:
            return State()
        except Exception as exc:  # corrupt file: start fresh, keep evidence
            self.log_error(exc, context=f"load {self.state_path}")
            try:
                os.replace(self.state_path, self.state_path + ".corrupt")
            except OSError:
                pass
            return State()

    def save(self, state: State) -> None:
        self._ensure_dir()
        data = json.dumps(state.to_dict(), indent=2, sort_keys=False)
        fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=self.dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    @contextlib.contextmanager
    def transaction(self) -> Iterator[State]:
        """Load under an exclusive lock, yield, save, release."""
        self._ensure_dir()
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            state = self.load()
            yield state
            self.save(state)
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def append_event(self, **fields: Any) -> None:
        self._ensure_dir()
        rec = {"ts": _now(), **fields}
        with open(self.events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    def log_error(self, exc: BaseException, context: str = "") -> None:
        try:
            self._ensure_dir()
            with open(self.errors_path, "a", encoding="utf-8") as fh:
                fh.write(f"{_now()} {context}\n")
                fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
                fh.write("\n")
        except OSError:
            pass


def find_project_dir(cwd: str) -> str:
    """Nearest ancestor (incl. cwd) holding ``.holdfast/``, searching no higher
    than the enclosing git root; else that git root; else cwd.

    The search stops at the first ``.git`` so a stray ``.holdfast/`` above the
    repository (e.g. in /tmp or $HOME) can never capture a project.
    """
    cwd = os.path.abspath(cwd)
    cur = cwd
    while True:
        if os.path.isdir(os.path.join(cur, STATE_DIR)):
            return cur
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return cwd


# ------------------------------------------------------------------ confirmations

def grant_confirmation(state: State, action: str, target: str | None, turn: int, remaining: int = 1) -> dict:
    c = {"action": action, "target": target, "granted_turn": turn, "remaining": remaining}
    state.confirmations.append(c)
    return c


def revoke_confirmations(state: State) -> int:
    n = 0
    for c in state.confirmations:
        if c.get("remaining", 0) > 0:
            c["remaining"] = 0
            n += 1
    return n


# ------------------------------------------------------------------ predicates

def is_test_command(command: str, cwd: str = "/") -> bool:
    """Is any simple command in ``command`` a test-suite run?"""
    for s in decompose(command, cwd):
        argv = list(s.argv)
        # strip `uv run`, `poetry run`, `npx` style launchers
        while argv and argv[0] in RUN_PREFIXES:
            subs = RUN_PREFIXES[argv[0]]
            if subs and not (len(argv) > 1 and argv[1] in subs):
                break  # e.g. `pnpm test` is itself the runner, not a launcher
            argv.pop(0)
            if subs:
                argv.pop(0)
            while argv and argv[0].startswith("-"):
                argv.pop(0)
        if not argv:
            continue
        cmd = os.path.basename(argv[0])
        rest = [a for a in argv[1:] if not a.startswith("-")]
        if cmd in TEST_RUNNERS:
            if cmd == "pytest" and "--collect-only" in argv:
                continue
            return True
        if cmd.startswith("python") and argv[1:3] in (["-m", "pytest"], ["-m", "unittest"], ["-m", "nose2"]):
            return True
        if cmd in ("npm", "pnpm", "yarn", "bun") and (rest[:1] == ["test"] or rest[:2] == ["run", "test"]
                                                    or rest[:1] in (["t"], ["tst"])):
            return True
        if cmd in ("go", "cargo", "mvn", "gradle", "gradlew", "dotnet", "mix", "sbt", "swift", "dart", "flutter", "deno", "bun") \
                and rest[:1] == ["test"]:
            return True
        if cmd == "make" and any(t in ("test", "tests", "check", "test-unit", "unit") for t in rest):
            return True
    return False


def has_edit_effect(tool_name: str, tool_input: dict, cwd: str) -> bool:
    for a in classify(tool_name, tool_input, cwd):
        if a.category in ("edit_path", "delete") and not a.opaque:
            return True
    return False


def record_tool_outcome(state: State, tool_name: str, tool_input: dict, success: bool, cwd: str) -> None:
    """Flip ``tests_passed`` from a finished tool call (SPEC §4 PostToolUse*)."""
    if tool_name == "Bash":
        cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
        if isinstance(cmd, str) and is_test_command(cmd, cwd):
            state.predicates["tests_passed"] = bool(success)
            return
    if success and has_edit_effect(tool_name, tool_input, cwd):
        state.predicates["tests_passed"] = False
