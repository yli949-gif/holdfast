"""Effect model: what a tool call *does* to the world.

A tool call (Bash command, Edit, Write, ...) is reduced to a list of Effects.
Rules are written against effects, not against raw command strings, so one
rule ("never delete anything under data/") covers `rm`, `find -delete`,
`mv`, `git clean`, shell redirects, `python -c "os.remove(...)"` etc.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import posixpath
import re

# Action vocabulary. Parent → children (a rule on the parent matches children).
ACTION_TREE: dict[str, list[str]] = {
    # deploying / destroying infrastructure changes the world too ("never touch prod" = modify /prod/)
    "modify": ["edit", "write", "delete", "discard", "move", "deploy", "deploy_destroy"],
    # a move removes its source path ("never delete X" must catch `mv X /tmp/`); see docs/ACTION_TAXONOMY.md
    # ... and destroying infrastructure / data (DROP TABLE, terraform destroy, docker volume rm) is deleting (REVIEW B19)
    "delete": ["discard", "move", "deploy_destroy"],
    "deploy": ["deploy_destroy"],
    # pushing is outbound network I/O even though a push effect does not also emit `network`
    "network": ["push", "force_push"],
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


def _rel_to_root(absp: str, root: str) -> str:
    root = root.rstrip("/") or "/"
    if absp == root:
        return "."
    if absp.startswith(root.rstrip("/") + "/"):
        return absp[len(root.rstrip("/")) + 1:]
    return absp


def resolve_on_disk(effects: list[Effect], cwd: str, project_root: str | None = None, max_glob: int = 64) -> list[Effect]:
    """Make path effects match what the shell / filesystem will actually touch. Only when the project
    root (or cwd) exists on disk:
      * a glob target (`da*`) that matches existing files also yields one effect per match (bash expands it);
      * a path whose real location differs from the lexical one (a symlink on the way, e.g. `t/` -> `tests/`)
        also yields an effect on the resolved path. Paths that do not exist yet still resolve through
        symlinked parents.
    Added effects follow their original."""
    import glob as _glob
    root = project_root or cwd
    if not root or not os.path.isdir(root):
        return effects
    expanded: list[Effect] = []
    for e in effects:
        expanded.append(e)
        t = e.target
        if e.kind == "path" and t not in (EVERYTHING, UNKNOWN) and any(c in t for c in "*?[") and "$" not in t:
            pat = t if posixpath.isabs(t) else posixpath.join(root, t)
            try:
                hits = sorted(_glob.glob(pat))[:max_glob]
            except (OSError, ValueError, re.error):
                hits = []
            for h in hits:
                expanded.append(Effect(e.action, _rel_to_root(posixpath.normpath(h), root), kind=e.kind,
                                       detail=f"{e.detail} (glob {t} matches)", certain=e.certain))
    real_root = os.path.realpath(root)
    out: list[Effect] = []
    for e in expanded:
        out.append(e)
        t = e.target
        if e.kind not in ("path", "opaque") or t in (EVERYTHING, UNKNOWN) or any(c in t for c in "*?[$`"):
            continue
        lexical = posixpath.normpath(t if posixpath.isabs(t) else posixpath.join(root, t))
        try:
            real = os.path.realpath(lexical)
        except (OSError, ValueError):
            continue
        if real == real_root:
            rel = "."
        elif real.startswith(real_root.rstrip("/") + "/"):
            rel = real[len(real_root.rstrip("/")) + 1:]
        else:
            rel = real
        if rel != t and real != lexical or (rel != t and not posixpath.isabs(t)):
            if rel == t or (posixpath.isabs(t) and real == lexical):
                continue
            out.append(Effect(e.action, rel, kind=e.kind, detail=f"{e.detail} (resolved symlink {t} -> {rel})", certain=e.certain))
    return out


def effect_from_tool(tool_name: str, tool_input: dict, cwd: str, project_root: str | None = None) -> list[Effect]:
    """Map a non-Bash Claude Code tool call to effects."""
    from .shell import classify_command  # local import to avoid cycle

    t = tool_name
    if t == "Bash":
        cmd = tool_input.get("command", "") or ""
        return classify_command(cmd, cwd, project_root)
    if t in ("Edit", "MultiEdit", "NotebookEdit"):
        fp = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return resolve_on_disk([Effect("edit", normalize_path(fp, cwd, project_root), detail=f"{t} {fp}")], cwd, project_root)
    if t == "Write":
        fp = tool_input.get("file_path", "")
        return resolve_on_disk([Effect("write", normalize_path(fp, cwd, project_root), detail=f"Write {fp}")], cwd, project_root)
    if t in ("Read", "Glob", "Grep", "LS"):
        fp = tool_input.get("file_path") or tool_input.get("path") or "."
        return [Effect("read", normalize_path(fp, cwd, project_root), detail=f"{t} {fp}")]
    if t == "WebFetch" or t == "WebSearch":
        return [Effect("network", tool_input.get("url", tool_input.get("query", UNKNOWN)), kind="url", detail=t)]
    if t in ("Agent", "Task"):
        # Subagent spawn: its own tool calls will be hooked individually.
        return [Effect("run", "subagent", kind="command", detail=t, certain=True)]
    if t.startswith("mcp__"):
        # read-only by name (get_/list_/search_...): a lookup, not an action (REVIEW-2 O1); anything else is opaque
        op = t.rsplit("__", 1)[-1].lower()
        if re.match(r"^(?:get|list|search|read|fetch|query|describe|view|show|find|lookup|count|check|inspect|retrieve)(?:_|$)", op):
            return [Effect("read", t, kind="command", detail=t)]
        return [Effect("exec_opaque", t, kind="opaque", detail=t, certain=False)]
    return []
