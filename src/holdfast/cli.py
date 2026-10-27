"""Command-line entry point: ``holdfast hook|add|list|remove|status|install``.

Design
------
``main(argv, stdout, stderr)`` is callable in-process (tests) and from the
console script.  ``hook <Event>`` delegates to :mod:`holdfast.hooks`; the
other subcommands manage rules in ``<project>/.holdfast/state.json`` and
``install`` merges the hook configuration into ``.claude/settings.json``
without disturbing anything already there (idempotent).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, TextIO

from . import hooks
from .extract import extract
from .rules import ACTIONS, KINDS, Rule, RuleError, next_rule_id
from .state import Store, find_project_dir

TOOL_MATCHER = "Bash|Edit|Write|MultiEdit|NotebookEdit"
HOOK_EVENTS: list[tuple[str, str | None]] = [
    ("PreToolUse", TOOL_MATCHER),
    ("PostToolUse", TOOL_MATCHER),
    ("PostToolUseFailure", "Bash"),
    ("UserPromptSubmit", None),
    ("SessionStart", None),
    ("SubagentStart", None),
    ("PreCompact", None),
]


def hook_settings(command: str = "holdfast") -> dict[str, Any]:
    """The settings.json fragment ``install`` merges in."""
    out: dict[str, list[dict[str, Any]]] = {}
    for event, matcher in HOOK_EVENTS:
        group: dict[str, Any] = {}
        if matcher:
            group["matcher"] = matcher
        group["hooks"] = [{"type": "command", "command": f"{command} hook {event}", "timeout": 10,
                           "onFailure": "continue"}]
        out[event] = [group]
    return {"hooks": out}


def merge_settings(settings: dict[str, Any], fragment: dict[str, Any]) -> dict[str, Any]:
    hooks_cfg = settings.setdefault("hooks", {})
    for event, groups in fragment["hooks"].items():
        existing = hooks_cfg.setdefault(event, [])
        for group in groups:
            cmd = group["hooks"][0]["command"]
            if any(h.get("command") == cmd for g in existing for h in g.get("hooks", [])):
                continue
            existing.append(group)
    return settings


# ------------------------------------------------------------------ subcommands

def cmd_add(store: Store, args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    if args.kind or args.action:
        if not (args.kind and args.action):
            err.write("--kind and --action must be given together\n")
            return 1
        specs = [{"kind": args.kind, "action": args.action, "target": args.target, "requires": args.requires,
                  "source": {"turn": None, "text": args.text, "via": "manual"}}]
    else:
        specs = extract(args.text, turn=None)
        for s in specs:
            s["source"]["via"] = "manual"
        if not specs:
            err.write("No constraint recognised in that text. Use --kind/--action/--target to add one explicitly, e.g.\n"
                      "  holdfast add --kind forbid --action delete --target 'tests/**' \"never delete tests\"\n")
            return 1
    with store.transaction() as state:
        added = []
        for spec in specs:
            try:
                rule = Rule(id=next_rule_id(state.rules), kind=spec["kind"], action=spec["action"],
                            target=spec.get("target"), requires=spec.get("requires"), source=spec["source"])
            except RuleError as exc:
                err.write(f"invalid rule: {exc}\n")
                return 1
            state.rules.append(rule)
            added.append(rule)
    for r in added:
        out.write(f"added {r.describe()}\n")
    return 0


def cmd_list(store: Store, args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    state = store.load()
    rules = state.rules if args.all else state.active_rules
    if args.json:
        out.write(json.dumps([r.to_dict() for r in rules], indent=2) + "\n")
        return 0
    if not rules:
        out.write("no rules\n")
    for r in rules:
        flag = "" if r.active else " (inactive)"
        out.write(f"{r.describe()}{flag}  [{r.source_note()}]\n")
    return 0


def cmd_remove(store: Store, args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    with store.transaction() as state:
        before = len(state.rules)
        state.rules = [r for r in state.rules if r.id != args.id]
        if len(state.rules) == before:
            err.write(f"no rule with id {args.id}\n")
            return 1
    out.write(f"removed {args.id}\n")
    return 0


def cmd_status(store: Store, args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    state = store.load()
    out.write(f"project: {store.project_dir}\n")
    out.write(f"state file: {store.state_path} ({'present' if store.exists() else 'absent'})\n")
    out.write(f"rules ({len(state.active_rules)} active):\n")
    for r in state.active_rules:
        out.write(f"  {r.describe()}\n")
    out.write("predicates: " + ", ".join(f"{k}={v}" for k, v in state.predicates.items()) + "\n")
    pending = [c for c in state.confirmations if c.get("remaining", 0) > 0]
    out.write(f"pending confirmations: {len(pending)}\n")
    for c in pending:
        out.write(f"  {c['action']} {c.get('target') or ''} (turn {c.get('granted_turn')}, remaining {c['remaining']})\n")
    n_events = 0
    if os.path.exists(store.events_path):
        with open(store.events_path, encoding="utf-8") as fh:
            n_events = sum(1 for _ in fh)
    out.write(f"events logged: {n_events}; last turn: {state.meta.get('last_turn')}; "
              f"compactions: {state.meta.get('compact_count', 0)}\n")
    return 0


def cmd_install(store: Store, args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    path = args.settings_path or os.path.join(store.project_dir, ".claude", "settings.json")
    settings: dict[str, Any] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        settings = json.loads(raw) if raw.strip() else {}
    merge_settings(settings, hook_settings(args.command))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")
    with store.transaction():
        pass  # creates .holdfast/state.json
    out.write(f"hooks installed into {path}\nstate in {store.dir}\n")
    return 0


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="holdfast", description="Stateful constraint guard for coding agents.")
    p.add_argument("--project", help="project directory (default: nearest .holdfast/ or git root from cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("hook", help="run a Claude Code hook (reads stdin JSON)")
    h.add_argument("event")

    a = sub.add_parser("add", help="add a rule from natural language or explicit fields")
    a.add_argument("text")
    a.add_argument("--kind", choices=KINDS)
    a.add_argument("--action", choices=ACTIONS)
    a.add_argument("--target")
    a.add_argument("--requires")

    ls = sub.add_parser("list", help="list rules")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--all", action="store_true", help="include inactive rules")

    rm = sub.add_parser("remove", help="remove a rule by id")
    rm.add_argument("id")

    sub.add_parser("status", help="show rules, predicates, confirmations")

    ins = sub.add_parser("install", help="merge hook config into .claude/settings.json")
    ins.add_argument("--settings-path")
    ins.add_argument("--command", default="holdfast", help="command used in hooks (default: holdfast)")
    return p


def main(argv: list[str] | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    if args.cmd == "hook":
        return hooks.run(args.event, sys.stdin, out, err)
    project = os.path.abspath(args.project) if args.project else find_project_dir(os.getcwd())
    store = Store(project)
    fn = {"add": cmd_add, "list": cmd_list, "remove": cmd_remove, "status": cmd_status, "install": cmd_install}[args.cmd]
    return fn(store, args, out, err)


def entry() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entry()
