#!/usr/bin/env python3
"""Passive tool-call logger, installed as a Claude Code hook in EVERY condition.

Usage (from .claude/settings.json):  python3 logger_hook.py <log_path>

Appends the raw hook payload as one JSON line to <log_path>.  It never blocks,
never prints to stdout and always exits 0, so it cannot change the agent's
behaviour; the oracle (oracle.py) replays the logged tool_input through
holdfast's effect model to classify what the agent *did*.

Large string fields (tool_response stdout, file contents) are shortened by
keeping their head and tail, so the pytest summary line at the end of a test
run's stdout survives truncation.
"""
import json
import os
import sys
import time

HEAD, TAIL = 4000, 2000


def _trim(obj):
    if isinstance(obj, str):
        if len(obj) <= HEAD + TAIL:
            return obj
        return obj[:HEAD] + f"\n...<{len(obj) - HEAD - TAIL} chars elided>...\n" + obj[-TAIL:]
    if isinstance(obj, dict):
        return {k: _trim(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_trim(v) for v in obj[:200]]
    return obj


def main() -> int:
    log = sys.argv[1] if len(sys.argv) > 1 else None
    if not log:
        return 0
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:  # noqa: BLE001 - a logger must never fail the session
        payload = {"_logger_error": repr(exc)}
    if not isinstance(payload, dict):
        payload = {"_raw": payload}
    payload["_ts"] = time.time()
    payload["_pid"] = os.getpid()
    try:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_trim(payload)) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
