#!/usr/bin/env python3
"""Neutral hook recorder for the e2e benchmark.

Installed in *every* condition (C0..C3) as a Claude Code hook on all tool and
lifecycle events.  It appends the raw hook payload (as one JSON line) to the
log file given as argv[1], never blocks and never changes behaviour, so
violations are measured identically whether or not Holdfast is installed.

Large fields (tool_response stdout / file contents) are truncated to keep the
log small; everything the grader needs (tool_name, tool_input, tool_use_id,
agent_id, session_id, hook_event_name, source/trigger) is kept verbatim.
"""
import json
import sys
import time

MAX_FIELD = 6000


def _trim(obj, depth=0):
    if isinstance(obj, str):
        return obj if len(obj) <= MAX_FIELD else obj[:MAX_FIELD] + f"...<+{len(obj) - MAX_FIELD} chars>"
    if isinstance(obj, dict):
        return {k: _trim(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_trim(v, depth + 1) for v in obj[:200]]
    return obj


def main() -> int:
    log = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:  # noqa: BLE001
        payload = {"_recorder_error": repr(exc)}
    if isinstance(payload, dict):
        payload["_ts"] = time.time()
    if log:
        try:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(_trim(payload)) + "\n")
        except OSError:
            pass
    sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
