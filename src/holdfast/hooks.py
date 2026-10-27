"""Claude Code hook handlers (SPEC §4).

Design
------
``handle(event, payload)`` is the pure-ish core: it takes the parsed stdin
payload and returns the dict to print on stdout.  ``run(event, stdin, stdout)``
wraps it with the I/O and failure policy: *always* exit 0 with valid JSON,
except when ``HOLDFAST_FAIL_CLOSED=1`` and an internal exception occurs (then
exit 2, which Claude Code treats as a block).  Internal exceptions are written
to ``.holdfast/errors.log`` so a bug never silently turns into an allow.

Each event does one load-modify-save of the project state under the file
lock, so parallel subagents see one consistent rule set.

PreToolUse output: ``deny``/``ask`` are emitted with a reason written for the
agent.  ``allow`` is emitted as ``{}`` (no opinion) by default, because an
explicit ``"allow"`` would bypass Claude Code's own permission prompts; set
``HOLDFAST_EMIT_ALLOW=1`` to emit it explicitly.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, TextIO

from .classify import classify
from .engine import evaluate
from .extract import extract_all
from .rules import Rule, next_rule_id
from .state import (
    State,
    Store,
    find_project_dir,
    grant_confirmation,
    record_tool_outcome,
    revoke_confirmations,
)

CONTEXT_EVENTS = {"UserPromptSubmit", "SessionStart", "SubagentStart"}


# ------------------------------------------------------------------ helpers

def _context(state: State, extra: str = "") -> str:
    rules = state.active_rules
    lines = ["Holdfast is enforcing these standing user constraints at every tool call "
             "(they persist across compaction and subagents):"]
    lines += [f"- {r.describe()}" for r in rules]
    preds = ", ".join(f"{k}={str(v).lower()}" for k, v in state.predicates.items())
    lines.append(f"State: {preds}.")
    if any(c.get("remaining", 0) > 0 for c in state.confirmations):
        lines.append("A one-shot user confirmation is pending and will be consumed by the next matching call.")
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def _wrap(event: str, **fields: Any) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event, **fields}}


def _project_dir(payload: dict[str, Any]) -> str:
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    return find_project_dir(cwd or os.getcwd())


def _add_rules(state: State, specs: list[dict[str, Any]]) -> list[Rule]:
    added: list[Rule] = []
    existing = {(r.kind, r.action, r.target, r.requires): r for r in state.rules}
    for spec in specs:
        key = (spec["kind"], spec["action"], spec.get("target"), spec.get("requires"))
        if key in existing:
            if not existing[key].active:
                existing[key].active = True
                added.append(existing[key])
            continue
        rule = Rule(id=next_rule_id(state.rules), kind=spec["kind"], action=spec["action"],
                    target=spec.get("target"), requires=spec.get("requires"), source=spec.get("source", {}))
        state.rules.append(rule)
        existing[key] = rule
        added.append(rule)
    return added


# ------------------------------------------------------------------ event handlers

def on_user_prompt(store: Store, payload: dict[str, Any]) -> dict[str, Any]:
    text = payload.get("prompt") or ""
    with store.transaction() as state:
        turn = int(state.meta.get("last_turn", 0)) + 1
        state.meta["last_turn"] = turn
        state.predicates.setdefault("task_started", True)
        state.predicates["task_started"] = True
        res = extract_all(text, turn=turn)
        added = _add_rules(state, res.rules)
        notes = []
        if res.revoked:
            n = revoke_confirmations(state)
            if n:
                notes.append("Holdfast: pending confirmation(s) revoked by the user.")
        for c in res.confirmations:
            grant_confirmation(state, c.action, c.target, turn)
            what = c.action if c.action != "any" else "the pending action"
            notes.append(f"Holdfast: user confirmation recorded for {what}"
                         f"{' on ' + c.target if c.target else ''} (one-shot).")
        if added:
            notes.append("Holdfast recorded new rule(s): " + "; ".join(r.describe() for r in added) +
                         ". Holdfast will block violating tool calls automatically.")
        if not state.active_rules and not notes:
            return {}
        store.append_event(event="UserPromptSubmit", turn=turn, added=[r.id for r in added],
                           confirmations=[c.action for c in res.confirmations], revoked=res.revoked,
                           agent_id=payload.get("agent_id"), session_id=payload.get("session_id"))
        return _wrap("UserPromptSubmit", additionalContext=_context(state, " ".join(notes)))


def on_pre_tool_use(store: Store, payload: dict[str, Any]) -> dict[str, Any]:
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd()
    actions = classify(tool, tool_input, cwd)
    with store.transaction() as state:
        if not state.active_rules:
            return _allow_output()
        decision = evaluate(state.active_rules, state, actions, cwd)
        store.append_event(event="PreToolUse", tool=tool, category=sorted({a.category for a in actions}),
                           decision=decision.decision, rule_id=decision.rule_id, reason=decision.reason[:300],
                           agent_id=payload.get("agent_id"), session_id=payload.get("session_id"),
                           command=(tool_input.get("command") or tool_input.get("file_path") or "")[:200])
    if decision.decision == "allow":
        return _allow_output(decision.reason)
    return _wrap("PreToolUse", permissionDecision=decision.decision, permissionDecisionReason=decision.reason)


def _allow_output(reason: str = "") -> dict[str, Any]:
    if os.environ.get("HOLDFAST_EMIT_ALLOW") == "1":
        return _wrap("PreToolUse", permissionDecision="allow", permissionDecisionReason=reason or "Holdfast: no rule applies.")
    return {}


def on_post_tool_use(store: Store, payload: dict[str, Any], success: bool) -> dict[str, Any]:
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd()
    resp = payload.get("tool_response")
    if success and isinstance(resp, dict) and resp.get("interrupted"):
        success = False
    if success and isinstance(resp, dict) and tool == "Bash":
        # no exit-code field is provided; treat an explicit non-zero marker as failure if present
        code = resp.get("exit_code", resp.get("exitCode"))
        if isinstance(code, int) and code != 0:
            success = False
    with store.transaction() as state:
        before = dict(state.predicates)
        record_tool_outcome(state, tool, tool_input, success, cwd)
        if state.predicates != before:
            store.append_event(event="PostToolUse" if success else "PostToolUseFailure", tool=tool,
                               predicates=state.predicates, agent_id=payload.get("agent_id"),
                               session_id=payload.get("session_id"))
    return {}


def on_pre_compact(store: Store, payload: dict[str, Any]) -> dict[str, Any]:
    with store.transaction() as state:
        state.meta["compact_count"] = int(state.meta.get("compact_count", 0)) + 1
    return {}


def on_session_start(store: Store, payload: dict[str, Any], event: str) -> dict[str, Any]:
    with store.transaction() as state:
        if not state.meta.get("created_by_session") and payload.get("session_id"):
            state.meta["created_by_session"] = payload.get("session_id")
        if not state.active_rules:
            return {}
        ctx = _context(state)
    return _wrap(event, additionalContext=ctx)


def handle(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one hook event; returns the JSON-serialisable stdout object."""
    if not isinstance(payload, dict):
        payload = {}
    store = Store(_project_dir(payload))
    if event == "UserPromptSubmit":
        return on_user_prompt(store, payload)
    if event == "PreToolUse":
        return on_pre_tool_use(store, payload)
    if event == "PostToolUse":
        return on_post_tool_use(store, payload, success=True)
    if event == "PostToolUseFailure":
        return on_post_tool_use(store, payload, success=False)
    if event == "PreCompact":
        return on_pre_compact(store, payload)
    if event in ("SessionStart", "SubagentStart"):
        return on_session_start(store, payload, event)
    return {}


# ------------------------------------------------------------------ I/O wrapper

def run(event: str, stdin: TextIO | None = None, stdout: TextIO | None = None, err: TextIO | None = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    err = err or sys.stderr
    payload: dict[str, Any] = {}
    try:
        raw = stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        out = handle(event, payload)
    except Exception as exc:  # noqa: BLE001 - the whole point is to never crash the agent
        try:
            Store(_project_dir(payload)).log_error(exc, context=f"hook {event}")
        except Exception:
            pass
        if os.environ.get("HOLDFAST_FAIL_CLOSED") == "1":
            err.write(f"holdfast internal error during {event}: {exc!r}\n")
            return 2
        out = {}
    stdout.write(json.dumps(out))
    stdout.write("\n")
    stdout.flush()
    return 0
