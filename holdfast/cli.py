"""`holdfast` command line: install hooks, manage rules, inspect state, replay audit."""
from __future__ import annotations

import argparse
import json
import shlex
import os
import sys
from pathlib import Path

from .rules import Rule, Scope, extract_rules, make_id
from .state import Store, state_dir, project_root

HOOK_PY = Path(__file__).resolve().parent / "hook.py"
EVENTS = ["PreToolUse", "PostToolUse", "PostToolUseFailure", "UserPromptSubmit", "SessionStart", "SubagentStart", "PreCompact", "PostCompact"]


def hooks_config(python: str = "python3", hook_py: str | None = None, timeout: int = 15) -> dict:
    hp = hook_py or str(HOOK_PY)
    cfg = {}
    for ev in EVENTS:
        # quoted: interpreter and package paths with spaces ("~/My Projects/.venv") must not split the command
        entry = {"type": "command", "command": f"{shlex.quote(python)} {shlex.quote(hp)} {ev}", "timeout": timeout}
        if ev == "PreToolUse":
            entry["onFailure"] = "block"   # fail closed if the hook itself crashes (Claude Code ≥ 2.1.295)
        block = {"hooks": [entry]}
        if ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
            block["matcher"] = ""
        cfg[ev] = [block]
    return {"hooks": cfg}


def cmd_install(a):
    root = project_root()
    target = root / ".claude" / ("settings.local.json" if a.local else "settings.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if target.exists():
        try:
            existing = json.load(open(target))
        except json.JSONDecodeError:
            print(f"refusing to overwrite unparsable {target}", file=sys.stderr)
            return 1
    new = hooks_config(a.python)
    hooks = existing.setdefault("hooks", {})
    for ev, blocks in new["hooks"].items():
        lst = hooks.setdefault(ev, [])
        lst[:] = [b for b in lst if "holdfast" not in json.dumps(b)]
        lst.extend(blocks)
    json.dump(existing, open(target, "w"), indent=2)
    if a.local:
        # keep the local hook config out of `git stash -u` / `git clean -fd` (they only take ignored files with -a / -x)
        exclude = root / ".git" / "info" / "exclude"
        if exclude.parent.is_dir():
            lines = exclude.read_text().splitlines() if exclude.exists() else []
            if ".claude/settings.local.json" not in lines:
                exclude.write_text("\n".join(lines + [".claude/settings.local.json"]) + "\n")
    print(f"installed holdfast hooks into {target}")
    return 0


def cmd_print_hooks(a):
    print(json.dumps(hooks_config(a.python), indent=2))
    return 0


def cmd_rules(a):
    d = state_dir()
    p = d / "rules.json"
    data = {"rules": []}
    if p.exists():
        data = json.load(open(p))
    if a.action == "list":
        for r in data["rules"]:
            print(f"{r['id']}: {Rule.from_dict(r).describe()}")
        return 0
    if a.action == "add":
        text = " ".join(a.text)
        rules = extract_rules(text, source="config")
        if not rules:
            print("could not extract a rule; use --kind/--rule-action/--paths to add it explicitly", file=sys.stderr)
            return 1
        for r in rules:
            data["rules"].append(r.as_dict())
            print(f"added {r.id}: {r.describe()}")
        json.dump(data, open(p, "w"), indent=2)
        return 0
    if a.action == "add-explicit":
        sc = Scope(paths=a.paths or [], branches=a.branches or [], pattern=a.pattern or "")
        r = Rule(id=make_id(" ".join(a.text), a.kind, a.rule_action + str(sc)), kind=a.kind, action=a.rule_action, scope=sc,
                 text=" ".join(a.text) or f"{a.kind} {a.rule_action}", prerequisite=a.prerequisite or "")
        data["rules"].append(r.as_dict())
        json.dump(data, open(p, "w"), indent=2)
        print(f"added {r.id}: {r.describe()}")
        return 0
    return 1


def cmd_status(a):
    d = state_dir()
    sessions = sorted((d / "sessions").glob("*.json"), key=os.path.getmtime)
    if not sessions:
        print("no sessions yet")
        return 0
    st = json.load(open(sessions[-1]))
    print(f"session {sessions[-1].stem}: turn={st['turn']} compactions={st['compactions']} subagents={st['subagents']} decisions={st['decisions']}")
    for r in st["rules"]:
        print(f"  {'[off] ' if not r.get('active', True) else ''}{r['id']}: {Rule.from_dict(r).describe()}   <{r['source']}>")
    if st.get("pending_confirm"):
        print("  pending confirmation:", st["pending_confirm"]["rule_id"], st["pending_confirm"]["effect"])
    return 0


def cmd_explain(a):
    """Dry-run: what would holdfast decide for this command, given the project's standing rules?"""
    from .monitor import Config, evaluate
    st = Store("explain").load()
    for r in extract_rules(" ".join(a.rule) if a.rule else ""):
        st["rules"].append(r.as_dict())
    st["task_started"] = True
    d = evaluate(a.tool, {"command": " ".join(a.command)} if a.tool == "Bash" else {"file_path": " ".join(a.command)},
                 st, os.getcwd(), str(project_root()), Config())
    print(json.dumps(d.as_dict(), indent=2))
    return 0


def cmd_audit(a):
    p = state_dir() / "audit.jsonl"
    if not p.exists():
        print("no audit log")
        return 0
    for line in open(p):
        rec = json.loads(line)
        if a.decisions_only and rec.get("event") != "PreToolUse":
            continue
        if rec.get("event") == "PreToolUse":
            print(f"{rec['decision']:5s} {rec.get('rule_id',''):9s} {rec['tool']:8s} {json.dumps(rec['tool_input'])[:100]}")
        else:
            print(f"{rec['event']:16s} {json.dumps({k: v for k, v in rec.items() if k not in ('ts', 'session_id', 'event')})[:120]}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="holdfast", description="Stateful constraint guard for coding agents")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("install", help="write hooks into .claude/settings(.local).json"); s.add_argument("--local", action="store_true"); s.add_argument("--python", default=sys.executable or "python3"); s.set_defaults(f=cmd_install)
    s = sub.add_parser("print-hooks"); s.add_argument("--python", default="python3"); s.set_defaults(f=cmd_print_hooks)
    s = sub.add_parser("rules"); s.add_argument("action", choices=["list", "add", "add-explicit"]); s.add_argument("text", nargs="*")
    s.add_argument("--kind", choices=["forbid", "confirm", "require_before", "freeze"]); s.add_argument("--rule-action"); s.add_argument("--paths", nargs="*"); s.add_argument("--branches", nargs="*"); s.add_argument("--pattern"); s.add_argument("--prerequisite"); s.set_defaults(f=cmd_rules)
    s = sub.add_parser("status"); s.set_defaults(f=cmd_status)
    s = sub.add_parser("explain", help="dry-run a tool call against rules"); s.add_argument("--tool", default="Bash"); s.add_argument("--rule", nargs="*"); s.add_argument("command", nargs="+"); s.set_defaults(f=cmd_explain)
    s = sub.add_parser("audit"); s.add_argument("--decisions-only", action="store_true"); s.set_defaults(f=cmd_audit)
    a = ap.parse_args(argv)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
