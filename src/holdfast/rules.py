"""Rule schema for Holdfast (SPEC §2).

Design
------
A rule is a tiny, closed-vocabulary record: ``kind`` says *how* to enforce
(forbid / confirm / order / scope) and ``action`` says *what* tool behaviour it
is about (delete, push, edit_path, ...). ``target`` optionally narrows the rule
(a glob for paths, a command name for run_cmd, a marker for touch_prod).

Validation is done once, in ``Rule.__post_init__``, so every other module can
trust that a ``Rule`` instance is well-formed. Serialisation is plain dicts so
the state file stays readable JSON.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Iterable

KINDS: tuple[str, ...] = ("forbid", "confirm", "order", "scope")

ACTIONS: tuple[str, ...] = (
    "delete", "force_push", "push", "commit", "edit_path",
    "run_cmd", "network", "install", "touch_prod", "any",
)

PREDICATES: tuple[str, ...] = ("tests_passed", "user_confirmed", "task_started")

# Canonical human phrasing for each action (used in reasons / listings).
ACTION_PHRASE: dict[str, str] = {
    "delete": "deleting files",
    "force_push": "force-pushing",
    "push": "pushing",
    "commit": "committing",
    "edit_path": "editing files",
    "run_cmd": "running the command",
    "network": "network access",
    "install": "installing packages",
    "touch_prod": "touching production",
    "any": "any action",
}


class RuleError(ValueError):
    """Raised when a rule dict/instance does not satisfy the schema."""


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


@dataclass(eq=True)
class Rule:
    id: str
    kind: str
    action: str
    target: str | None = None
    requires: str | None = None
    once: bool = True
    source: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    active: bool = True

    # -- validation ---------------------------------------------------------
    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise RuleError("rule id must be a non-empty string")
        if self.kind not in KINDS:
            raise RuleError(f"unknown kind {self.kind!r}; expected one of {KINDS}")
        if self.action not in ACTIONS:
            raise RuleError(f"unknown action {self.action!r}; expected one of {ACTIONS}")
        if self.kind == "order":
            if self.requires not in PREDICATES:
                raise RuleError(
                    f"order rule needs 'requires' in {PREDICATES}, got {self.requires!r}")
        if self.kind == "scope":
            if self.action != "edit_path":
                raise RuleError("scope rules apply to action 'edit_path' only")
            if not self.target:
                raise RuleError("scope rule needs a target glob")
        if self.action == "run_cmd" and not self.target:
            raise RuleError("run_cmd rules need a target command name")
        if self.target is not None and not isinstance(self.target, str):
            raise RuleError("target must be a string")
        self.source = dict(self.source or {})

    # -- (de)serialisation ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    # -- presentation ---------------------------------------------------------
    def describe(self) -> str:
        """One short line, e.g. ``r2: confirm before deleting files``."""
        phrase = ACTION_PHRASE.get(self.action, self.action)
        if self.action == "run_cmd" and self.target:
            phrase = f"running `{self.target}`"
        if self.kind == "forbid":
            body = f"forbid {phrase}"
            if self.target and self.action != "run_cmd":
                body += f" matching {self.target!r}"
        elif self.kind == "confirm":
            body = f"confirm with the user before {phrase}"
            if self.target:
                body += f" matching {self.target!r}"
        elif self.kind == "order":
            body = f"order: {phrase} only after {self.requires}"
        else:  # scope
            body = f"scope: only edit files under {self.target!r}"
        return f"{self.id}: {body}"

    def source_note(self) -> str:
        """``from your message at turn 3`` or ``added manually`` for reasons."""
        src = self.source or {}
        via = src.get("via")
        turn = src.get("turn")
        if via == "manual" or turn is None:
            return "added via CLI" if via == "manual" else "from your instructions"
        return f"from your message at turn {turn}"


_FIELDS = {f.name for f in dataclasses.fields(Rule)}


def rule_from_dict(d: dict[str, Any], default_id: str | None = None) -> Rule:
    """Build a Rule from a JSON dict, ignoring unknown keys."""
    if not isinstance(d, dict):
        raise RuleError("rule must be a dict")
    data = {k: v for k, v in d.items() if k in _FIELDS}
    if "id" not in data and default_id:
        data["id"] = default_id
    for key in ("id", "kind", "action"):
        if key not in data:
            raise RuleError(f"rule missing required field {key!r}")
    if not data.get("created_at"):
        data.pop("created_at", None)
    return Rule(**data)


def rules_from_list(items: Iterable[dict[str, Any]]) -> list[Rule]:
    return [rule_from_dict(d) for d in items]


def next_rule_id(rules: Iterable[Rule]) -> str:
    """Return the next free ``rN`` id, never reusing a number."""
    best = 0
    for r in rules:
        if r.id.startswith("r") and r.id[1:].isdigit():
            best = max(best, int(r.id[1:]))
    return f"r{best + 1}"
