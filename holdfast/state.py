"""Persistent, lock-protected session state.

State lives on disk (never in the model's context), so compaction, resume and
subagent spawns cannot erase it. One file per Claude Code session_id; subagents
share the parent's session_id and therefore the same rules and state.
"""
from __future__ import annotations

import fcntl
import re
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


def project_root(cwd: str | None = None) -> Path:
    """Claude Code exports CLAUDE_PROJECT_DIR to hooks; otherwise walk up from cwd to the nearest .holdfast/ or .git
    so that an agent `cd`-ing into a subdirectory keeps the same state (REVIEW.md: state loss on cd)."""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    start = Path(cwd or os.getcwd()).resolve()
    for d in (start, *start.parents):
        if (d / ".holdfast").is_dir() or (d / ".git").exists():
            return d
    return start


def _git_dir(root: Path) -> Path | None:
    """The repository's git directory (handles `.git` files of worktrees/submodules)."""
    g = root / ".git"
    if g.is_dir():
        return g
    if g.is_file():
        try:
            line = g.read_text().strip()
        except OSError:
            return None
        if line.startswith("gitdir:"):
            p = Path(line[7:].strip())
            return p if p.is_absolute() else (root / p)
    return None


def state_dir(cwd: str | None = None, create: bool = True) -> Path:
    """Where session state lives. Default: inside the git directory (`.git/holdfast/`), which git never tracks,
    stashes, cleans, resets or checks out — so ordinary git commands cannot wipe or roll back the rules
    (REVIEW-2 N1). Non-git projects fall back to `<root>/.holdfast/` with a `*` .gitignore."""
    override = os.environ.get("HOLDFAST_DIR")
    if override:
        d = Path(override)
    else:
        root = project_root(cwd)
        g = _git_dir(root)
        d = (g / "holdfast") if g is not None else (root / ".holdfast")
    if not create:
        return d
    d.mkdir(parents=True, exist_ok=True)
    (d / "sessions").mkdir(exist_ok=True)
    if d.name == ".holdfast" and not (d / ".gitignore").exists():
        try:
            (d / ".gitignore").write_text("*\n")
        except OSError:
            pass
    return d


def sentinel_dir() -> Path:
    """Out-of-tree record of which sessions have state, so a vanished state directory is detected (fail closed)."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "holdfast" / "sessions"


def team_rules_path(cwd: str | None = None) -> Path:
    """Standing rules a team commits to the repository (optional)."""
    return project_root(cwd) / ".holdfast" / "rules.json"


_SID = re.compile(r"[^A-Za-z0-9_.-]")


def safe_session_id(sid: str | None) -> str:
    """Session ids become file names: no path separators, no leading dots (REVIEW-2 S3)."""
    s = _SID.sub("_", sid or "default").lstrip(".") or "default"
    return s[:128]


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
        "created": [],                # project-relative paths first created in this session (deleting them is not a loss)
        "pending_created": {},        # tool_use_id -> [paths] written by a call that has not completed yet
        "errors": 0,                  # internal hook errors (state kept, see hook.main)
    }


class Store:
    def __init__(self, session_id: str, cwd: str | None = None):
        self.session_id = safe_session_id(session_id)
        self.cwd = cwd
        self.dir = state_dir(cwd)
        self.path = self.dir / "sessions" / f"{self.session_id}.json"
        self.backup_path = self.dir / "sessions" / f"{self.session_id}.bak.json"
        self.lock_path = self.dir / "sessions" / f"{self.session_id}.lock"
        self.audit_path = self.dir / "audit.jsonl"
        import hashlib
        key = hashlib.sha256(f"{os.path.realpath(self.dir)}|{self.session_id}".encode()).hexdigest()[:32]
        self.sentinel = sentinel_dir() / key

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

    @staticmethod
    def _read(path: Path) -> dict:
        with open(path) as f:
            st = json.load(f)
        if not isinstance(st, dict) or not isinstance(st.get("rules", []), list):
            raise ValueError("session file is not a state object")
        return st

    def load(self) -> dict:
        """Primary file, else the backup copy written with every save. A session whose state vanished or is
        unreadable with no usable backup is marked `integrity_lost`: the monitor then fails closed (REVIEW-2 S2/N1)
        instead of silently starting over with no rules."""
        st, incident = None, None
        if self.path.exists():
            try:
                st = self._read(self.path)
            except (json.JSONDecodeError, OSError, ValueError) as e:
                incident = f"session file unreadable: {str(e)[:160]}"
                try:
                    os.replace(self.path, self.path.with_suffix(f".corrupt.{int(time.time())}.json"))
                except OSError:
                    pass
        elif self.backup_path.exists():
            incident = "session file missing"
        elif self.sentinel.exists():
            incident = "session state directory missing"
        if st is None and incident:
            try:
                st = self._read(self.backup_path)
                st["errors"] = st.get("errors", 0) + 1
                st["restored_from_backup"] = incident
            except (json.JSONDecodeError, OSError, ValueError):
                st = default_state()
                st["rules"] = self._standing_rules()
                st["errors"] = 1
                st["integrity_lost"] = incident
                st["corrupt_reload"] = incident
        if st is None:
            st = default_state()
            st["rules"] = self._standing_rules()
        base = default_state()
        for k, v in base.items():
            st.setdefault(k, v)
        return st

    def _write_atomic(self, path: Path, state: dict) -> None:
        tmp = tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp")
        try:
            json.dump(state, tmp, indent=1)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, path)
        finally:
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)

    def save(self, state: dict) -> None:
        self._write_atomic(self.path, state)
        self._write_atomic(self.backup_path, state)
        if state.get("rules") and not self.sentinel.exists():
            try:
                self.sentinel.parent.mkdir(parents=True, exist_ok=True)
                self.sentinel.write_text(str(self.dir) + "\n")
            except OSError:
                pass

    def _standing_rules(self) -> list[dict]:
        """Standing rules: `<state dir>/rules.json` (`holdfast rules add`) and `<root>/.holdfast/rules.json` (committed)."""
        rules = []
        for p in dict.fromkeys([self.dir / "rules.json", team_rules_path(self.cwd)]):
            if not p.exists():
                continue
            try:
                data = json.load(open(p))
            except Exception:
                continue
            rules += list(data.get("rules", data) if isinstance(data, dict) else data)
        from .rules import Rule, make_id
        out, seen = [], set()
        for r in rules:
            if not isinstance(r, dict) or "kind" not in r or "action" not in r:
                continue
            r = dict(r)
            r.setdefault("id", make_id(r.get("text", ""), r["kind"], r["action"]))
            r.setdefault("source", "config")
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            try:
                out.append(Rule.from_dict(r).as_dict())
            except Exception:
                continue
        return out

    def audit(self, record: dict) -> None:
        record = {"ts": time.time(), "session_id": self.session_id, **_redact(record)}
        with open(self.audit_path, "a") as f:
            f.write(json.dumps(record) + "\n")


_SECRET = re.compile(r"(?i)((?:api[_-]?key|token|secret|passw(?:or)?d|pwd|auth|bearer|credential|private[_-]?key)[\w-]*\s*[=:]\s*|"
                     r"authorization:\s*(?:bearer|basic)\s+)(['\"]?)[^\s'\"]{4,}")
_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|"
                    r"AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})")


def _redact(obj, key: str = ""):
    """Audit records keep what is needed to explain a decision, not file contents or credentials (REVIEW S5)."""
    if isinstance(obj, dict):
        return {k: _redact(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v, key) for v in obj]
    if isinstance(obj, str):
        s = _TOKEN.sub("[REDACTED]", _SECRET.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", obj))
        if key in ("content", "new_string", "old_string", "new_source") and len(s) > 200:
            s = s[:200] + f"… [{len(obj)} chars]"
        return s
    return obj
