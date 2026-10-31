"""Rule schema and natural-language → rule extraction."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
import fnmatch
import hashlib
import json
import posixpath
import re
import time
from typing import Optional

from .effects import Effect, EVERYTHING, UNKNOWN, action_matches

KINDS = ("forbid", "confirm", "require_before", "freeze")


@dataclass
class Scope:
    paths: list[str] = field(default_factory=list)      # globs relative to project root; "" = any
    branches: list[str] = field(default_factory=list)   # e.g. ["origin/main", "*/main"]
    pattern: str = ""                                   # regex on effect target (urls, packages, free-form commands)
    command: str = ""                                   # regex on the raw command / detail

    def is_universal(self) -> bool:
        return not (self.paths or self.branches or self.pattern or self.command)

    def matches(self, e: Effect) -> Optional[bool]:
        """True = in scope, False = out of scope, None = can't tell (unknown target)."""
        if self.is_universal():
            return True
        if self.command and re.search(self.command, e.detail or e.target, re.I):
            return True
        if self.pattern and e.target not in (UNKNOWN,) and re.search(self.pattern, e.target, re.I):
            return True
        if self.branches and e.kind == "branch":
            for b in self.branches:
                if fnmatch.fnmatch(e.target, b) or fnmatch.fnmatch(e.target, "*/" + b):
                    return True
            return False
        if self.paths and e.kind in ("path", "opaque"):
            if e.target == EVERYTHING:
                return True
            if e.target == UNKNOWN:
                return None
            for g in self.paths:
                if _path_in_scope(e.target, g):
                    return True
            return False
        if e.target == UNKNOWN:
            return None
        return False


def _path_in_scope(target: str, glob: str) -> bool:
    t = posixpath.normpath(target)
    g = glob.rstrip("/")
    if t == g or fnmatch.fnmatch(t, g) or fnmatch.fnmatch(t, g + "/*") or fnmatch.fnmatch(t, g + "/**"):
        return True
    if "**" in g and fnmatch.fnmatch(t, g.replace("**/", "")):
        return True
    # recursive glob: a/**/b.py should match a/x/y/b.py
    if "**" in g:
        rx = re.escape(g).replace(r"\*\*/", "(.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        if re.fullmatch(rx, t):
            return True
    # deleting/moving a parent directory of the protected scope counts
    gd = g.split("*")[0].rstrip("/")
    if t in (".", "") and gd:
        return True
    if gd and (gd == t or gd.startswith(t.rstrip("/") + "/")):
        return True
    return False


@dataclass
class Rule:
    id: str
    kind: str                     # forbid | confirm | require_before | freeze
    action: str                   # effect action (or parent in ACTION_TREE)
    scope: Scope = field(default_factory=Scope)
    text: str = ""                # original natural-language constraint
    source: str = "config"        # config | user | extracted | llm
    mode: str = "once"            # confirm: once (one confirmation per matching call) | always (standing approval)
    prerequisite: str = ""        # require_before: tests_pass | commit | user_confirm
    created_at: float = field(default_factory=time.time)
    created_turn: int = 0
    active: bool = True

    def as_dict(self) -> dict:
        d = asdict(self)
        return d

    @staticmethod
    def from_dict(d: dict) -> "Rule":
        d = dict(d)
        sc = d.pop("scope", {}) or {}
        if isinstance(sc, str):
            sc = {"paths": [sc]}
        r = Rule(**{k: v for k, v in d.items() if k in Rule.__dataclass_fields__})
        r.scope = Scope(**{k: v for k, v in sc.items() if k in Scope.__dataclass_fields__})
        if r.kind not in KINDS:
            raise ValueError(f"unknown rule kind {r.kind}")
        return r

    def matches(self, e: Effect) -> Optional[bool]:
        if not self.active or not action_matches(self.action, e.action):
            return False
        return self.scope.matches(e)

    def describe(self) -> str:
        sc = []
        if self.scope.paths:
            sc.append("paths " + ", ".join(self.scope.paths))
        if self.scope.branches:
            sc.append("branches " + ", ".join(self.scope.branches))
        if self.scope.pattern:
            sc.append(f"matching /{self.scope.pattern}/")
        if self.scope.command:
            sc.append(f"commands /{self.scope.command}/")
        scope = (" on " + "; ".join(sc)) if sc else ""
        if self.kind == "forbid":
            return f"never {self.action}{scope}"
        if self.kind == "confirm":
            return f"ask the user before every {self.action}{scope} (one confirmation per action)" if self.mode == "once" else f"ask the user once before {self.action}{scope}"
        if self.kind == "require_before":
            return f"{self.action}{scope} only after {self.prerequisite.replace('_', ' ')}"
        if self.kind == "freeze":
            return f"no {self.action}{scope} after the task started (frozen)"
        return self.text


def make_id(text: str, kind: str, action: str) -> str:
    h = hashlib.sha1(f"{kind}|{action}|{text}".encode()).hexdigest()[:6]
    return f"R-{h}"


# ---------------------------------------------------------------------------
# Deterministic natural-language extractor.
# Coverage is intentionally narrow and precise; the LLM extractor (extract_llm)
# is the recall fallback. Every pattern here is covered by tests/test_extract.py.
# ---------------------------------------------------------------------------

_NEG = r"(?:do not|don't|dont|never|do\s+not\s+ever|no|avoid|must not|mustn't|should not|shouldn't|please don't|please do not)"
_DEL = r"(?:delete|remove|rm|erase|wipe|drop|destroy|nuke|clean up|clear out)"
_MOD = r"(?:touch|modify|edit|change|alter|update|rewrite|overwrite|mess with)"
_TESTS = r"(?:the\s+)?(?:unit\s+|existing\s+)?tests?(?:\s+files?|\s+suite|\s+directory|\s+folder)?"
_PATH = r"(?:`([^`]+)`|\"([^\"]+)\"|'([^']+)'|(\S*/\S*|\S+\.\w{1,5}|[\w.-]+/))"

_PATH_ALIASES = {
    "tests": ["tests/**", "test/**", "**/test_*.py", "**/*_test.py", "**/*.test.*", "**/*.spec.*", "**/tests/**", "**/__tests__/**"],
    "test": ["tests/**", "test/**", "**/test_*.py", "**/*_test.py", "**/*.test.*", "**/*.spec.*", "**/tests/**"],
    "the tests": ["tests/**", "test/**", "**/test_*.py", "**/*_test.py", "**/*.test.*", "**/*.spec.*", "**/tests/**"],
    "config": ["config/**", "*.config.*", "*.cfg", "*.ini", "*.toml", "*.yaml", "*.yml", ".env*"],
    "the config": ["config/**", "*.config.*", "*.cfg", "*.ini", "*.toml", "*.yaml", "*.yml", ".env*"],
    "migrations": ["**/migrations/**"],
    "the database": ["*.db", "*.sqlite", "*.sqlite3", "db/**", "data/**"],
    "data": ["data/**"],
    "the data": ["data/**"],
    "docs": ["docs/**", "*.md"],
    "ci": [".github/**", ".gitlab-ci.yml", ".circleci/**"],
    "the ci": [".github/**", ".gitlab-ci.yml", ".circleci/**"],
    "lockfile": ["*.lock", "package-lock.json", "uv.lock", "poetry.lock"],
    "lock file": ["*.lock", "package-lock.json", "uv.lock", "poetry.lock"],
    "lockfiles": ["*.lock", "package-lock.json", "uv.lock", "poetry.lock"],
    "dependencies": ["requirements*.txt", "pyproject.toml", "package.json", "Cargo.toml", "go.mod"],
}


def _clean_path(p: str) -> str:
    p = p.strip().strip(",.;:)(").rstrip("/")
    if p.startswith("./"):
        p = p[2:]
    if not p:
        return p
    if "*" in p or p.endswith(".py") or "." in p.rsplit("/", 1)[-1]:
        return p
    return p + "/**"


def _scope_from_phrase(phrase: str) -> Scope:
    phrase = phrase.strip().strip(".,;:!").lower()
    phrase = re.sub(r"^(?:anything|any files?|files?|stuff|things?)\s+(?:in|under|inside|from|within)\s+", "", phrase)
    phrase = re.sub(r"^(?:the\s+)?(?:directory|folder|dir)\s+", "", phrase)
    if re.fullmatch(r"(?:any\s+|all\s+|the\s+|existing\s+)*(?:files?|anything|stuff|things?|code|everything|directories|folders)?", phrase):
        return Scope()
    if phrase in _PATH_ALIASES:
        return Scope(paths=_PATH_ALIASES[phrase])
    for alias, globs in _PATH_ALIASES.items():
        if re.fullmatch(rf"(?:the\s+)?{re.escape(alias)}(?:\s+(?:directory|folder|dir|files?))?", phrase):
            return Scope(paths=globs)
    m = re.search(r"(?:`([^`]+)`|\"([^\"]+)\"|'([^']+)')", phrase)
    if m:
        p = next(g for g in m.groups() if g)
        return Scope(paths=[_clean_path(p)])
    toks = [t for t in re.split(r"\s+", phrase) if t]
    cands = [t for t in toks if "/" in t or re.search(r"\.\w{1,5}$", t) or t.startswith(".")]
    if cands:
        return Scope(paths=[_clean_path(c) for c in cands])
    if re.search(r"\b(prod|production)\b", phrase):
        return Scope(pattern=r"prod")
    if toks and len(toks) <= 2 and re.fullmatch(r"[\w.-]+", toks[-1]):
        return Scope(paths=[_clean_path(toks[-1])])
    return Scope()


_REMOTE_WORDS = ("remote", "origin", "github", "the remote", "upstream", "gitlab")
_WITHOUT_ASKING = re.compile(r"\bwithout\s+(?:first\s+)?(?:asking|checking|confirming|consulting|(?:my\s+|explicit\s+|user\s+)?(?:approval|permission|confirmation|consent|ok|sign-?off))", re.I)


def _is_universal_phrase(rest: str) -> bool:
    """'anything', 'any files', 'the files', 'stuff' ... — but not 'the test files' or 'data files'."""
    r = rest.strip().strip(".,;:!").lower()
    return r == "" or re.fullmatch(r"(?:(?:any|all|the|existing|of)\s+)*(?:anything|files?|stuff|things?|everything)", r) is not None


def _branch_scope(name: str) -> Scope:
    b = name.strip("`'\".,")
    if b in _REMOTE_WORDS:
        return Scope()
    return Scope(branches=[b, "*/" + b])


def _confirm_scope(action: str, rest: str) -> Scope:
    """Scope for a confirm-before rule: branch-kind actions take a branch scope, others a path scope."""
    rest = rest.strip()
    if not rest:
        return Scope()
    if action in ("push", "force_push"):
        m = re.match(r"(?:directly\s+|straight\s+|anything\s+|code\s+)?(?:to|onto|into)\s+(?:the\s+)?[`'\"]?([\w./-]+)", rest)
        return _branch_scope(m.group(1)) if m else Scope()
    return _scope_from_phrase(rest)


def extract_rules(text: str, source: str = "extracted", turn: int = 0) -> list[Rule]:
    """Extract constraint rules from a natural-language prompt. Precision-first."""
    rules: list[Rule] = []
    t = " ".join(text.split())
    low = t.lower()

    def add(kind, action, scope=None, mode="once", prereq="", frag=""):
        r = Rule(id=make_id(frag or t, kind, action + str(scope)), kind=kind, action=action, scope=scope or Scope(),
                 text=frag or t, source=source, mode=mode, prerequisite=prereq, created_turn=turn)
        if all(x.id != r.id for x in rules):
            rules.append(r)

    # sentences
    for sent in re.split(r"(?<=[.!?\n])\s+|;\s+|\s+(?:and also|also)\s+", t):
        s = sent.strip()
        sl = s.lower()
        if not s:
            continue

        # explicit syntax: "holdfast: forbid delete data/**"
        m = re.match(r"holdfast:\s*(forbid|confirm|require_before|freeze)\s+(\w+)(?:\s+(.*))?$", sl)
        if m:
            kind, action, rest = m.group(1), m.group(2), (m.group(3) or "").strip()
            scope = Scope()
            prereq = ""
            if kind == "require_before":
                parts = rest.split(None, 1)
                prereq = parts[0] if parts else "tests_pass"
                rest = parts[1] if len(parts) > 1 else ""
            if rest:
                scope = Scope(paths=[p.strip() for p in rest.split(",")]) if not rest.startswith("/") else Scope(pattern=rest.strip("/"))
            add(kind, action, scope, prereq=prereq, frag=s)
            continue

        # confirm-before
        m = re.search(rf"\b(?:ask|check|confirm|verify|get (?:my )?(?:approval|permission|ok))\s+(?:with\s+)?(?:me\s+|the user\s+)?(?:first\s+)?(?:before|prior to)\s+(?:you\s+)?(\w+)(?:ing)?\b(?:\s+(?:any|anything|all|the|a|an)\s*)?(.*)?", sl)
        if m:
            verb, rest = m.group(1), (m.group(2) or "")
            action = _verb_action(verb)
            if action:
                add("confirm", action, _confirm_scope(action, rest), frag=s)
                continue
        m = re.search(rf"\b(?:always|please)?\s*(?:ask|check with me|confirm|get approval)\s+(?:me\s+)?(?:first\s+)?(?:before|prior to)\s+(?:any\s+|every\s+)?(?:(\w+)\s+)?(\w+)", sl)
        if m:
            a1 = _verb_action(m.group(1) or "") or _verb_action(m.group(2) or "")
            if a1:
                add("confirm", a1, Scope(), frag=s)
                continue
        m = re.search(r"\b(\w+)(?:s|ing)?\s+(?:requires?|needs?|must have)\s+(?:my\s+|explicit\s+|user\s+)?(?:confirmation|approval|sign-?off|permission)", sl)
        if m and _verb_action(m.group(1)):
            add("confirm", _verb_action(m.group(1)), Scope(), frag=s)
            continue

        # require-before: "only push after tests pass", "don't push until tests pass", "run tests before pushing"
        m = re.search(r"\b(?:only\s+)?(?:(?:do not|don't|never)\s+)?(push|commit|deploy|merge)(?:\s+\w+){0,4}?\s+(?:until|unless|after|once|when)\s+(?:the\s+|all\s+)?tests?\s+(?:pass|are passing|are green|succeed|go green)", sl)
        if m:
            add("require_before", m.group(1), Scope(), prereq="tests_pass", frag=s)
            continue
        m = re.search(r"\b(?:run|make sure|ensure)\s+(?:the\s+|all\s+)?tests?\s+(?:pass\s+)?(?:before|prior to)\s+(?:you\s+)?(push|commit|deploy|merg)(?:ing)?", sl)
        if m:
            act = {"merg": "push"}.get(m.group(1), m.group(1))
            add("require_before", act, Scope(), prereq="tests_pass", frag=s)
            continue
        m = re.search(r"\btests?\s+must\s+(?:pass|be green)\s+before\s+(?:you\s+|any\s+)?(push|commit|deploy|merg)", sl)
        if m:
            add("require_before", {"merg": "push"}.get(m.group(1), m.group(1)), Scope(), prereq="tests_pass", frag=s)
            continue

        # freeze: "don't modify tests after you start", "tests are frozen", "leave the tests as they are"
        m = re.search(rf"\b(?:{_NEG}\s+{_MOD}|leave|keep)\s+(?:the\s+)?(?:existing\s+)?(tests?[\w\s/]*?|[\w./*-]+)\s+(?:as (?:it is|they are|is)|frozen|unchanged)", sl)
        if m:
            add("freeze", "modify", _scope_from_phrase(m.group(1)), frag=s)
            continue
        m = re.search(rf"\b{_NEG}\s+{_MOD}\s+(.+?)\s+(?:after|once)\s+(?:you|we|the task|work)\s+(?:start|begin)", sl)
        if m:
            add("freeze", "modify", _scope_from_phrase(m.group(1)), frag=s)
            continue
        m = re.search(rf"\b([\w./*-]+|{_TESTS})\s+(?:is|are)\s+(?:frozen|read[- ]only|off[- ]limits|locked|sacred)", sl)
        if m:
            add("forbid", "modify", _scope_from_phrase(m.group(1)), frag=s)
            continue

        # forbid force push / push to branch / commit / install / network
        if re.search(rf"\b{_NEG}\s+(?:ever\s+)?force[- ]?push", sl) or re.search(r"\bno force[- ]?push", sl):
            add("forbid", "force_push", Scope(), frag=s)
            continue
        m = re.search(rf"\b{_NEG}\s+(?:ever\s+)?push(?:\s+(?:directly|straight|anything|code|commits?))?\s+(?:to|onto|into)\s+(?:the\s+)?[`'\"]?([\w./-]+)", sl)
        if m:
            kind = "confirm" if _WITHOUT_ASKING.search(sl) else "forbid"
            add(kind, "push", _branch_scope(m.group(1)), frag=s)
            continue
        if re.search(rf"\b{_NEG}\s+(?:ever\s+)?push\b", sl) or re.search(r"\bno push(?:es|ing)?\b", sl):
            add("confirm" if _WITHOUT_ASKING.search(sl) else "forbid", "push", Scope(), frag=s)
            continue
        if re.search(rf"\b{_NEG}\s+(?:ever\s+)?(?:make\s+(?:any\s+)?)?(?:git\s+)?commits?\b", sl) or re.search(r"\bno commits?\b", sl):
            add("forbid", "commit", Scope(), frag=s)
            continue
        if re.search(rf"\b{_NEG}\s+(?:ever\s+)?(?:install|add)\s+(?:(?:any|new|extra|additional|more|third[- ]party)\s+)*(?:packages?|dependenc(?:y|ies)|libraries|libs|modules|deps)", sl) or re.search(r"\bno (?:new |additional )?(?:dependencies|packages|deps)\b", sl):
            add("forbid", "install", Scope(), frag=s)
            continue
        if re.search(rf"\b{_NEG}\s+(?:ever\s+)?(?:make|do|use|perform|access)\s+(?:any\s+|the\s+)?(?:network|internet|http|web|external)\b", sl) or re.search(r"\b(?:no|without)\s+(?:network|internet)\s+(?:access|calls?|requests?)\b|\bstay offline\b|\bwork offline\b", sl):
            add("forbid", "network", Scope(), frag=s)
            continue
        if re.search(rf"\b{_NEG}\s+(?:ever\s+)?(?:rewrite|rebase|amend)\s+(?:git\s+)?(?:history|commits|the history)", sl) or re.search(r"\bno (?:rebasing|amending|history rewrit)", sl):
            add("forbid", "git_history", Scope(), frag=s)
            continue
        if re.search(rf"\b{_NEG}\s+(?:ever\s+)?(?:touch|modify|change|deploy to|deploy|run anything against|use)\s+(?:the\s+)?(?:prod|production)\b", sl):
            add("forbid", "modify", Scope(pattern=r"prod"), frag=s)
            add("forbid", "deploy", Scope(pattern=r"prod"), frag=s)
            add("forbid", "run", Scope(command=r"\bprod(uction)?\b"), frag=s)
            continue

        # forbid delete / modify with scope
        m = re.search(rf"\b{_NEG}\s+(?:ever\s+)?{_DEL}\s+(.+)$", sl)
        if m:
            rest = m.group(1)
            # "don't delete X without asking me" is a confirm rule, not a hard forbid
            kind = "confirm" if _WITHOUT_ASKING.search(rest) else "forbid"
            rest = re.sub(r"\s+(?:without|unless|until)\s+.*$", "", rest)
            if _is_universal_phrase(rest):
                add(kind, "delete", Scope(), frag=s)
            else:
                add(kind, "delete", _scope_from_phrase(rest), frag=s)
            continue
        m = re.search(rf"\b{_NEG}\s+(?:ever\s+)?{_MOD}\s+(.+)$", sl)
        if m:
            kind = "confirm" if _WITHOUT_ASKING.search(m.group(1)) else "forbid"
            rest = re.sub(r"\s+(?:without|unless|until|except)\s+.*$", "", m.group(1))
            if _is_universal_phrase(rest):
                add(kind, "modify", Scope(), frag=s)
            else:
                add(kind, "modify", _scope_from_phrase(rest), frag=s)
            continue
        m = re.search(rf"\b(?:leave|keep)\s+(.+?)\s+(?:alone|untouched|as[- ]is|unchanged|intact)\b", sl)
        if m:
            add("forbid", "modify", _scope_from_phrase(m.group(1)), frag=s)
            continue
        m = re.search(rf"\b(?:hands off|stay away from|steer clear of|off[- ]limits:)\s+(.+)$", sl)
        if m:
            add("forbid", "modify", _scope_from_phrase(m.group(1)), frag=s)
            continue
    return rules


def _verb_action(verb: str) -> str:
    v = verb.lower().rstrip(".,")
    table = {
        "delet": "delete", "delete": "delete", "deleting": "delete", "remov": "delete", "remove": "delete", "removing": "delete", "rm": "delete",
        "push": "push", "pushing": "push", "force": "force_push",
        "commit": "commit", "committing": "commit", "commits": "commit",
        "deploy": "deploy", "deploying": "deploy", "deploys": "deploy",
        "install": "install", "installing": "install", "installs": "install",
        "modify": "modify", "modifying": "modify", "edit": "modify", "editing": "modify", "chang": "modify", "change": "modify", "changing": "modify", "touch": "modify", "touching": "modify",
        "run": "run", "running": "run", "execut": "run", "execute": "run", "executing": "run",
        "drop": "delete", "dropping": "delete", "wip": "delete", "wipe": "delete", "wiping": "delete", "overwrit": "write", "overwrite": "write", "overwriting": "write",
        "rebas": "git_history", "rebase": "git_history", "rebasing": "git_history", "merg": "push", "merge": "push", "merging": "push",
        "destructive": "destructive",
    }
    return table.get(v, table.get(v.rstrip("e"), ""))


LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "action": {"type": "string", "enum": ["delete", "modify", "edit", "write", "push", "force_push", "commit", "git_history", "install", "network", "deploy", "run", "destructive"]},
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "branches": {"type": "array", "items": {"type": "string"}},
                    "pattern": {"type": "string"},
                    "prerequisite": {"type": "string", "enum": ["", "tests_pass"]},
                    "text": {"type": "string"},
                },
                "required": ["kind", "action", "text"],
            },
        }
    },
    "required": ["rules"],
}

LLM_PROMPT = """You extract standing constraints that a user places on a coding agent from one user message.
Only extract constraints about what the agent may NOT do, must ASK before doing, may do only AFTER tests pass (require_before/tests_pass), or must not modify after the task starts (freeze).
Ignore task instructions (what to build). If there are no constraints, return {"rules": []}.
paths are globs relative to the repo root (use "tests/**" style). branches like "origin/main". pattern is a regex on targets (e.g. "prod").
Message:
<<<
%s
>>>
Return only JSON matching the schema."""


def extract_rules_llm(text: str, turn: int = 0, model: str = "haiku", timeout: int = 60) -> list[Rule]:
    """Optional recall fallback: ask Claude (via the local `claude` CLI, i.e. the user's subscription) for rules."""
    import subprocess
    try:
        proc = subprocess.run(
            ["claude", "-p", LLM_PROMPT % text, "--model", model, "--output-format", "json",
             "--json-schema", json.dumps(LLM_SCHEMA), "--max-turns", "1", "--no-session-persistence"],
            capture_output=True, text=True, timeout=timeout,
        )
        data = json.loads(proc.stdout)
        payload = data.get("structured_output") or json.loads(data.get("result", "{}"))
    except Exception:
        return []
    out = []
    for r in payload.get("rules", []):
        sc = Scope(paths=r.get("paths", []) or [], branches=r.get("branches", []) or [], pattern=r.get("pattern", "") or "")
        rule = Rule(id=make_id(r.get("text", text), r["kind"], r["action"] + str(sc)), kind=r["kind"], action=r["action"],
                    scope=sc, text=r.get("text", text), source="llm", prerequisite=r.get("prerequisite", ""), created_turn=turn)
        out.append(rule)
    return out
