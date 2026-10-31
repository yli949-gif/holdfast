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
EXPIRES = ("", "next_turn")   # "" = standing rule; "next_turn" = temporary hold, lapses at the next user prompt that asks for work


@dataclass
class Scope:
    paths: list[str] = field(default_factory=list)      # globs relative to project root; "" = any
    branches: list[str] = field(default_factory=list)   # e.g. ["origin/main", "*/main"]
    pattern: str = ""                                   # regex on effect target (urls, packages, free-form commands)
    command: str = ""                                   # regex on the raw command / detail
    # "only edit under src/api/" = forbid modify with exclude_paths=["src/api/**"]: a path effect is IN scope
    # (i.e. the rule applies) when its target is NOT under any exclude glob. Paths outside the project
    # (absolute, ~, ../) are never in an exclude-scope: scratch writes to /tmp are not edits to the project (B4).
    # Non-path effects (branches, urls, packages) are out of an exclude-only scope.
    exclude_paths: list[str] = field(default_factory=list)

    def is_universal(self) -> bool:
        return not (self.paths or self.branches or self.pattern or self.command or self.exclude_paths)

    def matches(self, e: Effect) -> Optional[bool]:
        """True = in scope, False = out of scope, None = can't tell (unknown target)."""
        if self.is_universal():
            return True
        if self.exclude_paths:
            return self._matches_exclude(e)
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


    def _matches_exclude(self, e: Effect) -> Optional[bool]:
        if e.kind not in ("path", "opaque"):
            return False
        if e.target == EVERYTHING:
            return True
        if e.target == UNKNOWN:
            return None
        t = posixpath.normpath(e.target)
        if t.startswith(("/", "~", "../")) or t == "..":
            return False
        if any(_path_in_scope(t, g, parents=False) for g in self.exclude_paths):
            return False
        if self.paths:
            return any(_path_in_scope(t, g) for g in self.paths)
        return True


def _path_in_scope(target: str, glob: str, parents: bool = True) -> bool:
    """Is `target` covered by `glob`? With parents=True a parent directory of the glob also counts
    (deleting/moving `src` destroys `src/legacy/**`); exclude-scopes use parents=False (strict containment)."""
    t = posixpath.normpath(target)
    g = glob.rstrip("/")
    if t == g or fnmatch.fnmatch(t, g) or fnmatch.fnmatch(t, g + "/*") or fnmatch.fnmatch(t, g + "/**"):
        return True
    if g.endswith("/**") and t == g[:-3]:      # the directory itself
        return True
    if "**" in g and fnmatch.fnmatch(t, g.replace("**/", "")):
        return True
    # recursive glob: a/**/b.py should match a/x/y/b.py
    if "**" in g:
        rx = re.escape(g).replace(r"\*\*/", "(.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        if re.fullmatch(rx, t):
            return True
    if not parents:
        return False
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
    expires: str = ""             # "" = standing; "next_turn" = temporary hold ("don't change anything yet")
    created_at: float = field(default_factory=time.time)
    created_turn: int = 0
    active: bool = True

    def as_dict(self) -> dict:
        d = asdict(self)
        if not d["scope"].get("exclude_paths"):
            d["scope"].pop("exclude_paths", None)   # keep the v1 on-disk shape when unused
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
        if (r.expires or "") not in EXPIRES:
            raise ValueError(f"unknown expires value {r.expires!r}")
        r.expires = r.expires or ""
        return r

    def matches(self, e: Effect) -> Optional[bool]:
        if not self.active or not action_matches(self.action, e.action):
            return False
        return self.scope.matches(e)

    def describe(self) -> str:
        d = self._describe()
        if self.expires == "next_turn":
            d += " (temporary: until the user's next request)"
        return d

    def _describe(self) -> str:
        sc = []
        if self.scope.exclude_paths:
            sc.append("outside " + ", ".join(self.scope.exclude_paths))
        if self.scope.paths:
            sc.append("paths " + ", ".join(self.scope.paths))
        if self.scope.branches:
            sc.append("branches " + ", ".join(self.scope.branches))
        if self.scope.pattern:
            sc.append(f"matching /{self.scope.pattern}/")
        if self.scope.command:
            sc.append(f"commands /{self.scope.command}/")
        scope = ""
        for part in sc:
            scope += (" " if part.startswith("outside ") else (" on " if not scope else "; ")) + part
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
#
# Pipeline (precision-first; the LLM extractor is the recall fallback):
#   1. normalise  — quotes, common contractions/typos ("dont", "pls", "w/o", " u "), whitespace
#   2. sentences  — split on . ! ? ; newlines (paths like `a/b.py` survive: a split needs ". " + capital/cue)
#   3. sentence guards — skip questions, permissions/releases ("feel free", "no longer needed", "is fine"),
#      and descriptions of *code* behaviour ("write a hook that blocks force pushes")
#   4. clauses    — split on dashes / colons / ", and" / ", but" / "and" + a constraint cue
#   5. templates  — each clause is tried against intent templates in a fixed order; each template returns
#      rules or None. Temporal markers ("yet", "for now", "at this point", "hold off", "until I say") turn a
#      forbid into a temporary hold (expires="next_turn").
#   6. scope resolution — a noun phrase must resolve to a path / alias / module / branch / prod, or to an
#      explicit universal ("anything", "any files", "the code"); anything else yields NO rule.
# ---------------------------------------------------------------------------

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
# phrase variants that resolve to an alias key
_ALIAS_SYNONYMS = {
    "test files": "tests", "test file": "tests", "test suite": "tests", "unit tests": "tests", "test code": "tests",
    "test directory": "tests", "tests directory": "tests", "tests folder": "tests", "test folder": "tests",
    "migration": "migrations", "migration files": "migrations", "database migrations": "migrations",
    "configuration": "config", "config files": "config", "lock files": "lockfiles", "ci config": "ci", "ci configuration": "ci",
    "database": "the database", "db": "the database",
}

# verb/noun stem -> action. Inflections (-s, -es, -ing, -ed, -ion, -ment) are stripped by _verb_action.
_VERBS = {
    "delete": "delete", "delet": "delete", "deletion": "delete", "remove": "delete", "remov": "delete", "removal": "delete",
    "rm": "delete", "erase": "delete", "eras": "delete", "wipe": "delete", "wip": "delete", "destroy": "delete", "nuke": "delete",
    "drop": "delete", "dropp": "delete", "unlink": "delete", "purge": "delete", "purg": "delete",
    "modify": "modify", "modifi": "modify", "modification": "modify", "edit": "modify", "change": "modify", "chang": "modify",
    "touch": "modify", "alter": "modify", "update": "modify", "updat": "modify", "rewrite": "modify", "rewrit": "modify",
    "overwrite": "modify", "overwrit": "modify", "write": "modify", "writ": "modify", "mess": "modify", "refactor": "modify",
    "rename": "modify", "renam": "modify", "move": "modify", "mov": "modify", "reformat": "modify",
    "push": "push", "pushe": "push", "merge": "push", "merg": "push",
    "force-push": "force_push", "force push": "force_push", "forcepush": "force_push", "force-pushe": "force_push", "force pushe": "force_push",
    "commit": "commit", "committ": "commit",
    "deploy": "deploy", "deployment": "deploy", "release": "deploy", "releas": "deploy", "ship": "deploy", "shipp": "deploy",
    "install": "install", "installation": "install", "instal": "install",
    "run": "run", "runn": "run", "execute": "run", "execut": "run", "execution": "run",
    "rebase": "git_history", "rebas": "git_history", "amend": "git_history",
}


def _verb_action(word: str) -> str:
    w = word.lower().strip(".,;:!?'\"`")
    if w in _VERBS:
        return _VERBS[w]
    for suf in ("ing", "ions", "ion", "ments", "ment", "es", "ed", "s", "e"):
        if w.endswith(suf) and w[: -len(suf)] in _VERBS:
            return _VERBS[w[: -len(suf)]]
    return ""


_V = r"(?:force[- ]?push(?:es|ing|ed)?|[a-z]+(?:-[a-z]+)?)"   # a verb token (validated by _verb_action)
_NEG = (r"(?:do\s+not|don't|never|mustn't|must\s+not|must\s+never|should\s+not|should\s+never|shouldn't|you\s+(?:are|re)\s+not\s+(?:allowed|permitted)\s+to"
        r"|(?:i'?d|i\s+would)\s+(?:prefer|rather|like)\s+(?:if\s+)?you\s+(?:not|didn't|did\s+not|don't)|refrain\s+from|avoid"
        r"|please\s+don't|please\s+do\s+not|you\s+may\s+not|you\s+can't|you\s+cannot|cannot|can't|not\s+allowed\s+to)")
_ADV = r"(?:\s+(?:ever|ever\s+again|at\s+all|again|directly|yourself))?"
_TEMP = re.compile(r"\b(?:yet|for\s+now|for\s+the\s+(?:moment|time\s+being)|at\s+this\s+(?:point|stage)|right\s+now|until\s+further\s+notice"
                   r"|until\s+i\s+(?:say|tell|give|approve|ok|confirm|get\s+back)\w*(?:\s+(?:so|you|the\s+go[- ]ahead|it))?|hold\s+off)\b", re.I)
_UNTIL_I_SAY = re.compile(r"\buntil\s+i\s+(?:say|tell|give|approve|ok|confirm)\w*(?:\s+(?:so|you|the\s+go[- ]ahead|it))?\b|\bwithout\s+my\s+go[- ]ahead\b", re.I)
_TEMP_ONLY = re.compile(r"^(?:not\s+yet|for\s+now|at\s+this\s+(?:point|stage)|for\s+the\s+(?:moment|time\s+being)|right\s+now|hold\s+on|wait)$", re.I)
_WITHOUT_ASKING = re.compile(r"\b(?:without\s+(?:first\s+)?(?:asking|checking|confirming|consulting|talking|my\s+(?:ok|okay|go[- ]ahead)"
                             r"|(?:my\s+|explicit\s+|user\s+|an?\s+)?(?:approval|permission|confirmation|consent|ok|sign-?off))"
                             r"|unless\s+i\s+(?:explicitly\s+)?(?:approve|ok|confirm|allow)\w*)", re.I)
_TESTS_WORD = r"(?:the\s+|all\s+(?:the\s+)?|our\s+)?(?:unit\s+|full\s+)?(?:tests?|test\s+suite|pytest|suite|ci|checks|the\s+build|build)"
_PASS = r"(?:pass(?:es|ing)?|are\s+passing|is\s+passing|are\s+green|is\s+green|go\s+green|succeed(?:s)?|come\s+back\s+green|are\s+all\s+green|run\s+and\s+pass|be\s+green|be\s+run\s+and\s+pass)"
_ORDER_VERBS = {"push": "push", "commit": "commit", "deploy": "deploy", "merg": "push", "ship": "deploy", "releas": "deploy"}
_UNIVERSAL = re.compile(r"(?:(?:any|all|the|existing|of|other|more|new|extra|single|your|my|our)\s+)*"
                        r"(?:anything(?:\s+else)?|everything|files?|stuff|things?|code|source(?:\s+code)?|source\s+files|codebase|the\s+repo|repo(?:sitory)?|changes?|edits?|projects?)?(?:\s+(?:new|else))?", re.I)
_PATHISH = re.compile(r"`([^`]+)`|\"([^\"]+)\"|'([^'\s]+)'|((?:\.{0,2}/)?[\w.*-]+(?:/[\w.*-]*)+|\.?[\w*-]+(?:\.[\w*-]+)*\.[A-Za-z]\w{0,9}|\.[A-Za-z][\w.-]*)")
_REMOTE_WORDS = ("remote", "origin", "github", "the remote", "upstream", "gitlab", "the server", "remotes")
_PROD = re.compile(r"\b(?:prod|production)\b", re.I)


def _clean_path(p: str) -> str:
    p = p.strip().rstrip(",.;:)(!?").lstrip("(")
    if p.startswith("./"):
        p = p[2:]
    trailing_slash = p.endswith("/")
    p = p.rstrip("/")
    if not p:
        return p
    last = p.rsplit("/", 1)[-1]
    if "*" in p or (not trailing_slash and "." in last.lstrip(".")) or (not trailing_slash and last.startswith(".env")):
        return p
    return p + "/**"


class _Res:
    """Result of resolving a noun phrase: kind in {universal, scope, prod, none}."""
    def __init__(self, kind: str, scope: Optional[Scope] = None):
        self.kind, self.scope = kind, scope or Scope()


_TRAILING = re.compile(r"(?:,?\s+(?:yet|for\s+now|for\s+the\s+(?:moment|time\s+being)|at\s+this\s+(?:point|stage)|at\s+all|ever|again|please|"
                       r"by\s+hand|either|in\s+(?:this|the)\s+(?:task|pr|change|session)|for\s+(?:this|the)\s+task|this\s+time|right\s+now|"
                       r"whatsoever|manually|only|then|directly|yourself|ok\??|okay\??|"
                       r"(?:without|unless|until|except|after|once|when|while|because|since|so|as|if|before)\b.*|"
                       r"(?:—|–|\s-\s|,).*|i'?ll\b.*|we'?ll\b.*))+$", re.I)


def _resolve(phrase: str, last_path: Optional[list] = None, module_ok: bool = True) -> _Res:
    """Resolve a noun phrase to a scope. Precision-first: an unrecognised noun phrase resolves to none."""
    ph = phrase.replace("(", " ").replace(")", " ").strip().rstrip(".,;:!?").lstrip(",;:!?")
    ph = re.sub(r"^(?:-{1,2}[a-z]+\s*)+", "", ph)   # command flags ("rm -rf")
    # cut at a comma / dash unless what follows is another path ("tests/, fixtures/")
    cut = re.split(r"\s*(?:,|;|\s[—–-]\s)\s*", ph, maxsplit=1)
    if len(cut) == 2 and not re.search(r"[\w*]/|/[\w*]|\.\w{1,5}\b", cut[1]):
        ph = cut[0]
    ph = _TRAILING.sub("", " " + ph).strip().rstrip(".,;:!?").lstrip(",;:!?")
    low = ph.lower()
    low = re.sub(r"^(?:on|with|to|into|in|under|inside|within|from)\s+", "", low)
    ph = ph[len(ph) - len(low):] if len(low) <= len(ph) else ph
    # explicit paths (case-preserved)
    paths = []
    for m in _PATHISH.finditer(ph):
        tok = next(g for g in m.groups() if g)
        if re.fullmatch(r"\.?\w+\.(?:e\.g|i\.e|etc)", tok.lower()) or tok.lower() in ("e.g", "i.e", "etc."):
            continue
        if "/" not in tok and "." not in tok:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)*", tok):
            continue
        paths.append(_clean_path(tok))
    for m in re.finditer(r"\b(Makefile|Dockerfile|Jenkinsfile|Procfile|Gemfile|Rakefile|Vagrantfile|LICENSE|CODEOWNERS|Justfile|Containerfile)\b", ph, re.I):
        if m.group(1) not in paths:
            paths.append(m.group(1))
    if paths:
        return _Res("scope", Scope(paths=paths))
    if re.fullmatch(r"(?:that|this|the\s+same)\s+(?:folder|directory|dir|path|module|package|area)|there|it", low) and last_path:
        return _Res("scope", Scope(paths=list(last_path)))
    if low in _PATH_ALIASES:
        return _Res("scope", Scope(paths=list(_PATH_ALIASES[low])))
    core = re.sub(r"^(?:anything|any\s+files?|files?|stuff|things?|everything|code)\s+(?:in|under|inside|from|within|of)\s+", "", low)
    core = re.sub(r"^(?:(?:the|any|all|our|your|my|existing|of)\s+)+", "", core).strip()
    if core in _ALIAS_SYNONYMS:
        core = _ALIAS_SYNONYMS[core]
    for alias, globs in _PATH_ALIASES.items():
        a = alias[4:] if alias.startswith("the ") else alias
        if re.fullmatch(rf"{re.escape(a)}(?:\s+(?:directory|folder|dir|files?|suite))?", core):
            return _Res("scope", Scope(paths=list(globs)))
    if _PROD.search(low) and re.fullmatch(r"(?:anything\s+|any\s+|the\s+|our\s+|everything\s+)*(?:in\s+|on\s+|to\s+|against\s+)?(?:the\s+)?(?:prod|production)(?:\s+(?:environment|env|cluster|servers?|database|db|systems?|stuff|deploy(?:ment)?s?|infra(?:structure)?))?", low):
        return _Res("prod")
    if low == "" or _UNIVERSAL.fullmatch(low):
        return _Res("universal")
    m = re.fullmatch(r"(?:the\s+)?([a-z][\w-]*)\s+(module|package|folder|directory|dir|app|service|component)", low)
    if m and module_ok:
        name, noun = m.groups()
        if noun in ("folder", "directory", "dir"):
            return _Res("scope", Scope(paths=[f"{name}/**"]))
        return _Res("scope", Scope(paths=[f"**/{name}/**", f"**/{name}.py"]))
    return _Res("none")


def _branch_scope(name: str) -> Scope:
    b = name.strip("`'\".,")
    if b.lower() in _REMOTE_WORDS:
        return Scope()
    return Scope(branches=[b, "*/" + b])


_QUESTION = re.compile(r"^(?:why|what|how|is|are|can|could|would|will|does|did|do\s+you|where|when|who|which|should\s+i|shall)\b", re.I)
_RELEASE = re.compile(r"\b(?:no\s+longer|is\s+fine|are\s+fine|is\s+ok(?:ay)?|feel\s+free|no\s+need\s+to|don'?t\s+need\s+to|doesn'?t\s+matter"
                      r"|go\s+ahead|you\s+can\s+(?!not\b)(?!only\b)|you\s+may\s+(?!not\b)(?!only\b)|you'?re\s+(?:free|allowed)|is\s+allowed|are\s+allowed"
                      r"|it'?s\s+(?:fine|ok(?:ay)?)\s+to|never\s*mind|don'?t\s+worry|i\s+don'?t\s+think|previously)\b", re.I)
_CODE_DESC = re.compile(r"^(?:please\s+)?(?:write|add|create|implement|build|make|edit|update|change|modify|fix|configure|set\s+up|design)\b.*"
                        r"\b(?:so\s+(?:that\s+)?(?:it|they|we)|that\s+(?:blocks?|prevents?|stops?|forbids?|rejects?|refuses?|checks?|enforces?|only|never|ensures?)"
                        r"|which\s+(?:blocks?|prevents?|only|never)|to\s+(?:block|prevent|reject|forbid))\b", re.I)
_LEAD = re.compile(r"^(?:(?:and|but|also|so|oh|ok|okay|alright|then|plus|one\s+more\s+thing|whatever\s+you\s+do|kindly|please|pls|"
                   r"from\s+now\s+on|going\s+forward|from\s+here\s+on(?:\s+out)?|for\s+this\s+task|in\s+general|important|note|rule|"
                   r"remember|reminder|btw|and\s+also)[,:]?\s+)+", re.I)


def _normalise(text: str) -> str:
    t = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = " ".join(t.split())
    subs = [(r"\bdont\b", "don't"), (r"\bdonot\b", "do not"), (r"\bdo'nt\b", "don't"), (r"\bpls\b|\bplz\b", "please"),
            (r"\bu\b", "you"), (r"\bw/o\b", "without"), (r"\binstal\b", "install"), (r"\bnever\s+ever\b", "never"),
            (r"\bdon't\s+you\s+dare\b", "don't"), (r"\bnever\s*,\s*ever\b", "never"),
            (r"\b(?:be\s+careful|make\s+sure|take\s+care|try|remember)\s+not\s+to\b", "don't"),
            (r"\b(don't|do\s+not|never)\s+do\s+anything\b", r"\1 change anything"), (r"\bunder\s+no\s+circumstances\s+(?:should\s+you\s+)?", "never "),
            (r"\bkeep\s+(?:your\s+)?hands\s+off\b", "hands off"), (r"\bmake\s+sure\s+(?:that\s+)?you\s+(?:don't|do\s+not)\b", "don't"),
            (r"\bnot\s+(?:allowed|permitted)\s+to\b", "not allowed to"),
            (r"\b(?:make|do|perform|create)\s+(?:any\s+|more\s+|further\s+)?(commits?|changes|edits|modifications|pushes|deletions)\b", r"\1"),
            (r"\bfrom\s+this\s+point\s+(?:on(?:wards?)?|forward)\b", "from now on")]
    for a, b in subs:
        t = re.sub(a, b, t, flags=re.I)
    return t


def _sentences(t: str) -> list[str]:
    # split on sentence punctuation followed by space, but never inside a path token ("src/a.py is ...")
    parts = re.split(r"(?<=[!?;])\s+|(?<=\.)\s+(?=[A-Z\"'`(]|(?:don't|do|never|no|please|ask|only|stay|leave|keep|make|run)\b)|\n+|;", t)
    return [p.strip() for p in parts if p and p.strip()]


_CLAUSE_CUE = (r"(?:don't|do\s+not|never|no|please|ask|check|confirm|get\s+my|only|stay|leave|keep|make\s+sure|ensure|run\s+the|"
               r"avoid|hands\s+off|hold\s+off|not\s+yet|let\s+me\s+know|refrain|you\s+(?:are|re)\s+not|nothing)\b")


def _clauses(s: str) -> list[str]:
    parts = re.split(rf"\s+[—–]\s+|\s[—–]|[—–]\s|\s+-\s+|:\s+|,\s*(?:and|but|or|then)\s+(?={_CLAUSE_CUE})|\s+(?:and|but|or|then)\s+(?={_CLAUSE_CUE})"
                     rf"|,\s+(?={_CLAUSE_CUE})|\s+(?:and\s+also|also)\s+", s, flags=re.I)
    out: list[str] = []
    for p in parts:
        if not p or not p.strip(" ,.!"):
            continue
        p = p.strip().strip(",.!")
        # a fronted subordinate clause belongs to the next clause ("Once you start, don't modify tests/")
        if out and re.match(r"^(?:before|once|after|when|until|if)\b", out[-1], re.I) and "," not in out[-1][-1:]:
            out[-1] = out[-1] + ", " + p
            continue
        out.append(p)
    return [_LEAD.sub("", p).strip() for p in out]


class _Ctx:
    def __init__(self, text, source, turn):
        self.text, self.source, self.turn = text, source, turn
        self.rules: list[Rule] = []
        self.last_path: Optional[list] = None
        self.temp_carry = False

    def add(self, kind, action, scope=None, frag="", prereq="", expires="", mode="once"):
        scope = scope or Scope()
        frag = frag or self.text
        r = Rule(id=make_id(frag, kind, action + str(scope) + expires), kind=kind, action=action, scope=scope, text=frag,
                 source=self.source, mode=mode, prerequisite=prereq, created_turn=self.turn, expires=expires)
        if all(x.id != r.id for x in self.rules):
            self.rules.append(r)
        if scope.paths or scope.exclude_paths:
            self.last_path = list(scope.exclude_paths or scope.paths)


def _actions_in(verbs: str) -> list[str]:
    """'delete or install' -> ['delete', 'install']; 'force push' -> ['force_push']."""
    out = []
    for v in re.split(r"\s*(?:,|/|\bor\b|\band\b|\bnor\b)\s*", verbs.strip()):
        v = v.strip()
        if not v:
            continue
        if re.fullmatch(r"force[- ]?push\w*|push\s+(?:-f|--force)\w*", v, re.I):
            a = "force_push"
        else:
            a = _verb_action(v.split()[0]) if v.split() else ""
        if a and a not in out:
            out.append(a)
    return out


_GIT_PUSH_F = re.compile(r"\b(?:git\s+)?push\s+(?:-f|--force(?:-with-lease)?)\b|\bforce[- ]?push\w*|\bpush\s+(?:with\s+)?force\b|\bpush\s+force\b", re.I)


def _action_rules(ctx: _Ctx, kind: str, action: str, rest: str, frag: str, expires: str = "") -> bool:
    """Emit rule(s) for `kind action <rest>`; returns False if the object can't be resolved (no rule)."""
    rest = rest.strip()
    if action == "force_push":
        ctx.add(kind, "force_push", Scope(), frag=frag, expires=expires)   # a force push is dangerous on any branch
        return True
    if action in ("push", "commit"):
        m = re.match(r"(?:\w+\s+)?(?:anything\s+|code\s+|changes\s+|commits?\s+|it\s+)?(?:directly\s+|straight\s+)?(?:to|onto|into|on)\s+(?:the\s+)?[`'\"]?([\w./-]+?)[`'\"]?(?:\s+branch)?(?:[\s,.;!]|$)", rest + " ", re.I)
        if m and m.group(1).lower() not in ("production", "prod"):
            sc = _branch_scope(m.group(1))
            ctx.add(kind, action, sc, frag=frag, expires=expires)
            return True
        if _PROD.search(rest):
            return _prod_rules(ctx, kind, frag, expires)
        # "never commit secrets" / "don't push the .env file" name an object we can't scope: no rule (a universal
        # forbid-commit would block every commit)
        obj = re.sub(r"^(?:(?:anything|any|all|the|your|my|our|of|these|those|new|code|changes?|commits?|work|it|them|this|that|stuff|things)\b\s*)+", "", _TRAILING.sub("", " " + rest).strip().lower())
        obj = re.sub(r"^(?:yet|for\s+now|until\b.*|without\b.*|unless\b.*|please)$", "", obj).strip(" .,!")
        if obj and not re.fullmatch(r"(?:to|on|into)\s+(?:the\s+)?(?:remote|origin|upstream|github|gitlab)\b.*|(?:yourself|again|ever|at\s+all|here|now|today)", obj):
            return False
        ctx.add(kind, action, Scope(), frag=frag, expires=expires)
        return True
    if action == "install":
        ctx.add(kind, "install", Scope(), frag=frag, expires=expires)
        return True
    if action == "git_history":
        ctx.add(kind, "git_history", Scope(), frag=frag, expires=expires)
        return True
    if action == "deploy":
        if _PROD.search(rest):
            if kind == "confirm":
                ctx.add(kind, "deploy", Scope(pattern=r"prod"), frag=frag, expires=expires)
                return True
            return _prod_rules(ctx, kind, frag, expires)
        ctx.add(kind, "deploy", Scope(), frag=frag, expires=expires)
        return True
    if action == "run":
        r = _TRAILING.sub("", rest).strip().lower()
        r = re.sub(r"^(?:(?:any|the|a|an|database|db|schema)\s+)+", "", r)
        if _PROD.search(r) and not re.search(r"[/.]", r):
            return _prod_rules(ctx, kind, frag, expires)
        if re.match(r"migrations?|migrate", r):
            ctx.add(kind, "run", Scope(command=r"migrat|alembic|flyway"), frag=frag, expires=expires)
            return True
        m = re.match(r"[`'\"]?([a-z][\w.-]*)", r)
        if m and m.group(1) not in ("anything", "commands", "command", "stuff", "things", "scripts", "it", "code"):
            ctx.add(kind, "run", Scope(command=rf"\b{re.escape(m.group(1))}\b"), frag=frag, expires=expires)
            return True
        if not r or re.fullmatch(r"(?:any\s+)?(?:anything|commands?|stuff|things|scripts|code)", r):
            ctx.add(kind, "run", Scope(), frag=frag, expires=expires)
            return True
        return False
    # path actions: modify / delete
    res = _resolve(rest, ctx.last_path)
    if res.kind == "prod":
        return _prod_rules(ctx, kind, frag, expires)
    if res.kind == "universal":
        ctx.add(kind, action, Scope(), frag=frag, expires=expires)
        return True
    if res.kind == "scope":
        ctx.add(kind, action, res.scope, frag=frag, expires=expires)
        return True
    return False


def _prod_rules(ctx: _Ctx, kind: str, frag: str, expires: str = "") -> bool:
    ctx.add(kind, "modify", Scope(pattern=r"prod"), frag=frag, expires=expires)
    ctx.add(kind, "deploy", Scope(pattern=r"prod"), frag=frag, expires=expires)
    ctx.add(kind, "run", Scope(command=r"\bprod(uction)?\b"), frag=frag, expires=expires)
    return True


def _exclude_rule(ctx: _Ctx, phrase: str, frag: str) -> bool:
    res = _resolve(phrase, ctx.last_path)
    if res.kind != "scope":
        return False
    ctx.add("forbid", "modify", Scope(exclude_paths=list(res.scope.paths)), frag=frag)
    return True


_PRONOUNS = {"you", "we", "i", "it", "they", "this", "that", "he", "she", "everything", "nothing"}
_MODV = r"(?:edit|touch|modify|change|write\s+to|work\s+on|work\s+in|alter|update|(?:make\s+)?(?:changes|edits)\s+(?:to|in))"


def _clause_rules(ctx: _Ctx, c: str, frag: str, temp: bool) -> bool:
    """Try the intent templates on one clause. Returns True if any rule was emitted."""
    exp = "next_turn" if temp else ""
    cl = c.strip()
    low = cl.lower()

    # ---- explicit syntax ("holdfast: forbid delete data/**") is handled at sentence level

    # ---- scope (exclude) rules
    pats = [
        rf"^only\s+{_MODV}\s+(?:files?\s+|code\s+|things\s+)?(?:in|under|inside|within|from)?\s*(.+)$",
        rf"^{_MODV}\s+only\s+(?:files?\s+)?(?:in|under|inside|within)\s+(.+)$",
        r"^(?:stay|remain|keep|work)\s+(?:only\s+)?(?:with)?in(?:side)?\s+(.+)$",
        r"^work\s+only\s+(?:with)?in(?:side)?\s+(.+)$",
        r"^(?:keep|limit|restrict|confine)\s+(?:your\s+|all\s+|the\s+)?(?:edits|changes|modifications|work)\s+(?:to|in|within|inside)\s+(.+?)(?:\s+only)?$",
        r"^(?:your\s+|all\s+|the\s+)?(?:edits|changes|modifications)\s+(?:should|must|need\s+to|have\s+to)\s+(?:be\s+)?(?:limited|restricted|confined|stay|remain|kept)\s+(?:to|in|within|inside)\s+(.+)$",
        rf"^(?:{_NEG})\s+(?:{_MODV}|make\s+(?:any\s+)?changes)\s+(?:anything|any\s+files?|files?|code)?\s*outside\s+(?:of\s+)?(.+)$",
        r"^avoid\s+(?:making\s+)?(?:any\s+)?(?:changes|edits)\s+(?:to\s+)?(?:files?\s+|anything\s+)?outside\s+(?:of\s+)?(.+)$",
        r"^nothing\s+outside\s+(?:of\s+)?(.+?)\s+(?:should|may|can|must)\s+(?:be\s+)?(?:change|modifi|edit|touch)\w*$",
    ]
    for p in pats:
        m = re.match(p, low)
        if m:
            phrase = cl[m.start(1):m.end(1)]
            if _exclude_rule(ctx, phrase, frag):
                return True
    # "you can edit anything except X"
    m = re.match(rf"^(?:you\s+(?:can|may)\s+)?(?:{_MODV}|touch)\s+(?:anything|everything|any\s+files?)\s+(?:except|but|other\s+than)\s+(?:for\s+)?(.+)$", low)
    if m:
        res = _resolve(cl[m.start(1):m.end(1)], ctx.last_path)
        if res.kind == "scope":
            ctx.add("forbid", "modify", res.scope, frag=frag)
            return True
        return False

    # ---- releases / permissions: never a constraint
    if _RELEASE.search(low):
        return False

    # ---- temporary hold: "hold off on X"
    m = re.match(r"^hold\s+off\s+(?:on\s+|with\s+)?(.*)$", low)
    if m:
        obj = re.sub(r"\s+(?:for\s+now|until\b.*|yet)$", "", m.group(1)).strip()
        acts = _actions_in(obj.split()[0]) if obj else []
        if acts and acts[0] != "modify":
            for a in acts:
                _action_rules(ctx, "forbid", a, " ".join(obj.split()[1:]), frag, "next_turn")
        else:
            ctx.add("forbid", "modify", Scope(), frag=frag, expires="next_turn")
        return True

    # ---- require_before tests_pass
    ov = r"(push|commit|deploy|merg|ship|releas)\w*"
    pats = [
        rf"^(?:only\s+)?(?:(?:{_NEG})\s+)?{ov}(?:\s+\S+){{0,4}}?\s+(?:only\s+)?(?:until|unless|after|once|when|if)\s+(?:all\s+)?{_TESTS_WORD}\s+(?:\w+\s+)?{_PASS}",
        rf"^(?:only\s+)?{ov}(?:\s+\S+){{0,3}}?\s+(?:only\s+)?(?:after|once)\s+(?:running|you(?:'ve|\s+have)?\s+run)\s+{_TESTS_WORD}",
        rf"^(?:always\s+)?(?:run|make\s+sure|ensure|check(?:\s+that)?|verify(?:\s+that)?)\s+{_TESTS_WORD}\s+(?:\w+\s+)?(?:{_PASS}\s+)?(?:before|prior\s+to)\s+(?:you\s+|every\s+|any\s+|each\s+|a\s+)?{ov}",
        rf"^{_TESTS_WORD}\s+(?:must|should|need\s+to|have\s+to)\s+{_PASS}\s+(?:before|prior\s+to)\s+(?:you\s+|any\s+|every\s+|each\s+)?{ov}",
        rf"^before\s+(?:you\s+|any\s+|every\s+|each\s+)?{ov}(?:\s+\S+){{0,3}}?,?\s+{_TESTS_WORD}\s+(?:must|should|need\s+to|have\s+to)\s+{_PASS}",
        rf"^before\s+(?:you\s+|any\s+|every\s+|each\s+)?{ov}(?:\s+\S+){{0,3}}?,?\s+(?:always\s+)?(?:run|make\s+sure|ensure)\s+{_TESTS_WORD}",
        rf"^no\s+{ov}\s+(?:until|unless|before)\s+{_TESTS_WORD}\s+{_PASS}",
        rf"^(?:{_NEG})\s+{ov}(?:\s+\S+){{0,3}}?\s+without\s+(?:first\s+)?(?:running|passing|green)\s+{_TESTS_WORD}",
    ]
    for p in pats:
        m = re.search(p, low)
        if m:
            ctx.add("require_before", _ORDER_VERBS[m.group(1)], Scope(), frag=frag, prereq="tests_pass")
            return True

    # ---- confirm-before
    ask = (r"(?:(?:you\s+)?(?:must|should|need\s+to|have\s+to)\s+)?(?:always\s+)?(?:ask(?:\s+me)?(?:\s+for\s+(?:permission|approval))?|check\s+with\s+me|check\s+in(?:\s+with\s+me)?|check|confirm(?:\s+with\s+me)?"
           r"|get\s+(?:my\s+|explicit\s+)?(?:approval|permission|ok|okay|sign-?off|go[- ]ahead)|wait\s+for\s+(?:my\s+|an?\s+)?(?:ok|okay|approval|go[- ]ahead|confirmation|sign-?off|permission)|let\s+me\s+know|talk\s+to\s+me|ping\s+me|consult\s+me|run\s+it\s+by\s+me)")
    m = re.match(rf"^{ask}\s+(?:me\s+)?(?:first\s+)?(?:before|prior\s+to)\s+(?:you\s+)?(?:any\s+|every\s+|each\s+|a\s+)?(?:git\s+)?((?:{_V})(?:\s*(?:,|/|or|and)\s*(?:{_V}))*)\b\s*(.*)$", low)
    if not m:
        m = re.match(rf"^before\s+(?:you\s+)?(?:any\s+|every\s+)?(?:git\s+)?((?:{_V})(?:\s*(?:,|/|or|and)\s*(?:{_V}))*)\b\s*(.*?),?\s+{ask}(?:\s+me)?(?:\s+first)?$", low)
    if m:
        verbs, rest_orig = m.group(1), cl[m.start(2):m.end(2)]
        if re.match(r"force\b", rest_orig, re.I):
            verbs, rest_orig = verbs + " force-push", rest_orig[len("force"):]
        acts = _actions_in(verbs)
        if acts:
            emitted = False
            for a in acts:
                emitted |= _action_rules(ctx, "confirm", a, rest_orig, frag)
            return emitted
    m = re.match(r"^(?:you\s+)?(?:need|must\s+have|must\s+get|require)\s+(?:my\s+|explicit\s+)?(?:approval|permission|ok|okay|sign-?off|go[- ]ahead|confirmation)\s+(?:to|before|for)\s+(?:you\s+|any\s+|every\s+)?(?:git\s+)?(\S+)\s*(.*)$", low)
    if m and _actions_in(m.group(1)):
        done = False
        for a in _actions_in(m.group(1)):
            done |= _action_rules(ctx, "confirm", a, cl[m.start(2):m.end(2)], frag)
        return done
    m = re.match(r"^(?:i\s+want\s+to|i'?d\s+like\s+to|let\s+me)\s+(?:review|see|approve|check)\s+(?:it\s+|things\s+|everything\s+)?before\s+(?:anything|it|you|they|changes|code)\s+(?:gets?\s+|is\s+|are\s+)?(\w+)", low)
    if m and _verb_action(m.group(1)):
        ctx.add("confirm", _verb_action(m.group(1)), Scope(), frag=frag)
        return True
    m = re.match(r"^(?:every|any|all|each)?\s*([a-z-]+?)\s+(?:of\s+\w+\s+)?(?:requires?|needs?|must\s+(?:have|get)|has\s+to\s+(?:have|get))\s+(?:my\s+|explicit\s+|user\s+|an?\s+)?(?:confirmation|approval|sign-?off|permission|ok|okay|review)", low)
    if m and _verb_action(m.group(1)):
        ctx.add("confirm", _verb_action(m.group(1)), Scope(), frag=frag)
        return True

    # ---- freeze
    m = re.match(rf"^(?:once|after|when)\s+(?:you(?:'ve|\s+have)?|we(?:'ve)?)\s+(?:start|begin|began|started)\w*(?:\s+\w+ing)?(?:\s+\w+)?,?\s+(?:{_NEG})\s+(\w+)\s+(.+)$", low)
    if m and _verb_action(m.group(1)) == "modify":
        res = _resolve(cl[m.start(2):m.end(2)], ctx.last_path)
        if res.kind == "scope":
            ctx.add("freeze", "modify", res.scope, frag=frag)
            return True
    m = re.match(rf"^(?:{_NEG})\s+(\w+)\s+(.+?)\s+(?:after|once|when)\s+(?:you|we|the\s+task|work|implementation)\s+(?:start|begin|get\s+going)\w*", low) or \
        re.match(rf"^(?:{_NEG})\s+(\w+)\s+(.+?)\s+(?:after\s+this\s+point|from\s+(?:now|this\s+point|here)\s+on(?:wards?)?|from\s+now|going\s+forward)\b", low)
    if m and _verb_action(m.group(1)) == "modify":
        res = _resolve(cl[m.start(2):m.end(2)], ctx.last_path)
        if res.kind == "scope":
            ctx.add("freeze", "modify", res.scope, frag=frag)
            return True
    m = re.match(r"^(?:from\s+(?:now|this\s+point|here)\s+on,?\s+)?(?:leave|keep)\s+(.+?)\s+(?:as\s+(?:it\s+is|they\s+are)|frozen|exactly\s+as\s+(?:it\s+is|they\s+are))$", low)
    if m:
        res = _resolve(cl[m.start(1):m.end(1)], ctx.last_path)
        if res.kind == "scope":
            ctx.add("freeze", "modify", res.scope, frag=frag)
            return True
        return False

    # ---- "leave/keep X alone", "stay out of X", "hands off X", "no changes to X"
    m = re.match(r"^(?:leave|keep)\s+(.+?)\s+(?:alone|untouched|as[- ]is|unchanged|intact|unmodified|be)$", low) or \
        re.match(r"^(?:stay\s+(?:out\s+of|away\s+from)|hands\s+off|steer\s+clear\s+of|keep\s+(?:out\s+of|away\s+from)|(?:do\s+not|don't|never)\s+go\s+near)\s+(?:of\s+)?(.+)$", low) or \
        re.match(r"^(?:no|zero|don't\s+make\s+any|do\s+not\s+make\s+any|make\s+no)\s+(?:changes|edits|modifications)\s+(?:to|in|on)\s+(.+)$", low)
    if m:
        res = _resolve(cl[m.start(1):m.end(1)], ctx.last_path)
        if res.kind == "prod":
            return _prod_rules(ctx, "forbid", frag, exp)
        if res.kind == "scope":
            ctx.add("forbid", "modify", res.scope, frag=frag, expires=exp)
            return True
        if res.kind == "universal" and temp:
            ctx.add("forbid", "modify", Scope(), frag=frag, expires=exp)
            return True
        return False

    # ---- passive / predicate on a subject: "X must not be modified", "X is off-limits / read-only / vendored"
    m = re.match(r"^(.+?)\s+(?:is|are)\s+(?:\w+\s+and\s+)?(?:must|should|may|can|shall)\s*(?:not|n't|never)\s+be\s+(\w+)", low) or \
        re.match(r"^(.+?)\s+(?:must|should|may|can|shall)\s*(?:not|n't|never)\s+(?:ever\s+)?be\s+(\w+)", low) or \
        re.match(r"^(.+?)\s+(?:is|are)\s+not\s+to\s+be\s+(\w+)", low)
    if m and m.group(1).strip() in _PRONOUNS:
        m = None
    if m:
        subj, verb = cl[m.start(1):m.end(1)], m.group(2)
        a = _verb_action(verb)
        if a in ("modify", "delete"):
            res = _resolve(subj, ctx.last_path, module_ok=False)
            if res.kind == "scope":
                ctx.add("forbid", a, res.scope, frag=frag, expires=exp)
                return True
            if res.kind == "prod":
                return _prod_rules(ctx, "forbid", frag, exp)
        return False
    m = re.match(r"^(.+?)\s+(?:must|should|has\s+to|needs\s+to|have\s+to|need\s+to)\s+(?:stay|remain|be\s+left|be\s+kept)\s+(?:as[- ]is|unchanged|untouched|intact|the\s+same|alone|as\s+(?:it\s+is|they\s+are))$", low)
    if m and m.group(1).strip() not in _PRONOUNS:
        res = _resolve(cl[m.start(1):m.end(1)], ctx.last_path, module_ok=False)
        if res.kind == "scope":
            ctx.add("forbid", "modify", res.scope, frag=frag, expires=exp)
            return True
        return False
    m = re.match(r"^(.+?)\s+(?:is|are)\s+(?:strictly\s+|completely\s+)?(off[- ]limits|read[- ]only|vendored|frozen|locked|sacred|untouchable|not\s+(?:allowed|permitted)|forbidden|prohibited|banned)\b", low) or \
        re.match(r"^(.+?)\s+(?:is|are)\s+\w+\s+and\s+(read[- ]only|off[- ]limits)\b", low)
    if m and m.group(1).strip() in _PRONOUNS:
        m = None
    if m:
        subj = cl[m.start(1):m.end(1)]
        g = re.match(r"^((?:\w+ing)(?:\s+or\s+\w+ing)?)\b\s*(.*)$", subj.lower())
        if g and _actions_in(g.group(1)):
            done = False
            for a in _actions_in(g.group(1)):
                done |= _action_rules(ctx, "forbid", a, g.group(2), frag, exp)
            return done
        res = _resolve(subj, ctx.last_path, module_ok=False)
        if res.kind == "scope":
            ctx.add("forbid", "modify", res.scope, frag=frag, expires=exp)
            return True
        if res.kind == "prod":
            return _prod_rules(ctx, "forbid", frag, exp)
        return False

    # ---- network
    if re.match(rf"^(?:{_NEG})\s+(?:ever\s+)?(?:hit|use|access|call|touch|reach|go\s+(?:on|to))\s+(?:the\s+)?(?:network|internet|web|external\s+(?:apis?|services?))\b", low) or \
       re.match(rf"^(?:{_NEG})\s+(?:make|do|perform|send)\s+(?:any\s+)?(?:network|internet|http|web|external|outbound)\s+(?:calls?|requests?|access)", low) or \
       re.match(r"^(?:no|without)\s+(?:network|internet|outbound|external)(?:\s+(?:access|calls?|requests?|connections?))?$", low) or \
       re.match(r"^(?:work|stay|remain|operate)\s+(?:fully\s+|completely\s+)?offline\b", low) or \
       re.match(r"^no\s+(?:curl|wget|http\s+requests?|web\s+requests?|downloads?)\b", low) or \
       re.match(rf"^(?:{_NEG})\s+(?:run|use)\s+(?:anything\s+(?:with|like)\s+)?(?:curl|wget)\b", low):
        ctx.add("forbid", "network", Scope(), frag=frag, expires=exp)
        return True

    # ---- install
    if re.match(rf"^(?:{_NEG})\s+(?:ever\s+)?(?:install|add|pull\s+in|bring\s+in|introduce|use)\s+(?:(?:any|new|extra|additional|more|third[- ]party|external|other)\s+)*(?:packages?|dependenc(?:y|ies)|libraries|libs?|modules|deps|anything(?:\s+new)?)\b", low) or \
       re.match(r"^no\s+(?:more\s+|new\s+|additional\s+|extra\s+|third[- ]party\s+)*(?:dependencies|packages|deps|installs?|libraries|pip\s+installs?)\b", low):
        ctx.add("forbid", "install", Scope(), frag=frag, expires=exp)
        return True

    # ---- git history
    if re.match(rf"^(?:you\s+)?(?:{_NEG})\s+(?:ever\s+)?(?:rewrite|rebase|amend|squash|alter)(?:\s+(?:any\s+|the\s+|git\s+|existing\s+|published\s+)*(?:history|commits?))?$", low) or \
       re.match(r"^no\s+(?:rebasing|rebases|amending|amends|squashing|squashes|history\s+rewrit\w*|rewriting\s+history)\b", low):
        ctx.add("forbid", "git_history", Scope(), frag=frag, expires=exp)
        return True

    # ---- "no VERBing [object]" / "no pushes to X" (only at clause start)
    m = re.match(r"^no\s+(force[- ]?push(?:es|ing)?|push(?:es|ing)?|commits?|committing|deleting|deletions?|deploys?|deploying|deployments?|installs?|installing|removing|rebasing)\b\s*(.*)$", low)
    if m:
        acts = _actions_in(m.group(1))
        done = False
        for a in acts:
            done |= _action_rules(ctx, "forbid", a, cl[m.start(2):m.end(2)], frag, exp)
        return done

    # ---- generic: NEG VERB(s) OBJECT  (forbid / confirm / temporary)
    m = re.match(rf"^(?:you\s+)?(?:{_NEG}){_ADV}\s+(?:use\s+)?(?:git\s+)?((?:{_V})(?:\s*(?:,|/|\bor\b|\band\b|\bnor\b)\s*(?:{_V}))*)\b\s*(.*)$", low)
    if m:
        verbs, rest = m.group(1), m.group(2)
        if _GIT_PUSH_F.search(verbs + " " + rest[:20]) and (verbs.startswith("push") or verbs.startswith("force")):
            acts = ["force_push"]
            rest = re.sub(r"^(?:-f|--force\S*|force|with\s+force)\b\s*", "", rest)
        else:
            if re.match(r"force\b", rest) and verbs.startswith("push"):
                verbs, rest = "force-push", rest[5:]
            acts = _actions_in(verbs)
        if not acts:
            return False
        rest_orig = cl[m.end(0) - len(rest):m.end(0)] if rest else ""
        kind = "forbid"
        if _WITHOUT_ASKING.search(low):
            kind = "confirm"
        until_say = bool(_UNTIL_I_SAY.search(low))
        done = False
        for a in acts:
            r_kind, r_exp = kind, exp
            if until_say:
                # "don't change anything until I say" is a hold on work; "don't push until I say so" asks once
                res = _resolve(rest_orig) if a == "modify" else None
                if a == "modify" and res and res.kind == "universal":
                    r_kind, r_exp = "forbid", "next_turn"
                else:
                    r_kind, r_exp = "confirm", ""
            if r_kind == "confirm":
                r_exp = ""
            done |= _action_rules(ctx, r_kind, a, rest_orig, frag, r_exp)
        return done
    return False


def extract_rules(text: str, source: str = "extracted", turn: int = 0) -> list[Rule]:
    """Extract constraint rules from a natural-language prompt. Precision-first."""
    t = _normalise(text)
    ctx = _Ctx(t, source, turn)
    if not t:
        return []
    for sent in _sentences(t):
        s = sent.strip()
        # explicit syntax: "holdfast: forbid delete data/**"
        m = re.match(r"holdfast:\s*(forbid|confirm|require_before|freeze)\s+(\w+)(?:\s+(.*))?$", s, re.I)
        if m:
            kind, action, rest = m.group(1).lower(), m.group(2).lower(), (m.group(3) or "").strip()
            scope, prereq = Scope(), ""
            if kind == "require_before":
                parts = rest.split(None, 1)
                prereq = parts[0] if parts else "tests_pass"
                rest = parts[1] if len(parts) > 1 else ""
            if rest:
                scope = Scope(paths=[p.strip() for p in rest.split(",")]) if not rest.startswith("/") else Scope(pattern=rest.strip("/"))
            ctx.add(kind, action, scope, frag=s, prereq=prereq)
            continue
        # remember explicit paths mentioned in task text (for "stay within that folder")
        body = _LEAD.sub("", s)
        if _QUESTION.match(body) and s.endswith("?"):
            continue
        if _CODE_DESC.match(body):
            continue
        sent_temp = bool(_TEMP.search(s))
        carry = False
        for c in _clauses(s):
            if not c:
                continue
            if _TEMP_ONLY.fullmatch(c.strip(" ,.!")):
                carry = True
                continue
            temp = carry or bool(_TEMP.search(c)) or (sent_temp and not re.search(r"\bafter\s+this\s+point\b", c, re.I))
            # temporal marker words must not leak into a freeze ("after this point" is standing)
            if re.search(r"\b(?:after\s+this\s+point|from\s+(?:now|this\s+point|here)\s+on)\b", c, re.I):
                temp = False
            before = len(ctx.rules)
            _clause_rules(ctx, c, s, temp)
            if len(ctx.rules) == before:
                pm = [m for m in _PATHISH.finditer(c) if "/" in next(g for g in m.groups() if g)]
                if pm:
                    ctx.last_path = [_clean_path(next(g for g in pm[-1].groups() if g))]
    return ctx.rules


_CUE = re.compile(r"\b(?:don'?t|do not|never|no|not|must|mustn'?t|shouldn'?t|should not|only|avoid|refrain|without|before|until|unless|ask|check with|confirm|approval|permission|sign-?off|off[- ]limits|read[- ]only|frozen|leave|keep|stay|hold off|hands off|careful|forbidden|prohibited|allowed)\b", re.I)


def has_constraint_cue(text: str) -> bool:
    """Cheap gate for the hybrid extractor: does the message contain any modal / negation / permission cue?"""
    return bool(_CUE.search(text))


# ---------------------------------------------------------------------------
# LLM extractor (recall fallback) via the local `claude` CLI — runs under the user's subscription.
# ---------------------------------------------------------------------------

LLM_ACTIONS = ["delete", "modify", "push", "force_push", "commit", "git_history", "install", "network", "deploy", "run"]

LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "action": {"type": "string", "enum": LLM_ACTIONS},
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "exclude_paths": {"type": "array", "items": {"type": "string"}},
                    "branches": {"type": "array", "items": {"type": "string"}},
                    "pattern": {"type": "string"},
                    "command": {"type": "string"},
                    "prerequisite": {"type": "string", "enum": ["", "tests_pass"]},
                    "expires": {"type": "string", "enum": list(EXPIRES)},
                    "text": {"type": "string"},
                },
                "required": ["kind", "action", "text"],
            },
        }
    },
    "required": ["rules"],
}

LLM_PROMPT = """You turn one user message to a coding agent into machine-enforced constraint rules (JSON).

Extract ONLY constraints on the agent's own actions. Most messages contain none: ordinary task instructions
("delete the unused imports", "push when done", "install pytest"), questions, permissions ("feel free to delete"),
releases ("don't worry about tests"), goals ("make sure tests pass") and descriptions of code to write
("add a hook that blocks force pushes") yield {"rules": []}.

Rule fields:
- kind: forbid (never do it) | confirm (ask the user before each one; also "don't X without asking", "don't X until I say so")
        | require_before (only after tests pass; set prerequisite "tests_pass") | freeze (don't modify X once work has started / after this point)
- action: delete | modify (edit/write/touch/change files) | push | force_push | commit | git_history (rebase/amend/rewrite history)
          | install (packages/dependencies) | network | deploy | run (a specific command; put a regex for it in "command")
- paths: globs relative to the repo root for the protected files: a directory "src/core/" -> "src/core/**", a file stays as is, tests -> "tests/**".
- exclude_paths: for "only edit files under X" / "stay within X" / "don't change anything outside X": kind forbid, action modify, exclude_paths ["X/**"].
- branches: for "don't push to main": ["main"]. A remote name like origin is not a branch (leave empty).
- pattern: "prod" for production-wide rules ("never touch prod" = three forbid rules: modify, deploy and run, each with pattern "prod").
- expires: "next_turn" for a temporary hold ("don't change anything yet", "for now", "at this point", "hold off"); otherwise "".
- text: the clause the rule came from.
Leave scope fields empty for "anything"/"any files". If you cannot name the object as a path, branch or prod, emit no rule for it.

Message:
<<<
%s
>>>"""


def parse_llm_rules(payload, text: str, turn: int = 0) -> list[Rule]:
    """Validate an LLM payload against the rule schema; silently drop anything malformed."""
    if not isinstance(payload, dict) or not isinstance(payload.get("rules"), list):
        return []
    out: list[Rule] = []
    for r in payload["rules"]:
        if not isinstance(r, dict):
            continue
        kind, action = r.get("kind"), r.get("action")
        if kind not in KINDS or action not in LLM_ACTIONS:
            continue
        expires = r.get("expires") or ""
        if expires not in EXPIRES:
            continue
        prereq = r.get("prerequisite") or ""
        if kind == "require_before":
            prereq = "tests_pass"

        def strs(k):
            v = r.get(k) or []
            return [x for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []
        pattern, command = r.get("pattern") or "", r.get("command") or ""
        try:
            for rx in (pattern, command):
                if rx:
                    re.compile(rx)
        except (re.error, TypeError):
            continue
        branches = []
        for b in strs("branches"):
            if b.lower() not in _REMOTE_WORDS:
                branches += [b, "*/" + b] if not b.startswith("*/") else [b]
        sc = Scope(paths=[_clean_path(p) for p in strs("paths")], exclude_paths=[_clean_path(p) for p in strs("exclude_paths")],
                   branches=branches, pattern=pattern if isinstance(pattern, str) else "", command=command if isinstance(command, str) else "")
        frag = r.get("text") if isinstance(r.get("text"), str) and r.get("text") else text
        rule = Rule(id=make_id(frag, kind, action + str(sc) + expires), kind=kind, action=action, scope=sc, text=frag,
                    source="llm", prerequisite=prereq, created_turn=turn, expires=expires)
        if all(x.id != rule.id for x in out):
            out.append(rule)
    return out


def extract_rules_llm(text: str, turn: int = 0, model: str = "haiku", timeout: int = 60) -> list[Rule]:
    """Recall fallback: ask Claude (local `claude` CLI, i.e. the user's subscription) for rules.

    Runs `claude -p --model <model> --output-format json --json-schema ...` from a neutral temp directory
    (so project hooks / CLAUDE.md don't leak in) and validates the structured output. Returns [] on any failure.
    """
    import shutil
    import subprocess
    import tempfile
    exe = shutil.which("claude")
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [exe, "-p", LLM_PROMPT % text, "--model", model, "--output-format", "json",
             "--json-schema", json.dumps(LLM_SCHEMA), "--no-session-persistence", "--tools", ""],
            capture_output=True, text=True, timeout=timeout, cwd=tempfile.gettempdir(),
        )
        data = json.loads(proc.stdout)
        payload = data.get("structured_output")
        if payload is None:
            res = data.get("result", "") or ""
            m = re.search(r"\{.*\}", res, re.S)
            payload = json.loads(m.group(0)) if m else None
    except Exception:
        return []
    return parse_llm_rules(payload, text, turn)
