"""Decision engine for Holdfast (SPEC §2 kinds, §4 PreToolUse).

Design
------
``evaluate(rules, state, actions, cwd)`` is a pure function of its inputs
except for one deliberate mutation: when the final decision is *allow* and a
``confirm`` rule was satisfied by a one-shot confirmation, that confirmation's
``remaining`` counter is decremented (the caller persists state).

Per-action semantics:

* ``forbid``  -> deny when the rule applies.
* ``confirm`` -> deny unless an unconsumed confirmation matches.
* ``order``   -> deny unless ``state.predicates[requires]`` is true
  (``user_confirmed`` is looked up in confirmations instead).
* ``scope``   -> for ``edit_path``, deny when the path is *not* under target.
* opaque actions (interpreter one-liners, piped shells): if any active rule
  could apply to the guessed category -> ``ask``.

Across actions the most restrictive outcome wins (deny > ask > allow).
Reasons are written for the agent: they name the rule, quote the user's
instruction, say what is blocked and what to do next.  Paths are shown
relative to ``cwd`` when they live under it.
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from typing import Any, Iterable

from .classify import Action
from .rules import Rule

PROD_MIN_CONF = 0.6
PATH_CATEGORIES = {"edit_path", "delete", "touch_prod"}
GLOB_CHARS = set("*?[")
_RANK = {"allow": 0, "ask": 1, "deny": 2}


@dataclass
class Decision:
    decision: str                # 'allow' | 'deny' | 'ask'
    reason: str = ""
    rule_id: str | None = None
    action: Action | None = None


# ------------------------------------------------------------------ paths & globs

def rel_path(path: str, cwd: str) -> str:
    """Path relative to cwd when under it (``.`` for cwd itself), else unchanged."""
    cwd = cwd.rstrip("/") or "/"
    norm = os.path.normpath(path)
    if norm == cwd:
        return "."
    if norm.startswith(cwd + "/"):
        return norm[len(cwd) + 1:]
    return norm


def glob_match(pattern: str, path: str, cwd: str) -> bool:
    """Does ``pattern`` (a project-relative or absolute glob) cover ``path``?

    ``tests``, ``tests/`` and ``tests/**`` all mean "tests and everything in
    it".  A directory *above* the pattern's literal prefix also matches, so
    ``rm -rf .`` is caught by a rule on ``tests/**``.
    """
    pat = pattern.strip()
    if pat.startswith("./"):
        pat = pat[2:]
    pat = pat.rstrip("/") or pat
    rel = rel_path(path, cwd)
    if pat.startswith("/"):
        cand = os.path.normpath(path)
    else:
        cand = rel
    if fnmatch.fnmatchcase(cand, pat) or fnmatch.fnmatchcase(cand, pat + "/*"):
        return True
    if pat.endswith("/**") and fnmatch.fnmatchcase(cand, pat[:-3]):
        return True
    if "/" not in pat and fnmatch.fnmatchcase(os.path.basename(cand), pat):
        return True
    # path is an ancestor of the literal prefix of the pattern
    prefix = pat
    for i, ch in enumerate(pat):
        if ch in GLOB_CHARS:
            prefix = pat[:i]
            break
    prefix = prefix.rsplit("/", 1)[0] if "/" in prefix else prefix
    if prefix and cand != prefix:
        if cand == "." and not pat.startswith("/"):
            return True
        if prefix.startswith(cand.rstrip("/") + "/"):
            return True
    return False


# ------------------------------------------------------------------ matching

def _paths_match(target: str, paths: list[str], cwd: str) -> bool:
    if not paths:
        return True  # unknown paths: be conservative
    return any(glob_match(target, p, cwd) for p in paths)


def rule_applies(rule: Rule, action: Action, cwd: str) -> bool:
    """Does ``rule`` govern ``action``?  (scope rules are handled separately.)"""
    if not rule.active or rule.kind == "scope":
        return False
    if rule.action != "any" and rule.action != action.category:
        return False
    cat = action.category
    if rule.target:
        if cat in PATH_CATEGORIES:
            return _paths_match(rule.target, action.paths, cwd)
        if cat in ("run_cmd", "push", "force_push"):
            return any(fnmatch.fnmatchcase(p, rule.target) or fnmatch.fnmatchcase(p, rule.target + " *")
                       for p in action.paths)
        return True
    if cat == "touch_prod" and action.confidence < PROD_MIN_CONF:
        return False
    return True


def _find_confirmation(confirmations: Iterable[dict[str, Any]], action: Action, cwd: str) -> dict | None:
    for c in confirmations:
        if c.get("remaining", 0) <= 0:
            continue
        if c.get("action") not in (action.category, "any"):
            continue
        if c.get("target") and not _paths_match(c["target"], action.paths, cwd):
            continue
        return c
    return None


# ------------------------------------------------------------------ reasons

def describe_action(action: Action, cwd: str) -> str:
    paths = ", ".join(rel_path(p, cwd) for p in action.paths[:4])
    if len(action.paths) > 4:
        paths += ", ..."
    cat = action.category
    if cat == "delete":
        return f"deletion of {paths}" if paths else "a deletion"
    if cat == "edit_path":
        return f"editing {paths}" if paths else "a file edit"
    if cat == "run_cmd":
        return f"running `{action.paths[1] if len(action.paths) > 1 else paths}`"
    if cat in ("push", "force_push"):
        verb = "force-pushing" if cat == "force_push" else "pushing"
        return f"{verb} to {action.paths[-1]}" if action.paths else verb
    if cat == "touch_prod":
        return f"touching production ({paths})" if paths else "touching production"
    return {"commit": "committing", "network": "network access", "install": "installing packages"}.get(cat, cat)


def short_rule(rule: Rule) -> str:
    tgt = f" {rule.target}" if rule.target else ""
    if rule.kind == "forbid":
        return f"never {rule.action}{tgt}"
    if rule.kind == "confirm":
        return f"confirm before {rule.action}{tgt}"
    if rule.kind == "order":
        return f"{rule.action} only after {rule.requires}"
    return f"edits only under {rule.target}"


def _head(rule: Rule) -> str:
    return f"Holdfast rule {rule.id} ({short_rule(rule)}, {rule.source_note()})"


def _quote(rule: Rule) -> str:
    text = (rule.source or {}).get("text")
    return f" The user said: {text!r}." if text else ""


def _deny_reason(rule: Rule, action: Action, cwd: str) -> str:
    what = describe_action(action, cwd)
    if rule.kind == "forbid":
        return (f"{_head(rule)}: {what} is forbidden.{_quote(rule)} Do not retry this command; "
                f"tell the user it is blocked by their instruction and propose an alternative.")
    if rule.kind == "confirm":
        return (f"{_head(rule)}: {what} is blocked until the user explicitly confirms.{_quote(rule)} "
                f"Ask the user to confirm this exact action, then retry.")
    if rule.kind == "order":
        if rule.requires == "tests_passed":
            how = "Run the test suite (e.g. pytest / npm test) and make it pass"
        elif rule.requires == "user_confirmed":
            how = "Ask the user for explicit confirmation"
        else:
            how = f"Make `{rule.requires}` true"
        return (f"{_head(rule)}: {what} is blocked because `{rule.requires}` is not currently true "
                f"(tests must pass after the latest edit).{_quote(rule)} {how}, then retry.")
    return (f"{_head(rule)}: {what} is outside the allowed scope {rule.target!r}.{_quote(rule)} "
            f"Only edit files under {rule.target!r}; do not retry this edit.")


def _ask_reason(rule: Rule, action: Action, cwd: str) -> str:
    return (f"Holdfast cannot inspect this command ({action.evidence or 'opaque code'}); it may perform "
            f"{describe_action(action, cwd)}, which rule {rule.id} ({short_rule(rule)}, {rule.source_note()}) "
            f"governs. Rewrite it as explicit shell commands Holdfast can check, or ask the user.")


# ------------------------------------------------------------------ evaluation

def evaluate(rules: Iterable[Rule], state: Any, actions: Iterable[Action], cwd: str) -> Decision:
    """Evaluate every action against every rule; the most restrictive decision wins."""
    rules = [r for r in rules if r.active]
    actions = list(actions)
    if not rules or not actions:
        return Decision("allow")
    predicates: dict[str, Any] = getattr(state, "predicates", {}) or {}
    confirmations: list[dict[str, Any]] = getattr(state, "confirmations", []) or []

    worst = Decision("allow")
    to_consume: list[dict[str, Any]] = []
    satisfied_by: Decision | None = None

    def consider(d: Decision) -> None:
        nonlocal worst
        if _RANK[d.decision] > _RANK[worst.decision]:
            worst = d

    for action in actions:
        for rule in rules:
            if rule.kind == "scope":
                if action.category == "edit_path" and not action.opaque and \
                        not (action.paths and all(glob_match(rule.target, p, cwd) for p in action.paths)):
                    consider(Decision("deny", _deny_reason(rule, action, cwd), rule.id, action))
                continue
            if not rule_applies(rule, action, cwd):
                continue
            if rule.kind == "confirm" or (rule.kind == "order" and rule.requires == "user_confirmed"):
                conf = _find_confirmation(confirmations, action, cwd)
                if conf is not None:
                    if rule.once and conf not in to_consume:
                        to_consume.append(conf)
                    satisfied_by = satisfied_by or Decision(
                        "allow", f"Holdfast rule {rule.id}: allowed by the user's confirmation at turn "
                                 f"{conf.get('granted_turn')}.", rule.id, action)
                    continue
            elif rule.kind == "order":
                if predicates.get(rule.requires) is True:
                    continue
            if action.opaque:
                consider(Decision("ask", _ask_reason(rule, action, cwd), rule.id, action))
            else:
                consider(Decision("deny", _deny_reason(rule, action, cwd), rule.id, action))

    if worst.decision == "allow":
        for conf in to_consume:
            conf["remaining"] = max(0, int(conf.get("remaining", 1)) - 1)
        return satisfied_by or worst
    return worst
