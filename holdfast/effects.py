"""Effect model: what a tool call *does* to the world.

A tool call (Bash command, Edit, Write, ...) is reduced to a list of Effects.
Rules are written against effects, not against raw command strings, so one
rule ("never delete anything under data/") covers `rm`, `find -delete`,
`mv`, `git clean`, shell redirects, `python -c "os.remove(...)"` etc.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import posixpath

# Action vocabulary. Parent → children (a rule on the parent matches children).
ACTION_TREE: dict[str, list[str]] = {
    "modify": ["edit", "write", "delete", "discard", "move"],
    "delete": ["discard"],
    "push": ["force_push"],
    "git_history": ["force_push", "history_rewrite"],
    "run": ["exec_opaque"],
    "destructive": ["delete", "discard", "force_push", "history_rewrite", "deploy_destroy"],
}

ALL_ACTIONS = {
    "read", "edit", "write", "delete", "discard", "move", "run", "exec_opaque",
    "push", "force_push", "commit", "history_rewrite", "network", "install",
    "deploy", "deploy_destroy", "test",
}


def action_matches(rule_action: str, effect_action: str) -> bool:
    if rule_action in ("*", "any"):
        return True
    if rule_action == effect_action:
        return True
    children = ACTION_TREE.get(rule_action, [])
    for c in children:
        if action_matches(c, effect_action):
            return True
    return False


EVERYTHING = "<everything>"   # target that covers every path (rm -rf /, git reset --hard)
UNKNOWN = "<unknown>"         # target we could not resolve ($VAR, xargs, interpreter)


@dataclass
class Effect:
    action: str
    target: str = UNKNOWN          # normalized path, "remote/branch", URL, package, or EVERYTHING/UNKNOWN
    kind: str = "path"             # path | branch | url | package | command | opaque
    detail: str = ""               # human-readable origin (the sub-command that produced it)
    certain: bool = True           # False if derived heuristically (interpreter scan, xargs)

    def as_dict(self) -> dict:
        return {"action": self.action, "target": self.target, "kind": self.kind,
                "detail": self.detail, "certain": self.certain}


def normalize_path(p: str, cwd: str, project_root: str | None = None) -> str:
    """Return a path relative to project_root (or cwd) with '..' resolved.

    Paths that escape the project root are returned as absolute. Paths
    containing unexpanded shell variables / globs that we cannot resolve are
    returned verbatim (callers treat '*' specially).
    """
    if p in ("", "."):
        p = "."
    if p.startswith("~"):
        p = os.path.expanduser(p)
    if "$" in p or "`" in p:
        return UNKNOWN
    root = project_root or cwd
    absp = p if posixpath.isabs(p) else posixpath.join(cwd, p)
    absp = posixpath.normpath(absp)
    root = posixpath.normpath(root)
    if absp == root:
        return "."
    if absp.startswith(root.rstrip("/") + "/"):
        return absp[len(root.rstrip("/")) + 1:]
    return absp


def effect_from_tool(tool_name: str, tool_input: dict, cwd: str, project_root: str | None = None) -> list[Effect]:
    """Map a non-Bash Claude Code tool call to effects."""
    from .shell import classify_command  # local import to avoid cycle

    t = tool_name
    if t == "Bash":
        cmd = tool_input.get("command", "") or ""
        return classify_command(cmd, cwd, project_root)
    if t in ("Edit", "MultiEdit", "NotebookEdit"):
        fp = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return [Effect("edit", normalize_path(fp, cwd, project_root), detail=f"{t} {fp}")]
    if t == "Write":
        fp = tool_input.get("file_path", "")
        return [Effect("write", normalize_path(fp, cwd, project_root), detail=f"Write {fp}")]
    if t in ("Read", "Glob", "Grep", "LS"):
        fp = tool_input.get("file_path") or tool_input.get("path") or "."
        return [Effect("read", normalize_path(fp, cwd, project_root), detail=f"{t} {fp}")]
    if t == "WebFetch" or t == "WebSearch":
        return [Effect("network", tool_input.get("url", tool_input.get("query", UNKNOWN)), kind="url", detail=t)]
    if t in ("Agent", "Task"):
        # Subagent spawn: its own tool calls will be hooked individually.
        return [Effect("run", "subagent", kind="command", detail=t, certain=True)]
    if t.startswith("mcp__"):
        return [Effect("exec_opaque", t, kind="opaque", detail=t, certain=False)]
    return []
