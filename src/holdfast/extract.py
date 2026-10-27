"""Pattern-based natural-language -> rule extraction (SPEC §4 UserPromptSubmit, A4).

Deterministic, stdlib-only, no ML in the hot path.  Four layers:

1. **Clause splitting** (``_clauses``): on ``. ; ! ? —``/newlines, and on
   ``,``/``and``/``but`` only when a constraint cue follows ("don't", "never",
   "no", "ask", "only", "stay", ...).  Paths mentioned earlier in the message
   are kept as *antecedents*: "Fix tests/test_io.py, but don't delete it" scopes
   the rule to that file; a bare "don't delete it" (no antecedent) yields nothing.
2. **Normalisation** (``_normalise``): lower-case; fix typos (dont/donot/instal/
   w/o/pls/u); collapse synonyms (remove|rm|wipe|nuke -> delete, deps|packages ->
   packages, curl|wget|internet -> network, production -> prod, push -f ->
   forcepush); fold whole phrases into three tokens: ``don't`` (every modal or
   polite prohibition), ``askme`` (ask / check with me / needs my sign-off /
   until I say so) and ``tests_ok`` (run the tests / tests pass / pytest is
   green).  Path tokens are protected, so ``config/production.yml`` survives.
3. **Intent templates** (``_TEMPLATES``), first match wins: CONFIRM (``askme
   before X``, ``don't X without askme``, ``every X needs askme``), ORDER
   (``tests_ok before X``, ``only X after tests_ok``), SCOPE (``only edit in X``,
   ``keep edits to X``, ``stay within X``, ``don't edit outside X``),
   OFF-LIMITS / HANDS-OFF / EXCEPT, then the generic FORBID (``don't|never|no X``).
4. **Action phrases** (``_acts``): ``X`` ("delete or install anything under
   build/") is split on or/and; each part becomes a head word (delete, edit,
   push, forcepush, commit, install, run, deploy, network, prod, destructive)
   plus an object that decides the target.  "outside X" turns a forbid into a
   scope, "prod" turns edits/runs into touch_prod, a pronoun resolves to the
   antecedent or drops the rule, and an unknown object on delete/edit drops the
   rule (precision over recall: a false rule silently blocks the agent).

Precedence: a task sentence with a restriction clause yields only the
restriction (the task part has no cue).  An explicit path beats a "prod"
keyword ("Don't touch the prod config (config/prod.yml)" -> edit_path).  A
``push`` rule subsumes ``force_push`` (the classifier labels a force push as
both), so the redundant force_push rule is dropped.  Also recognised:
confirmations ("yes, delete it") and revocations ("never mind"); partial or
contrastive answers ("yes to the push, no to the force push", "yes but don't
delete yet") are *not* confirmations.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .rules import Rule, RuleError


@dataclass
class Confirmation:
    action: str                 # category or 'any'
    target: str | None = None
    text: str = ""


@dataclass
class Extraction:
    rules: list[dict[str, Any]] = field(default_factory=list)
    confirmations: list[Confirmation] = field(default_factory=list)
    revoked: bool = False


@dataclass
class _Ctx:
    """Per-clause context: last path from an earlier clause, paths in this clause, lower->original case."""
    antecedent: str | None = None
    explicit: list[str] = field(default_factory=list)
    case: dict[str, str] = field(default_factory=dict)


_P = re.compile


def _w(p: str) -> str:  # word boundary that also refuses to match inside a path token
    return r"(?<![\w./\-*])(?:" + p + r")(?![\w./\-*])"


# ------------------------------------------------------------------ normalisation

_PARTICIPLE = {"deleted": "deleting", "pushed": "pushing", "committed": "committing", "installed": "installing",
               "edited": "editing", "forcepushed": "forcepush", "deployed": "deploying", "run": "running"}
_CUE = (r"(?:don't|do not|dont|donot|never|no\b|only\b|ask|check|confirm|make sure|run |keep |stay |leave |you |"
        r"please|pls|let me know|get my|hands off|avoid|refrain|before |work only|from now|going forward|i'd prefer|"
        r"i want to review|i'd rather)")
_CONJ_CUE = r"(?:don't|do not|dont|donot|never|no\b|only\b|make sure|stay |keep |leave |hands off|avoid|refrain|i'd prefer|i'd rather)"
_SPLIT = _P(r"\.(?=\s|$)|[;!?\n]+|\s[—–]\s|—|\s-\s|,?\s+(?:and|but|also|plus)\s+(?=" + _CONJ_CUE + r")|,\s*(?=" + _CUE + ")")
_PATH_TOKEN = _P(r"(?<![\w@])((?:[\w.\-]+/)+[\w.\-*]*|\.?[\w\-]+\.[a-z]{1,5}|\.[a-z]\w+)(?![\w/])")

_SUBS: list[tuple[re.Pattern[str], Any]] = [(_P(p), r) for p, r in [
    # typos / contractions / glyphs
    (r"\b(?:dont|donot|do not|do n't)\b", "don't"), (r"\bw/o\b", "without"), (r"\b(?:pls|plz)\b", "please"),
    (r"\bu\b", "you"), (r"\bur\b", "your"), (r"\bb4\b", "before"), (r"\binstal\b", "install"),
    (r"\s*(?:→|->|=>)\s*", " then "),
    # modal / polite prohibitions -> don't ; emphatic -> never
    (r"\b(?:you|we) (?:must|should|may|can|could|shall) ?(?:not|n't|never)\b", "don't"),
    (r"\byou (?:mustn't|shouldn't|can't|cannot|aren't to|are not to|are never to)\b", "don't"),
    (r"\byou(?:'re| are)(?: absolutely)? not (?:allowed|permitted|supposed|authori[sz]ed) to\b", "don't"),
    (r"\b(?:i|we) (?:don't|do not|wouldn't|would not) (?:want|like|need) you to\b", "don't"),
    (r"\b(?:i|we)(?:'d| would) (?:prefer|rather)(?: that)?(?: if)? you (?:not|didn't|did not|don't|do not|wouldn't)\b", "don't"),
    (r"\bit(?:'s| is) not (?:ok|okay|fine|acceptable|allowed|permitted) to\b", "don't"),
    (r"\b(?:refrain from|avoid|abstain from)\b", "don't"),
    (r"\bunder no circumstances(?: should you| are you to| may you)?\b", "never"),
    (r"\b(?:make sure|be careful|take care|remember|be sure) (?:you don't|not to|to never|you never|that you don't)\b", "don't"),
    (r"\b(?:don't|never|not) ever\b", "never"), (r"\bnever,? under any circumstances,?\b", "never"),
    (r"\bno more\b", "no"),
    # git
    (r"\b(?:git )?push (?:-f|--force(?:-with-lease)?|force|forced|forcefully)\b", "forcepush"),
    (r"\bforce[- ]?push(?:es|ed|ing)?\b|\bforced push(?:es)?\b|\b--force\b", "forcepush"),
    (r"\bgit (push|commit|rm)\b", r"\1"),
    # delete family
    (_w(r"rm(?: -\w+)*|rmdir|unlink|shred"), "delete"),
    (r"\b(?:remove|erase|wipe|destroy|trash|nuke|purge|obliterate|blow away|clear out|get rid of)\b", "delete"),
    (r"\b(?:removing|erasing|wiping|destroying|trashing|nuking|purging|blowing away|clearing out|getting rid of)\b", "deleting"),
    (r"\b(?:removed|erased|wiped|destroyed|trashed|nuked|purged)\b", "deleted"),
    (r"\b(?:removals?|deletions?|deletes|wipes)\b", "deletion"),
    # edit family
    (r"\b(?:make|making|made) (?:any |your |all |some )?(?:changes|edits|modifications|alterations) (?:to|in|on|under|inside|within)\b", "edit"),
    (r"\b(?:modify|change|alter|touch|overwrite|rewrite|rename|mess with|tamper with|update|write to)\b", "edit"),
    (r"\b(?:modifying|changing|altering|touching|overwriting|rewriting|renaming|messing with|tampering with|updating|writing to)\b", "editing"),
    (r"\b(?:modified|changed|altered|touched|overwritten|rewritten)\b", "edited"),
    (r"\b(?:changes|modifications|alterations)\b", "edits"),
    # install family
    (r"\b(?:pip3?|npm|pnpm|yarn|apt-get|apt|brew|cargo|gem|uv pip|uv|poetry|conda) (?:install|add|i)\b", "install"),
    (r"\bapt-get\b", "install"),
    (r"\b(?:dependencies|dependency|deps?|packages?|libraries|library|libs|modules|gems|crates)\b", "packages"),
    (r"\b(?:add|pull in|bring in|introduce|vendor|adding|pulling in|bringing in|introducing|vendoring)"
     r" ((?:(?:any|new|additional|extra|more|other|third[- ]party|external) )*)packages\b", r"install \1packages"),
    (r"\binstallations?\b|\binstalls\b", "installs"),
    # network
    (r"\b(?:curl|wget|the internet|internet|the web|the network|outbound|external (?:network|services?|apis?)|"
     r"https? requests?|web requests?|api calls?)\b", "network"),
    # prod
    (_w(r"(?:the |our |my |a )?(?:prod|production|prd)(?: environment| env| servers?| cluster| db| database| site| infra|"
        r" infrastructure| systems?| data| deployment| k8s| kubernetes| namespace| instance| box)?|the live (?:site|servers?|env|environment|db|database)"), "prod"),
    # tests -> tests_ok
    (r"\b(?:pytest|the test suite|test suite|the tests|unit tests|all tests|all the tests|the ci|ci|test cases|the unit tests|"
     r"the full test suite|full test suite|your tests|my tests|our tests|testsuite|npm test|go test|cargo test|make test|tests)\b", "tests"),
    (r"\b(?:make sure|ensure|verify|check|be sure|see|confirm|double[- ]check)(?: that)? (?:all |the )?tests\b", "tests"),
    (r"\b(?:run(?:ning)?|re-?run(?:ning)?|execut(?:e|ing)) (?:all |the |your )?tests(?: (?:successfully|first|and (?:they |make sure they |ensure they )?pass))?\b", "tests_ok"),
    (r"\btests (?:(?:is|are|all|must|should|have to|has to|need to|had better|ought to) )?(?:be |all |be run and |run and |are run and )?"
     r"(?:pass(?:es|ing|ed)?|green|succeed(?:s|ed)?|ok|okay|successful|clean|happy)(?: successfully)?\b", "tests_ok"),
    (r"\b(?:green|passing|successful) tests\b", "tests_ok"),
    # ask cues -> askme (after tests so "confirm that tests pass" is not a confirm cue)
    (r"\bmy (?:explicit |prior )?(?:ok|okay|approval|permission|sign-?off|go-?ahead|say-?so|consent|blessing|green light)\b", "askme"),
    (r"\b(?:an? |the |explicit |prior |your )*(?:permission|approval|consent|confirmation|sign-?off|go-?ahead|green light|blessing)(?: from me)?\b", "askme"),
    (r"\b(?:ask|check|confirm|verify|clear it|check in|run it by|consult|coordinate|talk|speak|double[- ]check) (?:with )?me\b", "askme"),
    (r"\b(?:ask|check|confirm|verify)(?= (?:first|before|prior|explicitly|beforehand)\b|$)", "askme"),
    (r"\b(?:asking|checking|confirming|clearing it|consulting|checking in)(?: with)?(?: me)?(?=$| first| explicitly|,)", "askme"),
    (r"\b(?:i|me) (?:explicitly |first |have |personally )*(?:approve(?:d)?|say so|say (?:it's |it is )?(?:ok|okay|fine|go)|"
     r"give (?:you )?the (?:ok|okay|go-?ahead|green light)|sign(?:ed)? off|ok it|okay it|confirm|green-?light(?: it)?|"
     r"review(?:ed)?(?: it| them)?|look(?:ed)?(?: at it)?)\b", "askme"),
    (r"\bi want to (?:review|see|check|look|approve|know)(?: it| them| first)?\b", "askme"),
    (r"\blet me (?:know|review|check|see|approve|look|look at it|decide)(?: first| beforehand)?\b", "askme"),
    (r"\b(?:get|ask for|obtain|request|have|wait for|getting|having|obtaining|requesting|receive|receiving|secure|"
     r"you've got|you have|you get|you got|with|given) askme\b", "askme"),
    (r"\baskme(?: (?:first|explicitly|beforehand|each time|every time))?(?: askme)*\b", "askme"),
    # passive -> gerund: "anything gets pushed" -> "pushing anything"
    (r"\b(anything|any files?|any file|stuff|things|something|code|files|edits|it|they|that) (?:gets?|is|are|being|be|has been|have been|is being|are being) "
     r"(deleted|pushed|committed|installed|edited|forcepushed|run|deployed)\b", lambda m: f"{_PARTICIPLE[m.group(2)]} {m.group(1)}"),
    # remotes are not targets
    (r"\b(?:to|into|onto|at|upstream to) (?:the )?(?:remote|origin|upstream|github|gitlab|remote repo|the remote repo)\b", ""),
    (r"\bplease\b|\bkindly\b", ""), (r"\s+", " "),
]]
_LEAD = _P(r"^(?:(?:please|pls|also|and|but|oh|btw|so|just|hey|then|plus|additionally|importantly|remember|important|note|nb|ps|"
           r"rule|rules|constraint|constraints|one more thing|fyi|again|this time|from now on|going forward|from here on(?: out)?|"
           r"in general|for this task|whatever you do|kindly|always)[,:!.\s-]+|(?:yes|ok|okay|sure|wait|actually|hmm|well|no|right)[,:!]+\s*)+")
_TAIL = _P(r"(?:[,\s]+(?:ok|okay|please|thanks|thank you|alright|got it|right|yeah|ever|at all|for now|for this task|for me|"
           r"on my behalf|yet|this time|though|first)[?!.]*)+$")


def _clauses(text: str) -> list[str]:
    text = re.sub(r"\s*(?:→|->|=>)\s*", " then ", text or "")
    return [c.strip() for c in _SPLIT.split(text) if c and c.strip()]


def _normalise(clause: str) -> str:
    c = re.sub(r"\([^)]*\)", " ", clause.lower())
    for rx, rep in _SUBS:
        c = rx.sub(rep, c)
    c = c.strip(" ,;:!.-")
    prev = None
    while prev != c:
        prev = c
        c = _TAIL.sub("", _LEAD.sub("", c)).strip(" ,;:!.-")
    return c.strip()


# ------------------------------------------------------------------ targets

NOUN_TARGETS = {**dict.fromkeys(["tests", "test", "test files", "test suite", "unit tests", "testing"], "tests"),
                **dict.fromkeys(["lockfile", "lock file", "lockfiles", "lock files"], "*lock*"),
                "spec": "spec", "specs": "spec", "docs": "docs", "documentation": "docs", "migrations": "migrations",
                "migration files": "migrations", "config": "config", "configs": "config", "configuration": "config",
                "fixtures": "fixtures", "snapshots": "snapshots", "vendor": "vendor", "node_modules": "node_modules",
                "main": "main", "master": "master", "src": "src", "lib": "lib", "scripts": "scripts", "build": "build", "dist": "dist"}
COMMON_DIRS = {"src", "lib", "tests", "test", "docs", "build", "dist", "config", "scripts", "migrations", "fixtures",
               "vendor", "app", "api", "core", "utils", "examples", "assets", "public", "static", "templates", "data",
               "tools", "bin", "pkg", "cmd", "internal", "spec", "out", "target", "cache", "frontend", "backend"}
PRONOUNS = {"it", "that", "this", "them", "those", "these", "him", "her", "there"}
_PATHLIKE = _P(r"^[\w.\-/*@~]+$")
_TRAIL_WORDS = _P(r"\s+(?:directory|dir|folder|files?|tree|code|branch|package|module|subtree|cluster)$")
_LEAD_WORDS = _P(r"^(?:the|any|all|of|my|our|your|this|that|those|these|entire|whole|existing|same)\s+")
_GENERIC = _P(r"^(?:(?:any|all|the|some|more|new|additional|existing|other|my|your)\s+)*"
              r"(?:thing|things|anything|stuff|files?|everything|code|data|something|whatever|any|all)?"
              r"(?:\s+(?:new|else|at all|more|additional|whatsoever))?$")
_DEMONSTRATIVE = _P(r"^(?:(?:in|inside|within|under|to) )?(?:that|this|the same|it|there|the current)\b")


def norm_target(obj: str | None) -> str | None:
    """Turn a noun phrase into a glob-ish target, or None when not path-like."""
    if obj is None:
        return None
    t = obj.strip().strip("`'\"").strip(" .,;:!?)(").lower()
    t = _LEAD_WORDS.sub("", _TRAIL_WORDS.sub("", _LEAD_WORDS.sub("", t))).strip("`'\"").rstrip("/")
    if t.startswith("./"):
        t = t[2:]
    if t in NOUN_TARGETS:
        return NOUN_TARGETS[t]
    if t in ("prod", "production"):
        return "prod*"
    if _PATHLIKE.match(t) and ("/" in t or "." in t or "*" in t or t in COMMON_DIRS):
        return t
    return None


def _target(obj: str, ctx: _Ctx) -> tuple[str, str | None]:
    """Resolve a delete/edit object: ('none'|'target'|'outside'|'prod'|'skip', target)."""
    o = obj.strip().strip(" .,;:!?")
    if _GENERIC.match(o):
        return "none", None
    m = re.match(r"^(?:anything |files |stuff |code |any files? )?outside (?:of )?(.+)$", o)
    if m:
        t = norm_target(m.group(1))
        return ("outside", t) if t else ("skip", None)
    if o in PRONOUNS or (o.split()[0] in PRONOUNS and len(o.split()) <= 2):
        return ("target", ctx.antecedent) if ctx.antecedent else ("skip", None)
    m = re.match(r"^(?:(?:anything|any files?|files|stuff|things|everything|code|something|any|all|any of the files)\s+)?"
                 r"(?:in|under|inside|within|from|of|at|below)\s+(.+)$", o)
    t = norm_target(m.group(1) if m else o)
    if t == "prod*":
        t = None
    if t:
        return "target", ctx.case.get(t, t)
    if ctx.explicit:
        return "target", ctx.explicit[-1]
    if re.search(r"\bprod\b", o):
        return "prod", None
    return "skip", None


_CMD_STOP = {"it", "that", "this", "them", "anything", "any", "tests", "tests_ok", "the", "a", "an", "commands",
             "scripts", "something", "code", "things", "stuff", "suite", "build", "everything", "again", "prod"}


def _cmd_target(obj: str) -> str | None:
    o = re.sub(r"^(?:the|any|a|an)\s+", "", obj.strip().strip("`'\" .,;:!?"))
    o = re.sub(r"\s+(?:command|cli|tool|binary|commands|utility)$", "", o)
    words = o.split()
    if not words or len(words) > 3 or words[0] in _CMD_STOP or any(w in ("tests", "tests_ok") for w in words):
        return None
    if not all(re.match(r"^[\w.\-/+=:@]+$", w) for w in words):
        return None
    return " ".join(words)


# ------------------------------------------------------------------ action phrases

_HEAD = _P(r"^(?:(?:ever|use|using|do|doing|run|running|try to|attempt to|attempting|git|a|an|any|more|new|the|make|making|"
           r"do any|perform|performing|issue|send|hit|call|access|allow|go|be|get|start|to|any kind of|all|every|each|you) )*"
           r"(delete|deleting|deletion|deletes|deleted|edit|editing|edits|edited|forcepush(?:ing|es)?|push(?:ing|es)?|"
           r"commit(?:ting|s)?|install(?:ing|s)?|packages|run|running|execute|executing|invoke|invoking|use|using|"
           r"deploy(?:ing|s)?|network|download(?:ing)?|fetch(?:ing)?|upload(?:ing)?|prod|destructive git (?:commands|operations|ops)|"
           r"(?:anything |any \w+ )?(?:destructive|irreversible|dangerous|risky))(?:\s+(.*))?$")
_REMOTE = {"remote", "github", "origin", "upstream", "gitlab"}
_HEAD_ACTION = {"commit": "commit", "committ": "commit", "install": "install", "packages": "install", "deploy": "touch_prod",
                "prod": "touch_prod", "network": "network", "download": "network", "fetch": "network", "upload": "network"}


def _branch(obj: str) -> str | None:
    m = re.match(r"^(?:(?:anything|edits|code|commits|any commits|this|stuff) )?(?:to|into|onto|at|upstream to) (?:the )?(.+?)(?: branch)?$", obj.strip())
    if not m:
        return None
    t = norm_target(m.group(1))
    return None if t in _REMOTE else t


def _acts(phrase: str, ctx: _Ctx) -> list[tuple[str, str | None]]:
    """'delete or install anything under build/' -> [('delete','build'), ('install',None)]."""
    phrase = _TAIL.sub("", phrase.strip()).strip(" ,.;:")
    parts = [p.strip() for p in re.split(r"\s*(?:,|\bor\b|\band\b)\s*", phrase) if p.strip()]
    heads: list[tuple[str, str]] = []
    for p in parts:
        m = _HEAD.match(p)
        if m:
            heads.append((m.group(1), (m.group(2) or "").strip()))
        elif not heads:
            return []                              # "no worries, push whenever": the phrase must open with an action
        elif not heads[-1][1]:
            heads[-1] = (heads[-1][0], p)          # "touch or delete anything in x/": object binds to the last verb
    last_obj = heads[-1][1] if heads else ""
    out: list[tuple[str, str | None]] = []
    for head, obj in heads:
        obj = obj or last_obj
        if head.startswith(("delet", "edit")):
            kind, tgt = _target(obj, ctx)
            if kind == "prod":
                out.append(("touch_prod", None))
            elif kind == "outside" and head.startswith("edit"):
                out.append(("scope", tgt))
            elif kind in ("none", "target"):
                out.append(("delete" if head.startswith("delet") else "edit_path", tgt))
        elif head.startswith(("run", "execut", "invok", "us")):
            if re.search(r"\bnetwork\b", obj):
                out.append(("network", None))
            elif re.search(r"\bprod\b", obj):
                out.append(("touch_prod", None))
            elif _cmd_target(obj):
                out.append(("run_cmd", _cmd_target(obj)))
        elif head.startswith(("forcepush", "push")):
            out.append(("force_push" if head.startswith("force") else "push", _branch(obj)))
        elif head.startswith("destructive git"):
            out.append(("delete", None))
        elif head.endswith(("destructive", "irreversible", "dangerous", "risky")):
            out += [("delete", None), ("force_push", None)]
        else:
            out.append((_HEAD_ACTION[head.rstrip("s").split("ing")[0] if head != "packages" else head], None))
    return out


# ------------------------------------------------------------------ templates

def _rules(kind: str, acts: list[tuple[str, str | None]], requires: str | None = None) -> list[dict[str, Any]]:
    out = []
    for a, t in acts:
        if a == "scope":
            out.append({"kind": "scope", "action": "edit_path", "target": t, "requires": None})
        elif kind == "order":
            if a in ("push", "commit", "force_push"):
                out.append({"kind": "order", "action": a, "target": None, "requires": requires})
        else:
            out.append({"kind": kind, "action": a, "target": t, "requires": None})
    return out


def _scope(obj: str, ctx: _Ctx) -> list[dict[str, Any]]:
    o = re.sub(r"\s+only$", "", obj.strip().strip(" .,;:"))
    t = norm_target(o) or (ctx.antecedent if _DEMONSTRATIVE.match(o) else None)
    return [{"kind": "scope", "action": "edit_path", "target": ctx.case.get(t, t), "requires": None}] if t else []


def _offlimits(subject: str, ctx: _Ctx) -> list[dict[str, Any]]:
    """'deleting files' / 'prod' / 'src/legacy/' is off limits."""
    s = subject.strip().strip(" .,;:")
    if re.fullmatch(r"(?:the |our )?prod", s):
        return _rules("forbid", [("touch_prod", None)])
    acts = _acts(s, ctx)
    if acts:
        return _rules("forbid", acts)
    t = norm_target(s)
    return _rules("forbid", [("edit_path", ctx.case.get(t, t))]) if t else []


ACTS = (r"((?:(?:a |every |any |each |you |doing a |making a )?(?:push(?:ing|es)?|commit(?:ting|s)?|forcepush(?:ing)?))"
        r"(?:(?: or | and |, )(?:push(?:ing|es)?|commit(?:ting|s)?|forcepush(?:ing)?))*)")
ASK_LEAD = r"(?:you |you've |you have |first |explicitly |i |having |getting |get |you first |you've first )*"
# (regex on the normalised clause, intent); the first template that yields rules wins.
_TEMPLATES: list[tuple[re.Pattern[str], str]] = [(_P(p), k) for p, k in [
    # CONFIRM: askme before X / before X askme / don't X without askme / every X needs askme / you may X but askme
    (r"^askme (?:before|prior to|ahead of) (?:you |any |every |each |all )?(.+)$", "confirm"),
    (r"^before (?:you |any |every |each )?(.+?),? (?:you )?(?:must |should |have to |need to |always )?askme$", "confirm"),
    (r"^(?:don't|never|no) (.+?) (?:without|unless|until|till|before|prior to) " + ASK_LEAD + r"askme$", "confirm"),
    (r"^(?:any |every |each |all |you may |you can |you're allowed to |you are allowed to |feel free to )?(.+?)"
     r"\W*(?:then|needs|requires|need|require|means|implies|but|only after|only with|only if you)?\s*askme$", "confirm"),
    # ORDER: only X after tests_ok / tests_ok before X / before X, tests_ok / don't X until tests_ok
    (r"^(?:only )?" + ACTS + r"(?: only)?(?: anything| edits| code| commits)? (?:after|once|when|if|unless) tests_ok$", "order"),
    (r"^(?:always |first |you should |you must )*tests_ok (?:first )?before (?:you |every |any |each )?" + ACTS + r"$", "order"),
    (r"^before (?:you |any |every |each )?" + ACTS + r",? (?:you )?(?:must |should |always )?tests_ok$", "order"),
    (r"^(?:don't|never|no) " + ACTS + r"(?: anything| edits| code)? (?:until|unless|before|if|when|without|till) tests_ok$", "order"),
    # SCOPE: only edit in X / work only inside X / keep edits to X / edits must stay in X / stay within X / stick to X
    (r"^only (?:ever )?(?:edit|editing|work|write|make edits|operate)(?: (?:files|stuff|things|code|the files|the code|source))?"
     r"(?: (?:in|under|inside|within|to|on|of))? (.+)$", "scope"),
    (r"^(?:edit|work|write|operate|stay|remain) only (?:in|inside|under|within|on) (.+)$", "scope"),
    (r"^(?:keep|restrict|limit|confine|scope) (?:all |your |the |any |code )?(?:edits|work|edits only) "
     r"(?:to|in|under|inside|within|limited to|confined to) (?:the )?(.+)$", "scope"),
    (r"^(?:all |your |any )?edits (?:should|must|need to|have to|are to) (?:be |stay |go |remain |live |be made |be kept )?"
     r"(?:in|under|inside|within|limited to|confined to|only in|only under) (?:the )?(.+)$", "scope"),
    (r"^(?:stay|remain|keep) (?:within|inside|in) (.+)$", "scope"),
    (r"^stick to (?:editing |files in |files under |the )?(.+)$", "scope"),
    # OFF LIMITS / HANDS OFF / LEAVE ALONE / EXCEPT
    (r"^(.+?) (?:is|are) (?:off[- ]limits|forbidden|prohibited|banned|not allowed|not permitted|not ok|not okay|out of bounds|"
     r"untouchable|read[- ]only|frozen|a no-go|verboten|not an option|not to be (?:edited|deleted))$", "offlimits"),
    (r"^(?:hands off|stay (?:away from|out of|off|clear of)|keep (?:away from|out of|off|your hands off)|steer clear of|don't go near) (.+)$", "offlimits"),
    (r"^leave (.+?) (?:alone|as is|as-is|untouched|unchanged|be|the way it is)$", "offlimits"),
    (r"^(?:you (?:can|may)|feel free to) (edit|editing|delete|deleting)(?: anything| any files?| everything| whatever(?: you (?:want|like))?)?"
     r" (?:except|but not|apart from|other than|excluding|save for|aside from) (?:for |the )?(.+)$", "except"),
    # NETWORK special forms, then the generic FORBID
    (r"^(?:stay|work|remain|keep it|operate|run|do everything|be) (?:fully |completely |entirely )?offline\b|^offline only\b", "network"),
    (r"^(?:don't|never|no) (?:ever )?(.+)$", "forbid"),
]]


def _apply(intent: str, m: re.Match[str], ctx: _Ctx) -> list[dict[str, Any]]:
    g = m.group(1) or "" if m.re.groups else ""
    if intent == "confirm":
        return _rules("confirm", _acts(g, ctx))
    if intent == "order":
        return _rules("order", _acts(g, ctx), "tests_passed")
    if intent == "scope":
        return _scope(g, ctx)
    if intent == "offlimits":
        return _offlimits(g, ctx)
    if intent == "except":
        return _rules("forbid", _acts(f"{g} {m.group(2)}", ctx))
    if intent == "network":
        return _rules("forbid", [("network", None)])
    return _rules("forbid", _acts(g, ctx))


def _validate(rule: dict[str, Any]) -> bool:
    try:
        Rule(id="tmp", kind=rule["kind"], action=rule["action"], target=rule.get("target"), requires=rule.get("requires"))
        return True
    except RuleError:
        return False


def _paths_in(clause: str) -> list[str]:
    out = []
    for tok in _PATH_TOKEN.findall(clause):
        tok = tok.rstrip("/").strip("`'\"")
        if len(tok) > 2 and tok not in ("w/o", "e.g", "i.e") and not re.fullmatch(r"[\d.]+", tok):
            out.append(tok)
    return out


def extract(text: str, turn: int | None = None) -> list[dict[str, Any]]:
    """Extract constraint rules (as dicts without ids) from a user message."""
    out: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    case = {p.lower(): p for p in _paths_in(text or "")}
    antecedent: str | None = None
    for clause in _clauses(text):
        ctx = _Ctx(antecedent=antecedent, explicit=_paths_in(clause), case=case)
        norm = _normalise(clause)
        found = next((f for rx, k in _TEMPLATES if norm and (m := rx.match(norm)) and (f := _apply(k, m, ctx))), [])
        for r in found:
            key = (r["kind"], r["action"], r.get("target"), r.get("requires"))
            if key in seen or not _validate(r):
                continue
            seen.add(key)
            r["source"] = {"turn": turn, "text": clause.strip(), "via": "pattern"}
            out.append(r)
        if ctx.explicit:
            antecedent = ctx.explicit[-1]
    # a push rule already covers force pushes (classify labels them push + force_push)
    pushes = {(r["kind"], r.get("target")) for r in out if r["action"] == "push"}
    return [r for r in out if not (r["action"] == "force_push" and (r["kind"], r.get("target")) in pushes)]


# ------------------------------------------------------------------ confirmations / revocations

_AFFIRM = _P(
    r"^(?:yes|yep|yeah|yup|ya|y|ok|okay|k|sure|go ahead|go for it|do it|approved|confirmed|i confirm|confirm|"
    r"proceed|fine|alright|affirmative|please do|yes please|that's fine|that is fine|sounds good|lgtm|absolutely|"
    r"of course|definitely|agreed|permission granted|you have my permission|granted|by all means|green light|"
    r"go on|carry on|make it so|confirming|approve|i approve|roger|aye|correct|right|exactly|please|affirm)"
    r"(?:[,.!:;\s-]+|$)")
_GRANT = _P(r"\b(?:you (?:can|may|are (?:free|allowed|welcome) to)|feel free to|it's (?:ok|okay|fine) to|"
            r"it is (?:ok|okay|fine) to|(?:you have |i give you )?permission to|i'm (?:ok|okay|fine) with you|"
            r"i am (?:ok|okay|fine) with you|i approve of you|i approve|go ahead and|i authorize you to)\s+")
_NEG_START = _P(r"^(?:no|nope|nah|not|don't|do not|never|wait|stop|hold|cancel|negative)\b")
_NEGATED = _P(r"\b(?:don't|do not|never|not yet|yet|hold off|hold on|hold up|wait|stop|first|no to|but not|not the|not this|"
              r"not that|not now|rather not|instead|leave it|skip it|cancel|nevermind|never mind|not until|not before)\b")
_NARROW = _P(r"^(?:but|only|as long as|provided|so long as)\b\s*")
_COND = _P(r"^(?:however|after|once|if|when|unless|except|although|though|before)\b")
_FOLLOWUP = _P(r"^(?:and then|and|then|after that|afterwards)\b")
_ACTION_VERB = _P(
    r"\b(forcepush|delete|remove|rm|erase|wipe|nuke|drop|purge|trash|clear|push|commit|install|deploy|run|"
    r"do it|do that|proceed|go ahead|go for it|continue|carry on|make the change|edit|modify|overwrite)\b")
_ACTION_MAP = {**dict.fromkeys("delete remove rm erase wipe nuke drop purge trash clear".split(), "delete"),
               **dict.fromkeys("edit modify overwrite".split(), "edit_path"), "forcepush": "force_push", "push": "push",
               "commit": "commit", "install": "install", "deploy": "touch_prod", "run": "run_cmd"}
_STOP_OBJ = {"it", "them", "that", "this", "those", "these", "everything", "all", "anything", "stuff", "files",
             "now", "then", "please", "away", "all of it", "the files", "and", "the", "whatever", "to"}


def _conf_target(rest: str) -> str | None:
    rest = rest.strip().strip(" .,!;:")
    if not rest:
        return None
    m = re.match(r"^(?:the |that |this |those |these |my |our )?([\w.\-/*~]+)\s+(?:dir|directory|folder|files?|tree)\b", rest)
    if m:
        return norm_target(m.group(1))
    words = [w.strip(" .,!;:") for w in rest.split()]
    if words[0] in _STOP_OBJ:
        return None
    if words[0] in ("the", "that", "this", "those", "these", "my", "our") and len(words) > 1:
        return words[1].rstrip("/") if _PATHLIKE.match(words[1]) and set(words[1]) & set("/.*") else None
    return norm_target(words[0]) if _PATHLIKE.match(words[0]) and not words[0].isdigit() else None


def extract_confirmations(text: str) -> list[Confirmation]:
    """Detect "yes / go ahead / you can delete X" style confirmations."""
    raw = (text or "").strip()
    if not raw or raw.endswith("?"):
        return []
    norm = re.sub(r"\bforce[- ]?push(?:es|ing)?\b|\b(?:git )?push (?:-f|--force(?:-with-lease)?)\b", "forcepush", raw.lower())
    norm = re.sub(r"\s+", " ", norm).strip(" .!")
    if _NEG_START.match(norm) and not re.match(r"^no(?:pe)?,?\s+(?:go ahead|do it|proceed|yes)", norm):
        return []
    rest: str | None = None
    m = _AFFIRM.match(norm)
    while m and m.end():  # chained affirmatives: "ok sure, go ahead"
        rest = (norm if rest is None else rest)[m.end():].strip()
        m = _AFFIRM.match(rest)
    g = _GRANT.search(norm)
    if g:
        grant_rest = norm[g.end():].strip()
        if rest is None or len(grant_rest) < len(rest):
            rest = grant_rest
    if rest is None or _NEGATED.search(rest) or _COND.match(rest):
        return []
    rest = rest.strip(" ,.!;:")
    if not rest:
        return [Confirmation("any", None, raw)]
    narrowed = bool(_NARROW.match(rest))
    rest = _NARROW.sub("", rest)
    if _FOLLOWUP.match(rest) and not g:
        return [Confirmation("any", None, raw)]
    for vm in _ACTION_VERB.finditer(rest):
        if vm.group(1) in _ACTION_MAP:
            tail = re.sub(r"^\s+(?:and|then)\s+", " ", rest[vm.end():])
            return [Confirmation(_ACTION_MAP[vm.group(1)], _conf_target(tail), raw)]
    if narrowed or _ACTION_VERB.search(rest) or len(rest.split()) <= 3:
        return [Confirmation("any", None, raw)]
    return []


_REVOKE = _P(
    r"^(?:actually,? |ok,? |okay,? |oh,? |hmm,? |wait,? |no,? |sorry,? )*(?:never ?mind|nevermind|cancel(?: that| it)?|scratch that|"
    r"forget (?:it|that|about it)|hold off|hold on|hold up|stop|wait|abort|undo that|i changed my mind|i've changed my mind|"
    r"on second thought|don't(?: do (?:it|that|this))?$|don't (?:delete|remove|rm|erase|wipe|destroy|trash|nuke|discard|drop|"
    r"purge|push|commit|install|run|touch) (?:it|that|this|them)\b|"
    r"no,? don't|no,? stop|not (?:yet|now)|let's not|disregard (?:that|it)|belay that|revoke)")


def is_revocation(text: str) -> bool:
    norm = re.sub(r"\s+", " ", (text or "").lower().strip()).strip(" .!,")
    return bool(norm) and bool(_REVOKE.match(norm))


def extract_all(text: str, turn: int | None = None) -> Extraction:
    """Rules + confirmations + revocation for one user message."""
    res = Extraction(rules=extract(text, turn))
    res.revoked = is_revocation(text)
    if not res.revoked:
        res.confirmations = extract_confirmations(text)
        if not res.confirmations:
            for clause in _clauses(text):
                res.confirmations = extract_confirmations(clause)
                if res.confirmations:
                    break
    return res
