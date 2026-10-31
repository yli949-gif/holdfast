"""Persistent, lock-protected session state.

State lives on disk (never in the model's context), so compaction, resume and
subagent spawns cannot erase it. One file per Claude Code session_id; subagents
share the parent's session_id and therefore the same rules and state.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


def project_root(cwd: str | None = None) -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    return Path(cwd or os.getcwd())


def state_dir(cwd: str | None = None) -> Path:
    override = os.environ.get("HOLDFAST_DIR")
    d = Path(override) if override else project_root(cwd) / ".holdfast"
    d.mkdir(parents=True, exist_ok=True)
    (d / "sessions").mkdir(exist_ok=True)
    return d


def default_state() -> dict:
    return {
        "version": 1,
        "rules": [],                  # list[Rule.as_dict()]
        "turn": 0,                    # user prompts seen
        "task_started": False,        # first tool call seen → freeze rules arm
        "task_started_at": None,
        "confirmations": {},          # rule_id -> {"granted_at": ts, "consumed": bool, "mode": once|always}
        "pending_confirm": None,      # {"rule_id", "effect", "tool_use_id", "asked_at"}
        "tests_passed_at": None,
        "last_edit_at": None,
        "last_commit_at": None,
        "compactions": 0,
        "subagents": 0,
        "decisions": {"allow": 0, "deny": 0, "ask": 0},
    }


class Store:
    def __init__(self, session_id: str, cwd: str | None = None):
        self.session_id = session_id or "default"
        self.dir = state_dir(cwd)
        self.path = self.dir / "sessions" / f"{self.session_id}.json"
        self.lock_path = self.dir / "sessions" / f"{self.session_id}.lock"
        self.audit_path = self.dir / "audit.jsonl"

    @contextmanager
    def locked(self):
        """Exclusive lock across concurrent hook processes (parallel tool calls)."""
        with open(self.lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                state = self.load()
                yield state
                self.save(state)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def load(self) -> dict:
        if not self.path.exists():
            st = default_state()
            st["rules"] = self._standing_rules()
            return st
        try:
            with open(self.path) as f:
                st = json.load(f)
        except (json.JSONDecodeError, OSError):
            st = default_state()
        base = default_state()
        for k, v in base.items():
            st.setdefault(k, v)
        return st

    def save(self, state: dict) -> None:
        tmp = tempfile.NamedTemporaryFile("w", dir=self.path.parent, delete=False, suffix=".tmp")
        try:
            json.dump(state, tmp, indent=1)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, self.path)
        finally:
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)

    def _standing_rules(self) -> list[dict]:
        """Project-level standing rules from .holdfast/rules.json (committed by the team)."""
        p = self.dir / "rules.json"
        if not p.exists():
            return []
        try:
            data = json.load(open(p))
        except Exception:
            return []
        rules = data.get("rules", data) if isinstance(data, dict) else data
        from .rules import Rule, make_id
        out = []
        for r in rules:
            r = dict(r)
            r.setdefault("id", make_id(r.get("text", ""), r["kind"], r["action"]))
            r.setdefault("source", "config")
            try:
                out.append(Rule.from_dict(r).as_dict())
            except Exception:
                continue
        return out

    def audit(self, record: dict) -> None:
        record = {"ts": time.time(), "session_id": self.session_id, **record}
        with open(self.audit_path, "a") as f:
            f.write(json.dumps(record) + "\n")
