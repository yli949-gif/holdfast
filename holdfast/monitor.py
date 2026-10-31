"""The stateful monitor: effects + rules + session state → decision."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import time

from .effects import Effect, EVERYTHING, UNKNOWN, action_matches, effect_from_tool
from .rules import Rule, extract_rules, has_constraint_cue

# an affirmation word must end at a word boundary ("yes, push it" is a yes; "yesterday's build broke" is not)
AFFIRM = re.compile(r"^\s*(?:yes|y|yep|yeah|yup|sure|ok|okay|confirm(?:ed)?|approved?|go ahead|proceed|do it|go for it|affirmative|please do|i confirm)\b(?:[\s,.!:;-].*)?$", re.I | re.S)
NEGATE = re.compile(r"^\s*(?:no|nope|don't|do not|stop|cancel|abort|never ?mind|negative|not now|hold on|wait)\b", re.I)

DROP = re.compile(r"holdfast:?\s*(?:drop|remove|delete|disable)\s+(?:rule\s+)?(R-[0-9a-f]{6})", re.I)
LIST = re.compile(r"holdfast:?\s*(?:list|rules|status)\b", re.I)


@dataclass
class Config:
    opaque_policy: str = os.environ.get("HOLDFAST_OPAQUE_POLICY", "ask")       # ask | deny | allow  (uncertain matches)
    confirm_via: str = os.environ.get("HOLDFAST_CONFIRM_VIA", "deny")           # deny (instruct agent to ask in chat) | ask (permission prompt)
    inject_context: bool = os.environ.get("HOLDFAST_INJECT", "1") != "0"        # re-anchor rules into context on session start/compact/subagent
    llm_extract: bool = os.environ.get("HOLDFAST_LLM_EXTRACT", "0") == "1"      # recall fallback via `claude -p`
    enforce: bool = os.environ.get("HOLDFAST_ENFORCE", "1") != "0"              # 0 = observe-only (log decisions, never block)


@dataclass
class Decision:
    decision: str                  # allow | deny | ask
    reason: str = ""
    rule_id: str = ""
    effects: list[Effect] = field(default_factory=list)
    uncertain: bool = False
    matched: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"decision": self.decision, "reason": self.reason, "rule_id": self.rule_id,
                "uncertain": self.uncertain, "effects": [e.as_dict() for e in self.effects], "matched": self.matched}


RANK = {"allow": 0, "ask": 1, "deny": 2}


def rules_of(state: dict) -> list[Rule]:
    return [Rule.from_dict(r) for r in state.get("rules", [])]


def _tests_fresh(state: dict) -> bool:
    tp, le = state.get("tests_passed_at"), state.get("last_edit_at")
    if not tp:
        return False
    # strictly later: an edit recorded in the same clock tick as the test run is treated as stale (conservative)
    return le is None or tp > le


def evaluate(tool_name: str, tool_input: dict, state: dict, cwd: str, root: str | None, cfg: Config,
             tool_use_id: str = "") -> Decision:
    """Pure-ish decision function. Mutates `state` only to record pending confirmations / consume grants."""
    effects = effect_from_tool(tool_name, tool_input, cwd, root)
    rules = rules_of(state)
    best = Decision("allow", effects=effects)
    now = time.time()

    for rule in rules:
        for e in effects:
            m = rule.matches(e)
            if m is False and e.action == "exec_opaque" and not e.certain:
                # an opaque execution (script file, unreadable make/npm target, decoded blob, eval of a variable)
                # could perform any action: it matches every active rule as *uncertain*
                m = None
            if m is False:
                continue
            # an effect derived heuristically but with a concrete, in-scope target (os.remove('x'), heredoc writing
            # src/core/util.py) is treated as certain: the target is known, only the mechanism was indirect
            concrete = e.target not in (UNKNOWN, EVERYTHING) and "*" not in e.target and m is True
            uncertain = (m is None) or (not e.certain and not concrete)
            cand: Decision | None = None
            if uncertain:
                pol = cfg.opaque_policy
                why = (f"Holdfast rule {rule.id} ({rule.describe()}): this call's effect on "
                       f"{'an unresolvable target' if e.target == UNKNOWN else e.target} could violate it "
                       f"(derived from: {e.detail}). Use an explicit, non-opaque command so the guard can verify it.")
                if pol == "allow":
                    continue
                cand = Decision(pol, why, rule.id, effects, uncertain=True)
            elif rule.kind == "forbid":
                cand = Decision("deny", f"Holdfast rule {rule.id} forbids this: {rule.describe()} (user said: \"{rule.text}\"). "
                                         f"Blocked effect: {e.action} {e.target}. Do not work around this; tell the user.", rule.id, effects)
            elif rule.kind == "freeze":
                if state.get("task_started"):
                    cand = Decision("deny", f"Holdfast rule {rule.id}: {rule.describe()} (user said: \"{rule.text}\"). "
                                             f"Blocked effect: {e.action} {e.target}. Adjust your approach instead of editing frozen files.", rule.id, effects)
            elif rule.kind == "require_before":
                if rule.prerequisite == "tests_pass" and not _tests_fresh(state):
                    cand = Decision("deny", f"Holdfast rule {rule.id}: {rule.describe()}. "
                                             + ("Tests have not passed since the last edit; run the test suite first."
                                                if state.get("tests_passed_at") else "No passing test run recorded in this session; run the test suite first."),
                                    rule.id, effects)
                elif rule.prerequisite == "user_confirm":
                    rule.kind = "confirm"  # fallthrough semantics
            if rule.kind == "confirm" and not uncertain and cand is None:
                grant = state.get("confirmations", {}).get(rule.id)
                if grant and not grant.get("consumed") and grant.get("target") and not _target_covers(grant["target"], e.target):
                    grant = None   # user confirmed a different target ("go ahead and delete build/") — does not cover this one
                if grant and (grant.get("mode") == "always" or not grant.get("consumed")):
                    if grant.get("mode") != "always":
                        grant["consumed"] = True
                        grant["consumed_by"] = tool_use_id
                    # explicitly allowed by user; continue checking other rules
                    continue
                state["pending_confirm"] = {"rule_id": rule.id, "effect": e.as_dict(), "tool_use_id": tool_use_id, "asked_at": now,
                                            "tool_name": tool_name, "tool_input": tool_input}
                how = cfg.confirm_via
                cand = Decision(how, f"Holdfast rule {rule.id}: {rule.describe()} (user said: \"{rule.text}\"). "
                                     f"Effect {e.action} {e.target} needs explicit user confirmation. "
                                     f"STOP and ask the user in plain language whether to proceed; retry only after they answer yes.",
                                rule.id, effects)
            if cand and RANK[cand.decision] > RANK[best.decision]:
                cand.matched = best.matched   # keep the matches accumulated so far
                best = cand
            if cand:
                best.matched.append({"rule_id": rule.id, "effect": e.as_dict(), "decision": cand.decision, "uncertain": cand.uncertain})
    return best


def observe_post(tool_name: str, tool_input: dict, tool_response: dict | str, state: dict, cwd: str, root: str | None) -> dict:
    """Update temporal state after a tool ran."""
    from .shell import is_test_success
    effects = effect_from_tool(tool_name, tool_input, cwd, root)
    now = time.time()
    changed = {}
    for e in effects:
        if e.action in ("edit", "write", "delete", "move", "discard"):
            state["last_edit_at"] = now
            changed["last_edit_at"] = now
        if e.action == "commit":
            state["last_commit_at"] = now
    if tool_name == "Bash":
        resp = tool_response if isinstance(tool_response, dict) else {"stdout": str(tool_response)}
        ok = is_test_success(tool_input.get("command", ""), resp.get("stdout", ""), resp.get("stderr", ""))
        if ok is True and not resp.get("interrupted"):
            state["tests_passed_at"] = now
            changed["tests_passed_at"] = now
        elif ok is False:
            state["tests_failed_at"] = now
    return changed


def register_rules(state: dict, new_rules: list[Rule]) -> list[Rule]:
    added = []
    existing = {r["id"] for r in state["rules"]}
    for r in new_rules:
        if r.id not in existing:
            state["rules"].append(r.as_dict())
            existing.add(r.id)
            added.append(r)
    return added


def _target_covers(granted: str, target: str) -> bool:
    """Does a confirmation granted for `granted` cover an effect on `target`? (same path or inside the granted dir)"""
    g = granted.strip("/").lower()
    t = target.strip("/").lower()
    return t == g or t.startswith(g + "/") or t.endswith("/" + g) or ("/" + g + "/") in ("/" + t + "/")


# explicit go-ahead for an action class that is under a confirm rule: "yes, delete build/", "go ahead and push"
_GOAHEAD = re.compile(r"\b(?:go ahead(?: and)?|yes,?(?: please)?|please|ok(?:ay)?,?|sure,?|you can|feel free to|approved?(?: to)?|confirmed?,?)\s+"
                      r"(?:(?:and|to)\s+)?(delete|remove|rm|wipe|drop|push|force[- ]?push|install|deploy|commit|run|modify|edit|change)\b"
                      r"(?:\s+(?:the\s+|it\s+|them\s+|those\s+|that\s+)?(?:files?\s+|dirs?\s+|directory\s+|folders?\s+)?(?:under\s+|in\s+)?([`\"']?[\w./*~-]+[`\"']?))?", re.I)
_VERB_ACTION = {"delete": "delete", "remove": "delete", "rm": "delete", "wipe": "delete", "drop": "delete", "push": "push",
                "force push": "force_push", "force-push": "force_push", "forcepush": "force_push", "install": "install",
                "deploy": "deploy", "commit": "commit", "run": "run", "modify": "modify", "edit": "modify", "change": "modify"}
_NOISE_TARGETS = {"it", "them", "that", "those", "this", "anything", "everything", "the", "a", "now", "please", "yes"}


def _expire_holds(state: dict, prompt: str) -> list[str]:
    """Temporary holds ("don't change anything yet") lapse at the next user turn that asks for work."""
    from .rules import extract_rules
    new_hold = any(r.expires == "next_turn" for r in extract_rules(prompt))
    if new_hold or AFFIRM.match(prompt.strip()) or NEGATE.match(prompt.strip()) or len(prompt.split()) < 3:
        return []
    expired = []
    for r in state.get("rules", []):
        if r.get("active", True) and r.get("expires") == "next_turn" and r.get("created_turn", 0) < state["turn"]:
            r["active"] = False
            r["expired_turn"] = state["turn"]
            expired.append(r["id"])
    return expired


def handle_prompt(prompt: str, state: dict, cfg: Config) -> dict:
    """UserPromptSubmit: extract constraints, resolve pending confirmations, handle holdfast: commands."""
    state["turn"] = state.get("turn", 0) + 1
    out = {"added": [], "granted": None, "denied": None, "dropped": [], "listed": False, "expired": [], "goahead": []}
    p = prompt.strip()
    out["expired"] = _expire_holds(state, p)

    for m in DROP.finditer(p):
        rid = m.group(1)
        for r in state["rules"]:
            if r["id"].lower() == rid.lower():
                r["active"] = False
                out["dropped"].append(rid)
    if LIST.search(p):
        out["listed"] = True

    pending = state.get("pending_confirm")
    if pending:
        if AFFIRM.match(p):
            state.setdefault("confirmations", {})[pending["rule_id"]] = {"granted_at": time.time(), "consumed": False, "mode": "once", "turn": state["turn"]}
            out["granted"] = pending
            state["pending_confirm"] = None
        elif NEGATE.match(p):
            out["denied"] = pending
            state["pending_confirm"] = None

    # proactive grants: the user explicitly tells the agent to perform a confirm-guarded action
    for m in _GOAHEAD.finditer(p):
        act = _VERB_ACTION.get(m.group(1).lower().replace("-", " "), None)
        if not act:
            continue
        tgt = (m.group(2) or "").strip("`\"' ")
        if tgt.lower() in _NOISE_TARGETS:
            tgt = ""
        for r in state.get("rules", []):
            if r.get("active", True) and r.get("kind") == "confirm" and action_matches(r["action"], act):
                state.setdefault("confirmations", {})[r["id"]] = {"granted_at": time.time(), "consumed": False, "mode": "once",
                                                                  "turn": state["turn"], "target": tgt, "via": "goahead"}
                out["goahead"].append({"rule_id": r["id"], "action": act, "target": tgt})

    rules = extract_rules(p, source="user", turn=state["turn"])
    if cfg.llm_extract and not rules and not AFFIRM.match(p) and len(p) > 15 and has_constraint_cue(p):
        from .rules import extract_rules_llm
        rules = extract_rules_llm(p, turn=state["turn"])
    out["added"] = register_rules(state, rules)
    return out


def context_block(state: dict, header: str = "Holdfast constraint guard is active") -> str:
    rules = [Rule.from_dict(r) for r in state.get("rules", []) if r.get("active", True)]
    if not rules:
        return ""
    lines = [f"{header}. These constraints are enforced mechanically at tool-call time (they survive compaction and apply to subagents); a blocked call returns the rule id:"]
    for r in rules:
        lines.append(f"- {r.id}: {r.describe()}  (from: \"{r.text}\")")
    return "\n".join(lines)
