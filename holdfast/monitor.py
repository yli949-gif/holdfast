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

_RETRACT = re.compile(r"^\s*\w+[,!.]?\s+(?:wait|no|nope|don't|do not|hold on|stop|not yet|actually no|never mind)\b", re.I)

DROP = re.compile(r"holdfast:?\s*(?:drop|remove|delete|disable)\s+(?:rule\s+)?(R-[0-9a-f]{6})", re.I)
RESET = re.compile(r"holdfast:?\s*reset\b", re.I)
LIST = re.compile(r"holdfast:?\s*(?:list|rules|status)\b", re.I)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    """Runtime knobs, read from the environment at construction time (so hooks and tests can override per call)."""
    opaque_policy: str = field(default_factory=lambda: _env("HOLDFAST_OPAQUE_POLICY", "ask"))       # ask | deny | allow  (uncertain matches)
    confirm_via: str = field(default_factory=lambda: _env("HOLDFAST_CONFIRM_VIA", "deny"))           # deny (instruct agent to ask in chat) | ask (permission prompt)
    inject_context: bool = field(default_factory=lambda: _env("HOLDFAST_INJECT", "1") != "0")        # re-anchor rules into context on session start/compact/subagent
    llm_extract: bool = field(default_factory=lambda: _env("HOLDFAST_LLM_EXTRACT", "0") == "1")      # recall fallback via `claude -p`
    enforce: bool = field(default_factory=lambda: _env("HOLDFAST_ENFORCE", "1") != "0")              # 0 = observe-only (log decisions, never block)
    exempt_created: bool = field(default_factory=lambda: _env("HOLDFAST_EXEMPT_CREATED", "1") != "0")      # deleting a file the agent created this session is not a loss
    exempt_artifacts: bool = field(default_factory=lambda: _env("HOLDFAST_EXEMPT_ARTIFACTS", "1") != "0")  # __pycache__/.pytest_cache/*.pyc are not protected content
    fail_closed: bool = field(default_factory=lambda: _env("HOLDFAST_FAIL_CLOSED", "1") != "0")      # internal error in PreToolUse → deny (0 = allow)


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

_GUARD_FILES = re.compile(r"(?:^|/)(?:\.holdfast(?:/|$)|\.claude/settings(?:\.local)?\.json$|\.claude/hooks(?:/|$)"
                          r"|\.git/holdfast(?:/|$)|\.git/hooks(?:/|$)|\.git/config$|\.git/info/attributes$)")
_ARTIFACTS = re.compile(r"(?:^|/)(?:__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.tox|\.nox|node_modules/\.cache|\.cache)(?:/|$)|\.py[co]$")
_MUTATE = ("edit", "write", "delete", "discard", "move")
# the agent driving Holdfast's own entry points (hook, state store, installer) could confirm or drop its own rules
_SELF_INVOKE = re.compile(r"holdfast[/.]hook\b|holdfast/(?:state|monitor|cli)\.py|-m\s*holdfast\b"
                          r"|\b(?:import|from)\s+holdfast\b|__import__\(\s*['\"]holdfast|(?:^|[\s;&|(`'\"])holdfast\s+(?:install|uninstall)\b")


_WHOLE_TREE = re.compile(r"(?:^|\s)['\"]?(?:\.{1,2}/?|/|~/?|\$HOME/?|\$\{HOME\}/?|\$PWD/?|\$\(pwd\)/?)['\"]?(?=\s|$|;|&|\|)")
_FIND_FILTER = re.compile(r"\bfind\b.*\s-(?:i?name|i?path|i?regex|type|newer|mtime|mmin|size|empty|user|perm)\b")


_MANIFEST = re.compile(r"(?:^|/)(?:requirements[\w.-]*\.(?:txt|in)|constraints[\w.-]*\.txt|pyproject\.toml|setup\.(?:py|cfg)|Pipfile|"
                       r"package\.json|Gemfile|go\.mod|Cargo\.toml|composer\.json|environment\.ya?ml|pom\.xml|build\.gradle(?:\.kts)?)$")
# a requirement-spec line: requests>=2 / "tenacity>=8.2", / "lodash": "^4.17" / tenacity = "^8" / gem 'x'
_REQ_LINE = re.compile(r"""^\s*(?:-\s*)?["']?[A-Za-z0-9@][\w.\-/\[\],]*["']?\s*(?:===?|>=|<=|~=|!=|>|<|@\s)\s*[\w.*]"""
                       r"""|^\s*(?!["']?(?:version|requires-python|python_requires|python|node|npm|engines|target-version)["']?\s*[:=])["']?[@\w.\-/]+["']?\s*[:=]\s*["'\{]\s*(?:version\s*=\s*["'])?[\^~<>=*\d]"""
                       r"""|^\s*gem\s+['"]|^\s*<artifactId>|^\s*(?:implementation|api|compile)\s*[('"]|^\s*require\s+[\w.\-/]+\s+v\d""")
_QUOTED_NAME = re.compile(r"""^\s*["'][A-Za-z0-9@][\w.\-/\[\]]*["']\s*,?\s*$""")
_BARE_REQ = re.compile(r"^\s*[A-Za-z][\w.\-\[\],]*\s*(?:;.*)?$")
_DEP_MARKER = re.compile(r"dependencies|requires|install_requires|devDependencies|peerDependencies|optionalDependencies|"
                         r"\[tool\.poetry\.(?:dev-)?dependencies\]|\brequire\b|<dependency>|\bgem\s+['\"]|^\s*require\s", re.I | re.M)


def _manifest_effects(tool_name: str, tool_input: dict, effects: list[Effect]) -> list[Effect]:
    """Adding a dependency by editing its manifest is installing it later (REVIEW B18/O4): an edit/write of a
    dependency manifest yields an uncertain `install` effect — always for requirements files and shell writes
    (content not visible), and for Edit/Write of other manifests only when the new text touches a dependency section."""
    out = []
    for e in effects:
        if e.action not in ("edit", "write") or e.kind != "path" or e.target in (UNKNOWN, EVERYTHING) or not _MANIFEST.search(e.target):
            continue
        name = e.target.rsplit("/", 1)[-1]
        new_text = ""
        if tool_name in ("Edit", "Write", "MultiEdit"):
            new_text = (tool_input.get("new_string") or tool_input.get("content") or "") + "".join(
                (x.get("new_string") or "") for x in tool_input.get("edits", []) if isinstance(x, dict))
        if tool_name in ("Edit", "Write", "MultiEdit"):
            old_text = (tool_input.get("old_string") or "") + "".join(
                (x.get("old_string") or "") for x in tool_input.get("edits", []) if isinstance(x, dict))
            if tool_name == "Write":
                try:
                    old_text = open(tool_input.get("file_path", "")).read()
                except OSError:
                    old_text = ""
            added = [l for l in new_text.splitlines() if l.strip() and l.strip() not in {x.strip() for x in old_text.splitlines()}]
            is_req = name.startswith(("requirements", "constraints"))
            in_dep_section = bool(_DEP_MARKER.search(old_text + "\n" + new_text))
            if not any(_REQ_LINE.match(l) or (is_req and _BARE_REQ.match(l) and not l.lstrip().startswith("#"))
                       or (in_dep_section and _QUOTED_NAME.match(l)) for l in added) \
                    and not _DEP_MARKER.search("\n".join(added)):
                continue   # removing / re-pinning nothing new, or editing tool config: not adding a dependency
        out.append(Effect("install", e.target, kind="manifest", detail=f"{e.detail} (edits dependency manifest {name})", certain=False))
    return out


_DOC = re.compile(r"(?:^|/)docs?/|\.(?:md|markdown|rst|adoc|txt)$", re.I)


def _is_doc(e: Effect) -> bool:
    return e.kind == "path" and e.target not in (UNKNOWN, EVERYTHING) and bool(_DOC.search(e.target)) \
        and not re.search(r"(?:^|/)requirements[\w.-]*\.txt$", e.target)


def _is_guard_file(target: str) -> bool:
    return target not in (UNKNOWN, EVERYTHING) and bool(_GUARD_FILES.search(target))


def _guard_hit(e: Effect, root: str | None, sdir: str | None) -> bool:
    """Does a mutating effect touch the guard's state, its hook configuration, or a git code-execution channel?
    Pattern match on the normalised target, plus a real-path check against wherever the state actually lives
    (HOLDFAST_DIR outside the project, REVIEW-2 N11) and against its ancestors for deletes/moves."""
    if e.action not in _MUTATE or e.kind not in ("path", "opaque"):
        return False
    if _is_guard_file(e.target):
        return True
    not_git = not re.match(r"\s*git\b", e.detail or "")
    if e.target == EVERYTHING and e.action in ("delete", "move") and not_git and _WHOLE_TREE.search(e.detail or "") \
            and not _FIND_FILTER.search(e.detail or ""):
        return True             # `rm -rf .` / `rm -rf /` / `rm -rf ~`: the whole project, including the guard's state
    if not sdir or e.target in (UNKNOWN, EVERYTHING) or "*" in e.target:
        return False
    t = e.target if e.target.startswith("/") else os.path.join(root or "/", e.target)
    try:
        real = os.path.realpath(os.path.expanduser(t))
    except (OSError, ValueError):
        return False
    if real == sdir or real.startswith(sdir + os.sep):
        return True
    # deleting/moving an ancestor (`rm -rf .`, `mv .git /tmp`) takes the state with it; git's own commands never
    # touch the git directory (`git clean -fdx` on "." is not an attack on .git/holdfast)
    return e.action in ("delete", "move", "discard") and sdir.startswith(real.rstrip(os.sep) + os.sep) and not_git


def _state_dir_real(root: str | None) -> str | None:
    try:
        from .state import state_dir
        return os.path.realpath(state_dir(root, create=False))
    except OSError:
        return None


def _is_artifact(target: str) -> bool:
    return target not in (UNKNOWN, EVERYTHING) and bool(_ARTIFACTS.search(target))


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
    effects = effects + _manifest_effects(tool_name, tool_input, effects)
    rules = rules_of(state)
    best = Decision("allow", effects=effects)
    now = time.time()

    # Built-in: the guard's own state and the hook configuration are never writable by the agent while Holdfast
    # is installed (REVIEW.md critical: `echo {} > .holdfast/sessions/<sid>.json` emptied every rule).
    sdir = _state_dir_real(root)
    for e in effects:
        if _guard_hit(e, root, sdir):
            return Decision("deny", f"Holdfast: {e.action} on {e.target} is blocked — the guard's own state, hook "
                                    "configuration and git hook/config files are read-only for the agent. "
                                    "Ask the user to change them.",
                            "builtin-selfprotect", effects, matched=[{"rule_id": "builtin-selfprotect", "effect": e.as_dict(),
                                                                       "decision": "deny", "uncertain": False}])
    if tool_name == "Bash" and _SELF_INVOKE.search(tool_input.get("command", "") or ""):
        return Decision("deny", "Holdfast: running Holdfast's own hook, state store or installer from a tool call is "
                                "blocked — only the user can change rules or confirmations.", "builtin-selfprotect", effects)
    if state.get("integrity_lost") and any(e.action not in ("read", "run", "test") for e in effects):
        return Decision("deny", "Holdfast: this session's guard state was lost or corrupted "
                                f"({state['integrity_lost']}), so its constraints cannot be verified. Mutating calls are "
                                "blocked until the user re-states their constraints and says 'holdfast: reset'.",
                        "builtin-integrity", effects)

    created = set(state.get("created", []))
    to_consume: list[dict] = []
    for rule in rules:
        for e in effects:
            if e.action in ("delete", "move") and cfg.exempt_created and e.target in created and rule.action in ("delete", "modify", "destructive"):
                continue   # the agent created this file in this session: removing it loses nothing of the user's
            if e.action in ("delete", "move", "edit", "write") and cfg.exempt_artifacts and _is_artifact(e.target) and rule.action != "*":
                continue   # regenerable caches (__pycache__, .pytest_cache, *.pyc) are not what "never delete" protects
            if rule.action == "network" and e.action in ("push", "force_push") and "(local remote)" in (e.detail or "") \
                    and not (rule.scope.pattern or rule.scope.paths or rule.scope.branches):
                continue   # pushing to a remote that is a local path is not network access
            if _is_doc(e) and rule.scope.pattern and re.search(r"prod", rule.scope.pattern) and e.action in ("edit", "write"):
                continue   # docs/production-checklist.md is documentation about production, not production
            m = rule.matches(e)
            if m is False and e.action == "exec_opaque" and not e.certain:
                # an opaque execution (script file, unreadable make/npm target, decoded blob, eval of a variable)
                # could perform any action: it matches every active rule as *uncertain*
                m = None
            if m is False:
                continue
            # an effect derived heuristically but with a concrete, in-scope target (os.remove('x'), heredoc writing
            # src/core/util.py) is treated as certain: the target is known, only the mechanism was indirect
            # (a dependency-manifest edit is semantically uncertain even though its path is known)
            concrete = e.target not in (UNKNOWN, EVERYTHING) and "*" not in e.target and m is True and e.kind != "manifest"
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
                    if (state.get("tests_partial_at") or 0) > (state.get("tests_passed_at") or 0):
                        why = "The last green run was partial (a file, -k/-m, --lf, --deselect...); run the whole suite."
                    elif state.get("tests_passed_at"):
                        why = "Tests have not passed since the last edit or tree-changing git command; run the test suite first."
                    else:
                        why = "No passing test run recorded in this session; run the test suite first."
                    cand = Decision("deny", f"Holdfast rule {rule.id}: {rule.describe()}. {why} "
                                            "(Run the test command on its own — not chained with other commands.)",
                                    rule.id, effects)
                elif rule.prerequisite == "user_confirm":
                    rule.kind = "confirm"  # fallthrough semantics
            if rule.kind == "confirm" and not uncertain and cand is None:
                grant = state.get("confirmations", {}).get(rule.id)
                if grant and _grant_covers(grant, e, state):
                    if grant.get("mode") != "always":
                        to_consume.append(grant)   # consumed only if the call as a whole is allowed (REVIEW-2 N2)
                    continue
                pend = state.get("pending_confirm")
                if pend and pend.get("tool_use_id") == tool_use_id and pend.get("rule_id") == rule.id and tool_use_id:
                    if e.target not in pend.setdefault("targets", []):
                        pend["targets"].append(e.target)
                else:
                    state["pending_confirm"] = {"rule_id": rule.id, "effect": e.as_dict(), "targets": [e.target],
                                                "action": e.action, "tool_use_id": tool_use_id, "asked_at": now,
                                                "turn": state.get("turn", 0), "tool_name": tool_name, "tool_input": tool_input}
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
    if best.decision == "allow":
        for g in to_consume:
            g["consumed"] = True
            g["consumed_by"] = tool_use_id
    return best


def _grant_covers(grant: dict, e: Effect, state: dict) -> bool:
    """A one-shot grant is valid only during the user turn it was given in, and only for the targets the user was
    asked about (or named). An untargeted go-ahead never covers a delete (REVIEW-2 N2/N3)."""
    if grant.get("mode") == "always":
        return True
    if grant.get("consumed"):
        return False
    if grant.get("turn") is not None and grant["turn"] != state.get("turn"):
        return False
    targets = [t for t in (grant.get("targets") or ([grant["target"]] if grant.get("target") else [])) if t]
    if not targets:
        return e.action not in ("delete", "move", "discard", "destructive")
    return any(_target_covers(t, e.target) for t in targets)


def observe_post(tool_name: str, tool_input: dict, tool_response: dict | str, state: dict, cwd: str, root: str | None,
                 tool_use_id: str = "", failed: bool = False) -> dict:
    """Update temporal state after a tool ran (PostToolUse, or PostToolUseFailure with failed=True)."""
    from .shell import is_test_success
    from .testruns import FULL, PARTIAL, test_run_kind, tree_changing_git
    effects = effect_from_tool(tool_name, tool_input, cwd, root)
    now = time.time()
    changed = {}
    for e in effects:
        if e.action in ("edit", "write", "delete", "move", "discard"):
            state["last_edit_at"] = now
            changed["last_edit_at"] = now
        if e.action == "commit":
            state["last_commit_at"] = now
    started = state.get("test_started", {}).pop(tool_use_id, None) if tool_use_id else None
    if tool_name == "Bash":
        resp = tool_response if isinstance(tool_response, dict) else {"stdout": str(tool_response)}
        cmd = tool_input.get("command", "")
        if tree_changing_git(cmd):
            # pull/merge/rebase/stash pop/checkout rewrite the tree: an earlier green run is stale (REVIEW-2 N5)
            state["last_edit_at"] = now
            changed["last_edit_at"] = now
        ok = is_test_success(cmd, resp.get("stdout", ""), resp.get("stderr", ""))
        if failed and ok is not None:
            ok = False
        kind = test_run_kind(cmd, cwd, root) if ok is not None else ""
        if ok is True and kind == FULL and not resp.get("interrupted") and not _prints_fake_summary(cmd):
            # stamped with the time the run *started*: an edit that lands while it runs makes it stale (N5 race)
            ts = started if started else now
            state["tests_passed_at"] = ts
            changed["tests_passed_at"] = ts
        elif ok is True and kind == PARTIAL:
            state["tests_partial_at"] = now
            changed["tests_partial_at"] = now
        elif ok is False:
            state["tests_failed_at"] = now
            if kind == FULL:
                state["tests_passed_at"] = None
                changed["tests_passed_at"] = None
    # files that did not exist before this call now exist: they were created by the agent (see hook PreToolUse)
    tid = tool_use_id or state.get("_last_tool_use_id")
    pend = state.get("pending_created", {}).pop(tid, None) if tid else None
    if pend and not failed:
        state.setdefault("created", [])
        for pth in pend:
            if pth not in state["created"]:
                state["created"].append(pth)
        changed["created"] = pend
    return changed


_FAKE = re.compile(r"\b(?:echo|printf)\b[^|;&]*\b(?:passed|PASS|ok)\b", re.I)


def _prints_fake_summary(cmd: str) -> bool:
    """`pytest --version; echo '3 passed'` must not count as a green run (REVIEW.md)."""
    return bool(_FAKE.search(cmd))


def note_new_paths(effects: list[Effect], state: dict, root: str | None, tool_use_id: str) -> None:
    """PreToolUse: remember which write targets do not exist yet; PostToolUse moves them to `created`."""
    if not root or not tool_use_id:
        return
    new = []
    for e in effects:
        if e.action in ("write", "edit") and e.target not in (UNKNOWN, EVERYTHING) and not e.target.startswith("/") \
                and "*" not in e.target and not os.path.exists(os.path.join(root, e.target)):
            new.append(e.target)
    if new:
        state.setdefault("pending_created", {})[tool_use_id] = new
    state["_last_tool_use_id"] = tool_use_id


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
    """Does a confirmation granted for `granted` cover an effect on `target`? Same path or inside it — prefix only
    (a go-ahead for `build` does not cover `src/build`, REVIEW-2 N3)."""
    g = granted.strip().strip("/").lower()
    t = target.strip().strip("/").lower()
    if g in ("", "."):
        return False
    if g.startswith("./"):
        g = g[2:]
    return t == g or t.startswith(g + "/")


# explicit go-ahead for an action class that is under a confirm rule: "yes, delete build/", "go ahead and push"
_GOAHEAD = re.compile(r"\b(?:go ahead(?: and)?|yes,?(?: please)?|please|ok(?:ay)?,?|sure,?|you can|feel free to|approved?(?: to)?|confirmed?,?)\s+"
                      r"(?:(?:and|to)\s+)?(delete|remove|rm|wipe|drop|push|force[- ]?push|install|deploy|commit|run|modify|edit|change)\b"
                      r"(?:\s+(?:the\s+|it\s+|them\s+|those\s+|that\s+)?(?:files?\s+|dirs?\s+|directory\s+|folders?\s+)?(?:under\s+|in\s+)?([`\"']?[\w./*~-]+[`\"']?))?", re.I)
_VERB_ACTION = {"delete": "delete", "remove": "delete", "rm": "delete", "wipe": "delete", "drop": "delete", "push": "push",
                "force push": "force_push", "force-push": "force_push", "forcepush": "force_push", "install": "install",
                "deploy": "deploy", "commit": "commit", "run": "run", "modify": "modify", "edit": "modify", "change": "modify"}
_NOISE_TARGETS = {"it", "them", "that", "those", "this", "anything", "everything", "the", "a", "an", "now", "please", "yes",
                  "file", "files", "both", "all", "to", "dir", "directory", "folder", "stuff", "things", "everything", "one", "ones"}
# words that turn "ok ..." into something other than a go-ahead ("ok, but first show me", "sure, I'll do it myself later")
_AFFIRM_VETO = re.compile(r"\b(?:but|first|later|myself|except|instead|not|don't|dont|do not|wait|hold|before|after|show|"
                          r"what|why|how|which|tell|explain|move on|let's|lets|anyway|however|unless|if)\b|\?", re.I)
_GOAHEAD_VETO = re.compile(r"\b(?:except|but not|other than|apart from|besides|without|unless|never|don't|do not|not)\b|\?", re.I)


def is_affirmation(p: str) -> bool:
    """A reply that grants a pending confirmation: a short, single-line, unhedged yes."""
    p = p.strip()
    if not p or "\n" in p or len(p.split()) > 8 or not AFFIRM.match(p) or _RETRACT.match(p):
        return False
    return not _AFFIRM_VETO.search(p)


_QUESTION = re.compile(r"\?\s*$|^\s*(?:what|why|how|where|when|who|which|whose|is|are|was|were|does|do|did|could|would|should|"
                       r"can you (?:show|tell|explain|list|describe|remind)|tell me|show me|explain|describe|walk me through|remind me)\b",
                       re.I)
_PROCEED = re.compile(r"\b(?:go ahead|proceed|do it|go for it|implement (?:it|this|that|the plan)|start (?:on it|implementing|now)|"
                      r"carry on|make the changes?|apply (?:it|the (?:plan|changes?|fix))|looks good,? (?:go|ship|do)|plan (?:is )?approved|"
                      r"you can (?:now )?(?:start|go|proceed|edit|change|implement|make))\b", re.I)
_WORK = re.compile(r"^\s*(?:(?:now|ok(?:ay)?|great|thanks|alright|cool|good|also|and|then|next|so)[,.!]?\s+)*(?:please\s+)?"
                   r"(?:fix|add|implement|change|update|refactor|make|create|write|edit|remove|delete|tidy|clean|rename|move|build|"
                   r"set up|install|bump|replace|convert|migrate|port|improve|optimi[sz]e|get rid|modify|patch|rewrite|split|merge|"
                   r"push|commit|rebase|deploy|publish|ship|revert|restore|upgrade|resolve)\b",
                   re.I)
_UNTIL_EXPLICIT = re.compile(r"\buntil\s+(?:i|we|you get my|after i)\b|\buntil\s+(?:approved|reviewed|confirmed|signed)|\bwait for (?:my|me)",
                             re.I)
_ACTION_VERB = {"push": r"push", "force_push": r"force[- ]?push", "commit": r"commit", "deploy": r"deploy|release|ship",
                "install": r"install", "delete": r"delete|remove|rm|clean ?up|get rid"}


def _work_request(p: str) -> bool:
    return any(_WORK.match(seg) for seg in re.split(r"(?<=[.!;\n])\s+|\s+(?:and then|then)\s+", p) if seg.strip())


def _releases(hold: dict, p: str) -> bool:
    """Does user prompt `p` release temporary hold `hold`? (REVIEW-2 N7/N8)"""
    if _QUESTION.search(p) or NEGATE.match(p):
        return False                                  # a question about the code is not the go-ahead
    action = hold.get("action", "modify")
    if action in _ACTION_VERB:
        # "don't push for now": only a go-ahead for that very action lifts it
        verb = _ACTION_VERB[action]
        return bool(re.search(rf"\b(?:go ahead and|now|please|you can|ok,?|yes,?|feel free to)?\s*(?:{verb})\b", p, re.I)) and \
            not re.search(rf"\b(?:don't|do not|not|never|no)\s+(?:\w+\s+)?(?:{verb})", p, re.I)
    if _UNTIL_EXPLICIT.search(hold.get("text", "")):
        return bool(_PROCEED.search(p))               # "until I've reviewed the plan" needs an explicit go-ahead
    return bool(_PROCEED.search(p)) or _work_request(p)


def _expire_holds(state: dict, prompt: str) -> list[str]:
    """Temporary holds ("don't change anything yet") lapse at the next user turn that asks for that work."""
    from .rules import extract_rules
    if any(r.expires == "next_turn" for r in extract_rules(prompt)):
        return []
    expired = []
    for r in state.get("rules", []):
        if r.get("active", True) and r.get("expires") == "next_turn" and r.get("created_turn", 0) < state["turn"] \
                and _releases(r, prompt.strip()):
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
    if RESET.search(p) and state.get("integrity_lost"):
        state["integrity_reset"] = state.pop("integrity_lost")

    pending = state.get("pending_confirm")
    if pending:
        if is_affirmation(p):
            state.setdefault("confirmations", {})[pending["rule_id"]] = {
                "granted_at": time.time(), "consumed": False, "mode": "once", "turn": state["turn"], "via": "reply",
                "targets": list(pending.get("targets") or [pending["effect"]["target"]])}
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
        # the rest of the clause can take the go-ahead back: "you can delete anything except src",
        # "you can remove src from the docs"
        clause = re.split(r"[.;!\n]", p[m.start():], maxsplit=1)[0]
        if _GOAHEAD_VETO.search(clause) or re.match(r"\s+from\b", p[m.end():]):
            continue
        tgt = (m.group(2) or "").strip("`\"' ").rstrip(".,")
        if tgt.lower() in _NOISE_TARGETS:
            tgt = ""
        if not tgt and act in ("delete", "move"):
            continue                   # never an untargeted go-ahead for destruction
        for r in state.get("rules", []):
            if out["granted"] and r["id"] == out["granted"]["rule_id"] and action_matches(r["action"], act):
                # a fresh reply-grant for the pending call is extended, never replaced ("yes, delete the file")
                g = state["confirmations"][r["id"]]
                if tgt and tgt not in g["targets"]:
                    g["targets"].append(tgt)
                out["goahead"].append({"rule_id": r["id"], "action": act, "target": tgt})
                continue
            if r.get("active", True) and r.get("kind") == "confirm" and action_matches(r["action"], act):
                state.setdefault("confirmations", {})[r["id"]] = {"granted_at": time.time(), "consumed": False, "mode": "once",
                                                                  "turn": state["turn"], "target": tgt,
                                                                  "targets": [tgt] if tgt else [], "via": "goahead"}
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
