"""Optional LLM-backed extractor (SPEC §4: ``--llm``; off by default).

Design
------
Shells out to ``claude -p --model haiku --output-format json`` with a strict
prompt that asks for a JSON object matching the rule schema and nothing else.
The result is validated through :class:`holdfast.rules.Rule` so a creative
model cannot inject unknown kinds or actions.  Never used in tests (stubbed)
and never in the PreToolUse hot path; any failure returns ``[]`` so the
pattern extractor's result stands on its own.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from .rules import ACTIONS, KINDS, Rule, RuleError

SCHEMA = {
    "type": "object",
    "properties": {
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"enum": list(KINDS)},
                    "action": {"enum": list(ACTIONS)},
                    "target": {"type": ["string", "null"]},
                    "requires": {"enum": ["tests_passed", "user_confirmed", None]},
                },
                "required": ["kind", "action"],
            },
        }
    },
    "required": ["rules"],
}


def build_prompt(text: str) -> str:
    return (
        "You extract standing CONSTRAINTS a user places on a coding agent. Output ONLY a JSON object "
        f"matching this JSON schema, with no prose:\n{json.dumps(SCHEMA)}\n\n"
        "Semantics: forbid = the action is never allowed; confirm = the action needs explicit user "
        "confirmation each time; order = the action is allowed only when `requires` holds "
        "(tests_passed = the test suite passed after the latest edit); scope = edit_path is allowed ONLY "
        "under `target` (a path glob). `target` is a path glob (e.g. \"tests/**\"), a branch name, a "
        "command name (for run_cmd) or null.\n"
        "Only imperative restrictions count. Task instructions (\"delete the TODO comment\"), statements, "
        "questions and one-off refusals (\"don't delete it\") produce {\"rules\": []}.\n\n"
        f"User message:\n<<<\n{text}\n>>>"
    )


def _parse(stdout: str) -> dict[str, Any]:
    outer = json.loads(stdout)
    result = outer.get("result", outer) if isinstance(outer, dict) else outer
    if isinstance(result, str):
        m = re.search(r"\{.*\}", result, re.S)
        result = json.loads(m.group(0)) if m else {}
    return result if isinstance(result, dict) else {}


def extract_llm(text: str, turn: int | None = None, model: str = "haiku", timeout: float = 30.0) -> list[dict[str, Any]]:
    """Return validated rule dicts from the LLM, or ``[]`` on any failure."""
    cmd = ["claude", "-p", "--model", model, "--output-format", "json", build_prompt(text)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        data = _parse(proc.stdout)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for r in data.get("rules", []) or []:
        if not isinstance(r, dict):
            continue
        spec = {"kind": r.get("kind"), "action": r.get("action"), "target": r.get("target") or None,
                "requires": r.get("requires") or None}
        try:
            Rule(id="tmp", **spec)
        except (RuleError, TypeError):
            continue
        spec["source"] = {"turn": turn, "text": text.strip(), "via": "llm"}
        out.append(spec)
    return out
