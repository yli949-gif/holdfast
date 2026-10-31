"""Claude Code hook entrypoint.

Usage (from hooks config):  python3 /path/to/holdfast/hook.py <event>
Reads the hook JSON on stdin, writes a hook JSON response on stdout.
Must be fast and must never crash the session: any internal error → exit 0 (fail-open)
unless HOLDFAST_FAIL_CLOSED=1, in which case internal errors deny the tool call.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from holdfast.monitor import Config, context_block, evaluate, handle_prompt, observe_post  # noqa: E402
from holdfast.state import Store, project_root  # noqa: E402


def _emit(obj: dict | None) -> None:
    if obj:
        sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def run(event: str, payload: dict, cfg: Config | None = None) -> dict | None:
    cfg = cfg or Config()
    cwd = payload.get("cwd") or os.getcwd()
    root = str(project_root(cwd))
    sid = payload.get("session_id", "default")
    store = Store(sid, cwd)
    t0 = time.time()

    if event == "PreToolUse":
        tool, tin = payload.get("tool_name", ""), payload.get("tool_input", {}) or {}
        with store.locked() as st:
            first = not st.get("task_started")
            if first:
                st["task_started"] = True
                st["task_started_at"] = t0
            d = evaluate(tool, tin, st, cwd, root, cfg, payload.get("tool_use_id", ""))
            st["decisions"][d.decision] = st["decisions"].get(d.decision, 0) + 1
            store.audit({"event": event, "tool": tool, "tool_input": tin, "agent_id": payload.get("agent_id"),
                         "decision": d.decision if cfg.enforce else f"observe:{d.decision}", "rule_id": d.rule_id,
                         "uncertain": d.uncertain, "effects": [e.as_dict() for e in d.effects], "reason": d.reason,
                         "latency_ms": round((time.time() - t0) * 1000, 1)})
        if not cfg.enforce or d.decision == "allow":
            return None
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": d.decision,
                                       "permissionDecisionReason": d.reason}}

    if event == "PostToolUse":
        tool, tin = payload.get("tool_name", ""), payload.get("tool_input", {}) or {}
        with store.locked() as st:
            changed = observe_post(tool, tin, payload.get("tool_response", {}), st, cwd, root)
        if changed:
            store.audit({"event": event, "tool": tool, "state_changed": changed})
        return None

    if event == "UserPromptSubmit":
        prompt = payload.get("prompt", "") or ""
        with store.locked() as st:
            res = handle_prompt(prompt, st, cfg)
            rules_ctx = context_block(st, "Holdfast registered constraints") if (res["added"] or res["listed"]) else ""
        msgs = []
        if res["added"]:
            msgs.append("Holdfast registered " + str(len(res["added"])) + " new constraint(s) from this message: " +
                        "; ".join(f"{r.id}: {r.describe()}" for r in res["added"]) +
                        ". They are enforced mechanically on every tool call for the rest of the session, including after context compaction and inside subagents.")
        if res["granted"]:
            msgs.append(f"Holdfast: user confirmation recorded for rule {res['granted']['rule_id']} — the previously blocked "
                        f"{res['granted']['effect']['action']} on {res['granted']['effect']['target']} may now be retried once.")
        if res["denied"]:
            msgs.append(f"Holdfast: user declined confirmation for rule {res['denied']['rule_id']}; do not retry that action.")
        if res["dropped"]:
            msgs.append("Holdfast: dropped rule(s) " + ", ".join(res["dropped"]))
        if res["listed"] and rules_ctx:
            msgs.append(rules_ctx)
        store.audit({"event": event, "added": [r.id for r in res["added"]], "granted": bool(res["granted"]), "turn": st["turn"]})
        if msgs and cfg.inject_context:
            return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(msgs)}}
        return None

    if event in ("SessionStart", "SubagentStart", "PostCompact"):
        with store.locked() as st:
            if event == "PostCompact" or payload.get("source") == "compact":
                st["compactions"] = st.get("compactions", 0) + 1
            if event == "SubagentStart":
                st["subagents"] = st.get("subagents", 0) + 1
            ctx = context_block(st, {"SessionStart": "Holdfast constraint guard is active (session start: %s)" % payload.get("source", ""),
                                     "SubagentStart": "Holdfast constraint guard is active for this subagent",
                                     "PostCompact": "Holdfast constraint guard is still active after compaction"}[event])
        store.audit({"event": event, "source": payload.get("source"), "trigger": payload.get("trigger"), "rules": len(st["rules"])})
        if ctx and cfg.inject_context and event != "PostCompact":
            return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": ctx}}
        return None

    if event == "PreCompact":
        with store.locked() as st:
            st["last_precompact_at"] = t0
        store.audit({"event": event, "trigger": payload.get("trigger")})
        return None

    store.audit({"event": event})
    return None


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    event = argv[0] if argv else ""
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
        event = event or payload.get("hook_event_name", "")
        out = run(event, payload)
        _emit(out)
        return 0
    except Exception:
        sys.stderr.write("holdfast hook error:\n" + traceback.format_exc())
        if os.environ.get("HOLDFAST_FAIL_CLOSED") == "1" and event == "PreToolUse":
            return 2
        return 0


if __name__ == "__main__":
    sys.exit(main())
