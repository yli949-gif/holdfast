"""Pattern-based natural-language -> rule extraction (SPEC §4 UserPromptSubmit, A4).

Design
------
No ML in the hot path.  A user message is split into clauses, each clause is
*normalised* (lower-cased, fillers removed, "you must not" -> "don't",
"git push --force" -> "forcepush", ...) and then run through an ordered list
of anchored regexes.  Anchoring at the clause start is what keeps the
false-positive rate low: "don't delete anything" is a constraint, "I don't
think the tests cover this" is not, and "delete the TODO comment" is a task.

Three things are recognised:

* constraints  -> rule dicts (``kind``/``action``/``target``/``requires``)
* confirmations -> :class:`Confirmation` ("yes, delete it", "go ahead")
* revocations   -> ``is_revocation`` ("never mind", "cancel that")

Objects after a verb decide the target: path-like tokens (``src/``,
``config/settings.py``) and a few well-known directory nouns ("the tests")
become globs; pronouns ("don't delete *it*") mean a one-off refusal, not a
rule, and yield nothing.
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


# ------------------------------------------------------------------ normalisation

_SUBS: list[tuple[re.Pattern[str], str]] = [(re.compile(p), r) for p, r in [
    (r"\bdo not\b", "don't"), (r"\bdo n't\b", "don't"),
    (r"\b(?:you|we) (?:must|should|may|can) ?(?:not|n't|never)\b", "don't"),
    (r"\byou (?:mustn't|shouldn't|can't|cannot|aren't to|are not to)\b", "don't"),
    (r"\byou(?:'re| are)(?: absolutely)? not (?:allowed|permitted|supposed) to\b", "don't"),
    (r"\b(?:i|we) (?:don't|do not|wouldn't) (?:want|like) you to\b", "don't"),
    (r"\bi'd rather you (?:didn't|did not|don't)\b", "don't"),
    (r"\bit(?:'s| is) not (?:ok|okay|fine|acceptable) to\b", "don't"),
    (r"\bunder no circumstances(?: should you| are you to)?\b", "never"),
    (r"\b(?:make sure|be careful|take care|remember) (?:you don't|not to|to never)\b", "don't"),
    (r"\b(?:don't|never) ever\b", "never"), (r"\bnever ever\b", "never"),
    (r"\bnever,? under any circumstances,?\b", "never"),
    (r"\bgit push (?:-f|--force(?:-with-lease)?)\b", "forcepush"),
    (r"\bpush (?:-f|--force(?:-with-lease)?)\b", "forcepush"),
    (r"\bforce[- ]?push(?:es|ing)?\b", "forcepush"), (r"\b--force\b", "forcepush"),
    (r"\bplease\b", ""), (r"\s+", " "),
]]
_FILLER = re.compile(
    r"^(?:(?:please|pls|also|and|but|oh|btw|so|just|hey|then|plus|additionally|importantly|remember|"
    r"important|note|rule|rules|constraint|constraints|one more thing|fyi|again|this time|from now on|"
    r"going forward|in general|for this task)[,:!.\s-]+|(?:yes|ok|okay|sure|wait|actually|hmm|well|no|right)[,:!]+\s*)+")
_CLAUSE_SPLIT = re.compile(
    r"\.(?=\s|$)|[;!?\n]+|,\s*(?=(?:and |but |also |then )?(?:don't|do not|never|no |only |ask |confirm |always |"
    r"make sure|run |keep |stay |leave |you ))")


def _clauses(text: str) -> list[str]:
    out = []
    for raw in _CLAUSE_SPLIT.split(text or ""):
        raw = raw.strip()
        if raw:
            out.append(raw)
    return out


def _normalise(clause: str) -> str:
    c = clause.lower().strip()
    for rx, rep in _SUBS:
        c = rx.sub(rep, c)
    c = c.strip(" ,;:!.-")
    prev = None
    while prev != c:
        prev = c
        c = _FILLER.sub("", c).strip(" ,;:!.-")
    return c.strip()


# ------------------------------------------------------------------ targets

NOUN_TARGETS = {
    "tests": "tests", "test": "tests", "test files": "tests", "test suite": "tests", "unit tests": "tests",
    "the tests": "tests", "testing": "tests", "spec": "spec", "specs": "spec",
    "docs": "docs", "documentation": "docs", "migrations": "migrations", "migration files": "migrations",
    "config": "config", "configs": "config", "configuration": "config", "fixtures": "fixtures",
    "snapshots": "snapshots", "vendor": "vendor", "node_modules": "node_modules", "lockfile": "*lock*",
    "lock file": "*lock*", "lockfiles": "*lock*", "lock files": "*lock*", "main": "main", "master": "master",
    "src": "src", "lib": "lib", "scripts": "scripts", "build": "build", "dist": "dist",
}
COMMON_DIRS = {"src", "lib", "tests", "test", "docs", "build", "dist", "config", "scripts", "migrations",
               "fixtures", "vendor", "app", "api", "core", "utils", "examples", "assets", "public", "static",
               "templates", "data", "tools", "bin", "pkg", "cmd", "internal", "spec", "out", "target", "cache"}
PRONOUNS = {"it", "that", "this", "them", "those", "these", "him", "her"}
GENERIC = {"", "anything", "any files", "any file", "files", "stuff", "things", "everything", "code",
           "any code", "anything at all", "any of the files", "something", "any", "all", "all files",
           "whatever", "anything else", "data", "any data", "my files", "my stuff", "the files"}
_PATHLIKE = re.compile(r"^[\w.\-/*@~]+$")
_TRAIL_WORDS = re.compile(r"\s+(?:directory|dir|folder|files?|tree|code|branch|package|module|subtree)$")
_LEAD_WORDS = re.compile(r"^(?:the|any|all|of|my|our|your|this|that|those|these|entire|whole|existing)\s+")


def norm_target(obj: str | None) -> str | None:
    """Turn a noun phrase into a glob-ish target, or None when not path-like."""
    if obj is None:
        return None
    t = obj.strip().strip("`'\"").strip(" .,;:!?)(").lower()
    t = _LEAD_WORDS.sub("", t)
    t = _TRAIL_WORDS.sub("", t)
    t = _LEAD_WORDS.sub("", t)
    t = t.strip("`'\"").rstrip("/")
    if t.startswith("./"):
        t = t[2:]
    if t in NOUN_TARGETS:
        return NOUN_TARGETS[t]
    if t in ("prod", "production", "the prod"):
        return "prod*"
    if _PATHLIKE.match(t) and ("/" in t or "." in t or "*" in t or t in COMMON_DIRS):
        return t
    return None


def _parse_object(obj: str | None) -> tuple[str, str | None]:
    """Classify the object of a verb: ('none'|'pronoun'|'target'|'outside'|'unknown', target)."""
    if obj is None:
        return "none", None
    o = obj.strip().strip(" .,;:!?")
    if o in GENERIC:
        return "none", None
    if o in PRONOUNS or o.split()[0] in PRONOUNS and len(o.split()) <= 2:
        return "pronoun", None
    m = re.match(r"^(?:(?:anything|any files?|files|stuff|things|everything|code|any code|something|any of the "
                 r"files|any)\s+)?(?:in|under|inside|within|from|of|at|below)\s+(.+)$", o)
    if m:
        t = norm_target(m.group(1))
        return ("target", t) if t else ("unknown", None)
    m = re.match(r"^(?:anything |files |stuff |code )?outside (?:of )?(.+)$", o)
    if m:
        t = norm_target(m.group(1))
        return ("outside", t) if t else ("unknown", None)
    t = norm_target(o)
    return ("target", t) if t else ("unknown", None)


# ------------------------------------------------------------------ verbs

DEL_V = r"(?:delete|remove|rm|erase|wipe|destroy|trash|nuke|discard|drop|purge|clear out|blow away|unlink)"
DEL_ING = r"(?:deleting|removing|erasing|wiping|destroying|trashing|nuking|discarding|dropping|purging)"
EDIT_V = r"(?:touch|modify|edit|change|alter|update|rewrite|overwrite|rename|move|mess with|refactor|write to)"
EDIT_ING = r"(?:touching|modifying|editing|changing|altering|updating|rewriting|overwriting|renaming|moving|messing with)"
_VERB = re.compile(DEL_V + "|" + DEL_ING + "|" + EDIT_V + "|" + EDIT_ING + "|forcepush|push(?:ing)?|commit(?:ting)?|"
                   "install(?:ing)?|run(?:ning)?|execut(?:e|ing)|us(?:e|ing)|deploy(?:ing)?|"
                   "(?:doing |making |performing )?(?:anything|any (?:changes|operations|actions|commands)) "
                   "(?:destructive|irreversible|dangerous|risky)|(?:making )?(?:any )?(?:changes|edits|modifications)")
_DEL_RX = re.compile("^(?:" + DEL_V + "|" + DEL_ING + ")$")
_EDIT_RX = re.compile("^(?:" + EDIT_V + "|" + EDIT_ING + ")$")


def _verb_action(v: str) -> str | None:
    v = v.strip()
    if _DEL_RX.match(v):
        return "delete"
    if _EDIT_RX.match(v):
        return "edit_path"
    if v == "forcepush":
        return "force_push"
    if v.startswith("push"):
        return "push"
    if v.startswith("commit"):
        return "commit"
    if v.startswith("install"):
        return "install"
    if v.startswith("deploy"):
        return "touch_prod"
    if v.startswith(("run", "execut", "us")):
        return "run_cmd"
    if "destructive" in v or "irreversible" in v or "dangerous" in v or "risky" in v:
        return "destructive"
    if "change" in v or "edit" in v or "modif" in v:
        return "edit_path"
    return None


def _actions_from_phrase(phrase: str) -> list[tuple[str, str | None]]:
    """'deleting anything in build/' -> [('delete', 'build')]; handles 'X or Y'."""
    phrase = phrase.strip()
    out: list[tuple[str, str | None]] = []
    parts = re.split(r"\s*(?:,|\bor\b|\band\b|/)\s*", phrase)
    verbs: list[str] = []
    rest = ""
    for i, p in enumerate(parts):
        m = _VERB.match(p.strip())
        if m and (m.end() == len(p.strip()) or i == len(parts) - 1 or not rest):
            verbs.append(m.group(0))
            tail = p.strip()[m.end():].strip()
            if tail:
                rest = tail
        else:
            rest = (rest + " " + p).strip() if rest else p
    if not verbs:
        return []
    for v in verbs:
        act = _verb_action(v)
        if act is None:
            continue
        if act == "destructive":
            out += [("delete", None), ("force_push", None)]
            continue
        if act == "run_cmd":
            tgt = _cmd_target(rest)
            if tgt:
                out.append(("run_cmd", tgt))
            continue
        kind, tgt = _parse_object(rest or None)
        if kind == "pronoun":
            continue
        if kind == "unknown" and act in ("delete", "edit_path"):
            continue
        if act == "push" and tgt in ("remote", "github", "origin", "upstream", "gitlab"):
            tgt = None
        out.append((act, tgt if kind in ("target", "outside") else None))
    return out


_CMD_STOP = {"it", "that", "this", "them", "anything", "any", "tests", "test", "the", "a", "an", "commands",
             "scripts", "something", "code", "things", "stuff", "suite", "build", "everything", "again"}


def _cmd_target(obj: str | None) -> str | None:
    if not obj:
        return None
    o = obj.strip().strip("`'\" .,;:!?")
    o = re.sub(r"^(?:the|any|a|an)\s+", "", o)
    o = re.sub(r"\s+(?:command|cli|tool|binary|commands|utility)$", "", o)
    words = o.split()
    if not words or len(words) > 3 or words[0] in _CMD_STOP or any(w in ("tests", "test") for w in words):
        return None
    if not all(re.match(r"^[\w.\-/+=:@]+$", w) for w in words):
        return None
    return " ".join(words)


# ------------------------------------------------------------------ constraint patterns

NEG = r"(?:don't|never|no)"
TESTS_PASS = r"(?:the |all |your |my |our )?(?:unit |integration )?(?:tests?|test suite|ci|test cases)\s*(?:pass(?:es)?|are passing|is passing|succeed|are green|is green|go green|have passed|has passed|passed|are ok|are okay|pass(?:es)? successfully)"
_VERB_LIST = r"((?:%s|%s)(?:(?:,? or |,? and |, |/)(?:%s|%s))*)" % (DEL_V, EDIT_V, DEL_V, EDIT_V)

_P = re.compile


def _rules(*specs: tuple[str, str, str | None, str | None]) -> list[dict[str, Any]]:
    return [{"kind": k, "action": a, "target": t, "requires": r} for k, a, t, r in specs]


def _confirm_from(phrase: str) -> list[dict[str, Any]]:
    return _rules(*[("confirm", a, t, None) for a, t in _actions_from_phrase(phrase)])


def _forbid_from(phrase: str) -> list[dict[str, Any]]:
    return _rules(*[("forbid", a, t, None) for a, t in _actions_from_phrase(phrase)])


def _order_from(verbs: str) -> list[dict[str, Any]]:
    out = []
    for v in re.split(r"\s*(?:,|\bor\b|\band\b|/)\s*", verbs):
        a = _verb_action(v.strip())
        if a in ("push", "commit", "force_push"):
            out += _rules(("order", a, None, "tests_passed"))
    return out


_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    # force push
    (_P(rf"^{NEG} (?:(?:ever|use|do|attempt|try to|git|run|do a|do any|any|a) )*forcepush(?:es|ing)?\b"),
     lambda m: _rules(("forbid", "force_push", None, None))),
    # destructive
    (_P(rf"^{NEG} (?:do |make |perform |run |attempt |try )?(?:anything|any (?:changes|operations|actions|commands|ops))"
        r" (?:destructive|irreversible|dangerous|risky|that can'?t be undone)"),
     lambda m: _rules(("forbid", "delete", None, None), ("forbid", "force_push", None, None))),
    (_P(r"^no destructive (?:actions|operations|changes|commands|ops)"),
     lambda m: _rules(("forbid", "delete", None, None), ("forbid", "force_push", None, None))),
    # confirm
    (_P(r"^(?:ask|check|confirm|verify|clear it|check in)(?: with)?(?: me)?(?: first| explicitly)?(?: with me)? before "
        r"(?:you )?(?:doing |making |running |attempting )?(.+)$"), lambda m: _confirm_from(m.group(1))),
    (_P(r"^(?:get|ask for|obtain|request) (?:my |explicit |an? )*(?:permission|approval|ok|okay|confirmation|sign-?off|go-ahead|consent)"
        r" before (?:you )?(?:doing |making |running )?(.+)$"), lambda m: _confirm_from(m.group(1))),
    (_P(r"^(?:don't|never) (.+?) (?:without|unless you|until you|before you) (?:first )?(?:(?:asking|checking|confirming|"
        r"clearing it|getting|having|obtaining|get|have|ask|check|confirm)(?: with)?(?: me| for)?(?: first| explicitly)?"
        r"(?: my| an| the| explicit| for my)*(?: permission| approval| ok| okay| consent| confirmation| go-ahead| sign-?off)?"
        r"|(?:my |an |the |explicit |prior )*(?:permission|approval|ok|okay|consent|confirmation|go-ahead|sign-?off))"),
     lambda m: _confirm_from(m.group(1))),
    (_P(r"^no (deletions?|deleting|removals?|pushes|pushing|commits?|committing|installs?|installing|changes|edits)"
        r" without (?:asking|approval|permission|confirmation|my \w+|checking)"),
     lambda m: _confirm_from({"deletion": "delete", "deletions": "delete", "deleting": "delete", "removal": "delete",
                              "removals": "delete", "pushes": "push", "pushing": "push", "commit": "commit",
                              "commits": "commit", "committing": "commit", "installs": "install", "install": "install",
                              "installing": "install", "changes": "edit", "edits": "edit"}[m.group(1)])),
    # order
    (_P(rf"^only (push|commit|push or commit|commit or push)(?: (?:anything|changes|code|commits))? (?:after|once|when|if) {TESTS_PASS}"),
     lambda m: _order_from(m.group(1))),
    (_P(r"^(?:always |first |please )*run (?:the |all |your )?(?:unit |full )?(?:tests?|test suite)(?: first| successfully)? before (?:you )?"
        r"((?:pushing|push|committing|commit)(?:(?: or | and |, )(?:pushing|push|committing|commit))*)"),
     lambda m: _order_from(m.group(1))),
    (_P(rf"^(?:make sure|ensure|verify|check|confirm|be sure) (?:that )?{TESTS_PASS} before (?:you )?"
        r"((?:pushing|push|committing|commit)(?:(?: or | and |, )(?:pushing|push|committing|commit))*)"),
     lambda m: _order_from(m.group(1))),
    (_P(rf"^(?:don't|never|no) ((?:pushing|pushes|push|committing|commits|commit)(?:(?: or | and |, )(?:pushing|push|committing|commit))*)"
        rf"(?: anything| changes| code| commits)? (?:until|unless|before|if|when) {TESTS_PASS}"),
     lambda m: _order_from(m.group(1))),
    (_P(r"^(?:the |all )?tests (?:must|have to|need to|should|had better) (?:pass|be passing|be green|succeed) before (?:you )?"
        r"((?:pushing|push|committing|commit)(?:(?: or | and |, )(?:pushing|push|committing|commit))*)"),
     lambda m: _order_from(m.group(1))),
    (_P(r"^(?:don't|never) ((?:push|commit)(?: or (?:push|commit))?) (?:without|before) (?:running|passing|green) (?:the )?tests"),
     lambda m: _order_from(m.group(1))),
    # push (before prod so "don't push to prod" is a push rule)
    (_P(rf"^{NEG} (?:ever )?(?:git )?(?:push|pushing|pushes)(?: anything| changes| code| commits| any commits| this)?"
        r"(?: (?:to|into|onto|at|upstream to) (?:the )?(.+?)(?: branch)?)?$"),
     lambda m: _rules(("forbid", "push", None if norm_target(m.group(1)) in (None, "remote", "github", "origin", "upstream")
                       else norm_target(m.group(1)), None)) if (m.group(1) is None or norm_target(m.group(1)) is not None
                                                                  or m.group(1) in ("remote", "github", "origin", "upstream", "the remote")) else []),
    # prod
    (_P(r"^(?:don't|never|no|avoid) (?:(?:ever |touch|modify|edit|change|deploy to|deploy on|deploying to|ship to|release to|"
        r"mess with|alter|affect|hit|use|go near|make changes to|make changes in|changes to|changes in|touching|modifying|"
        r"changing|run (?:anything )?(?:against|on|in)|connect to|access|log into|ssh into) )*(?:the |our |my )?"
        r"(?:prod|production|prd|live)(?: environment| env| servers?| db| database| cluster| systems?| infra| data| site| deployment)?\b"),
     lambda m: _rules(("forbid", "touch_prod", None, None))),
    (_P(r"^(?:stay|keep) (?:away from|out of|off|clear of) (?:the |our )?(?:prod|production)"),
     lambda m: _rules(("forbid", "touch_prod", None, None))),
    (_P(r"^(?:the |our )?(?:prod|production)(?: environment| env| servers?| db| database)? (?:is|are) (?:off[- ]limits|out of bounds|untouchable|read[- ]only)"),
     lambda m: _rules(("forbid", "touch_prod", None, None))),
    (_P(r"^leave (?:the |our )?(?:prod|production)(?: environment| env| servers?| db| database)? alone"),
     lambda m: _rules(("forbid", "touch_prod", None, None))),
    # network
    (_P(r"^(?:don't|never) (?:(?:make|do|perform|issue|send|use|access|hit|call|touch|allow|attempt|try|go on|reach|talk to) )+"
        r"(?:any |the |an |to the |to )?(?:network|internet|outbound|external|remote|http|https|web|api|online)"
        r"(?: calls?| requests?| access| connections?| traffic| i/o| services?| apis?| endpoints?| resources?)?\b"),
     lambda m: _rules(("forbid", "network", None, None))),
    (_P(r"^no (?:network|internet|outbound|external|web)(?: (?:calls?|requests?|access|connections?|traffic|i/o|activity))?\b"),
     lambda m: _rules(("forbid", "network", None, None))),
    (_P(r"^(?:stay|work|remain|keep it|operate|run|do everything) (?:fully |completely |entirely )?offline\b|^offline only\b"),
     lambda m: _rules(("forbid", "network", None, None))),
    (_P(r"^(?:don't|never) (?:download|fetch|curl|wget|upload|call out to|phone home)(?: anything| stuff| files| any files| from the internet| from the web)?$"),
     lambda m: _rules(("forbid", "network", None, None))),
    # install
    (_P(r"^(?:don't|never) (?:install|pip install|npm install)(?: any| new| additional| extra| more| third[- ]party| external| other)*"
        r"(?: packages?| dependencies| deps| libraries| libs| modules| anything| software| tools| stuff| things| gems| crates)?$"),
     lambda m: _rules(("forbid", "install", None, None))),
    (_P(r"^(?:don't|never) (?:add|pull in|bring in|introduce|vendor)(?: any| new| additional| extra| more| third[- ]party| external)*"
        r" (?:packages?|dependencies|deps|libraries|libs|modules)$"),
     lambda m: _rules(("forbid", "install", None, None))),
    (_P(r"^no (?:new |additional |extra |third[- ]party |external )*(?:packages|dependencies|deps|libraries|libs|installs|installing|package installs)\b"),
     lambda m: _rules(("forbid", "install", None, None))),
    # commit
    (_P(r"^(?:don't|never) (?:ever )?(?:commit|git commit|make (?:any |a )?commits?|create (?:any |a )?commits?)"
        r"(?: anything| changes| code| this| yet| for me| on my behalf)?$"),
     lambda m: _rules(("forbid", "commit", None, None))),
    (_P(r"^no (?:commits?|committing)(?: yet| for now)?$"), lambda m: _rules(("forbid", "commit", None, None))),
    # scope
    (_P(r"^only (?:ever )?(?:edit|modify|touch|change|alter|write to|work on|work in|work under|make (?:your )?changes (?:in|to|under|inside|within))"
        r" (?:files |stuff |things |code |the files |the code |source )?(?:in |under |inside |within |to |on |of )?(.+)$"),
     lambda m: _rules(("scope", "edit_path", norm_target(m.group(1)), None)) if norm_target(m.group(1)) else []),
    (_P(r"^(?:keep|restrict|limit|confine|scope) (?:all |your |the |any )?(?:changes|edits|modifications|work|code changes)"
        r" (?:to |in |under |inside |within |limited to |confined to )(?:the )?(.+)$"),
     lambda m: _rules(("scope", "edit_path", norm_target(m.group(1)), None)) if norm_target(m.group(1)) else []),
    (_P(r"^(?:all |your |any )?(?:changes|edits|modifications) (?:should|must|need to|have to|are to) (?:be|stay|go|remain|live|be made|be kept)"
        r" (?:in|under|inside|within|limited to|confined to|only in|only under) (?:the )?(.+)$"),
     lambda m: _rules(("scope", "edit_path", norm_target(m.group(1)), None)) if norm_target(m.group(1)) else []),
    (_P(r"^stick to (?:editing |modifying |changing )?(?:files in |files under |the )?(.+)$"),
     lambda m: _rules(("scope", "edit_path", norm_target(m.group(1)), None)) if norm_target(m.group(1)) else []),
    (_P(r"^(?:don't|never) (?:edit|touch|modify|change|write to|alter|create) (?:anything |files |stuff |code |any files |any file )?outside (?:of )?(?:the )?(.+)$"),
     lambda m: _rules(("scope", "edit_path", norm_target(m.group(1)), None)) if norm_target(m.group(1)) else []),
    # leave alone / off limits
    (_P(r"^(?:leave|keep) (.+?) (?:alone|as is|as-is|untouched|unchanged|the way it is)$|^hands off (.+)$|"
        r"^(?:stay|keep) (?:away from|out of|off) (.+)$|^(.+?) (?:is|are) (?:off[- ]limits|out of bounds|untouchable|read[- ]only|frozen|not to be (?:touched|modified|changed|edited))$"),
     lambda m: _rules(("forbid", "edit_path", norm_target(next(g for g in m.groups() if g)), None))
     if norm_target(next(g for g in m.groups() if g)) else []),
    # forbid delete/edit verbs
    (_P(rf"^(?:don't|never) (?:ever )?{_VERB_LIST}(?: (.+))?$"), None),  # handled specially below
    (_P(rf"^no (?:{DEL_ING}|deletions?|deletes|removals?)(?: (?:of|in|under) (.+))?$"),
     lambda m: _rules(("forbid", "delete", norm_target(m.group(1)) if m.group(1) else None, None))
     if not m.group(1) or norm_target(m.group(1)) else []),
    # run_cmd
    (_P(r"^(?:don't|never) (?:ever )?(?:run|use|execute|invoke|call|shell out to) (.+)$"),
     lambda m: _rules(("forbid", "run_cmd", _cmd_target(m.group(1)), None)) if _cmd_target(m.group(1)) else []),
]


def _forbid_verbs(m: re.Match[str]) -> list[dict[str, Any]]:
    verbs = re.split(r"\s*(?:,|\bor\b|\band\b|/)\s*", m.group(1))
    obj = m.group(2)
    kind, tgt = _parse_object(obj)
    out: list[dict[str, Any]] = []
    for v in verbs:
        act = _verb_action(v)
        if act not in ("delete", "edit_path"):
            continue
        if kind == "pronoun" or kind == "unknown":
            continue
        if kind == "outside":
            if act == "edit_path":
                out += _rules(("scope", "edit_path", tgt, None))
            continue
        out += _rules(("forbid", act, tgt, None))
    return out


def _validate(rule: dict[str, Any]) -> bool:
    try:
        Rule(id="tmp", kind=rule["kind"], action=rule["action"], target=rule.get("target"),
             requires=rule.get("requires"))
        return True
    except RuleError:
        return False


def extract(text: str, turn: int | None = None) -> list[dict[str, Any]]:
    """Extract constraint rules (as dicts without ids) from a user message."""
    out: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for clause in _clauses(text):
        norm = _normalise(clause)
        if not norm:
            continue
        found: list[dict[str, Any]] = []
        for rx, fn in _PATTERNS:
            m = rx.match(norm)
            if not m:
                continue
            found = _forbid_verbs(m) if fn is None else fn(m)
            if found:
                break
        for r in found:
            key = (r["kind"], r["action"], r.get("target"), r.get("requires"))
            if key in seen or not _validate(r):
                continue
            seen.add(key)
            r["source"] = {"turn": turn, "text": clause.strip(), "via": "pattern"}
            out.append(r)
    return out


# ------------------------------------------------------------------ confirmations / revocations

_AFFIRM = re.compile(
    r"^(?:yes|yep|yeah|yup|ya|y|ok|okay|k|sure|go ahead|go for it|do it|approved|confirmed|i confirm|confirm|"
    r"proceed|fine|alright|affirmative|please do|yes please|that's fine|that is fine|sounds good|lgtm|absolutely|"
    r"of course|definitely|agreed|permission granted|you have my permission|granted|by all means|green light|"
    r"go on|carry on|make it so|confirming|approve|i approve|roger|aye|correct|right|exactly|please|affirm)"
    r"(?:[,.!:;\s-]+|$)")
_GRANT = re.compile(r"\b(?:you (?:can|may|are (?:free|allowed|welcome) to)|feel free to|it's (?:ok|okay|fine) to|"
                    r"it is (?:ok|okay|fine) to|(?:you have |i give you )?permission to|i'm (?:ok|okay|fine) with you|"
                    r"i am (?:ok|okay|fine) with you|i approve of you|i approve|go ahead and|i authorize you to)\s+")
_NEG_START = re.compile(r"^(?:no|nope|nah|not|don't|do not|never|wait|stop|hold|cancel|negative)\b")
_COND = re.compile(r"^(?:but|however|first|after|once|if|when|as long as|unless|only|provided|except|although|though)\b")
_ACTION_VERB = re.compile(
    r"\b(forcepush|delete|remove|rm|erase|wipe|nuke|drop|purge|trash|clear|push|commit|install|deploy|run|"
    r"do it|do that|proceed|go ahead|go for it|continue|carry on|make the change|edit|modify|overwrite)\b")
_ACTION_MAP = {"forcepush": "force_push", "delete": "delete", "remove": "delete", "rm": "delete", "erase": "delete",
               "wipe": "delete", "nuke": "delete", "drop": "delete", "purge": "delete", "trash": "delete",
               "clear": "delete", "push": "push", "commit": "commit", "install": "install", "deploy": "touch_prod",
               "run": "run_cmd", "edit": "edit_path", "modify": "edit_path", "overwrite": "edit_path"}
_STOP_OBJ = {"it", "them", "that", "this", "those", "these", "everything", "all", "anything", "stuff", "files",
             "now", "then", "please", "away", "now.", "all of it", "the files", "and", "the", "whatever"}


def _conf_target(rest: str, action: str) -> str | None:
    rest = rest.strip().strip(" .,!;:")
    if not rest:
        return None
    m = re.match(r"^(?:the |that |this |those |these |my |our )?([\w.\-/*~]+)\s+(?:dir|directory|folder|files?|tree)\b", rest)
    if m:
        return norm_target(m.group(1))
    words = rest.split()
    first = words[0]
    if first in _STOP_OBJ:
        return None
    if first in ("the", "that", "this", "those", "these", "my", "our") and len(words) > 1:
        cand = words[1].strip(" .,!;:")
        return cand.rstrip("/") if _PATHLIKE.match(cand) and ("/" in cand or "." in cand or "*" in cand) else None
    cand = first.strip(" .,!;:")
    if _PATHLIKE.match(cand) and cand not in _STOP_OBJ and not cand.isdigit():
        return norm_target(cand) or cand.rstrip("/")
    return None


def extract_confirmations(text: str) -> list[Confirmation]:
    """Detect "yes / go ahead / you can delete X" style confirmations."""
    raw = (text or "").strip()
    if not raw:
        return []
    norm = raw.lower().strip()
    for rx, rep in _SUBS[-6:-2]:  # force-push normalisation only
        norm = rx.sub(rep, norm)
    norm = re.sub(r"\s+", " ", norm).strip(" .!")
    if _NEG_START.match(norm) and not re.match(r"^no(?:pe)?,?\s+(?:go ahead|do it|proceed|yes)", norm):
        return []
    rest: str | None = None
    m = _AFFIRM.match(norm)
    if m:
        rest = norm[m.end():].strip()
        while True:  # chained affirmatives: "ok sure, go ahead"
            m2 = _AFFIRM.match(rest)
            if not m2 or m2.end() == 0:
                break
            rest = rest[m2.end():].strip()
    g = _GRANT.search(norm)
    if g and (rest is None or g.start() >= 0):
        grant_rest = norm[g.end():].strip()
        if rest is None or len(grant_rest) < len(rest):
            rest = grant_rest
    if rest is None:
        return []
    if _COND.match(rest):
        return []
    rest = rest.strip(" ,.!;:")
    if not rest:
        return [Confirmation("any", None, raw)]
    v = _ACTION_VERB.search(rest)
    concrete = None
    for vm in _ACTION_VERB.finditer(rest):
        if vm.group(1) in _ACTION_MAP:
            concrete = vm
            break
    if concrete is not None:
        action = _ACTION_MAP[concrete.group(1)]
        tail = rest[concrete.end():]
        tail = re.sub(r"^\s+(?:and|then)\s+", " ", tail)
        return [Confirmation(action, _conf_target(tail, action), raw)]
    if v is not None or len(rest.split()) <= 3:
        return [Confirmation("any", None, raw)]
    return []


_REVOKE = re.compile(
    r"^(?:actually,? |ok,? |okay,? |oh,? |hmm,? |wait,? |no,? |sorry,? )*(?:never ?mind|nevermind|cancel(?: that| it)?|scratch that|"
    r"forget (?:it|that|about it)|hold off|hold on|hold up|stop|wait|abort|undo that|i changed my mind|i've changed my mind|"
    r"on second thought|don't(?: do (?:it|that|this))?$|don't (?:" + DEL_V + r"|push|commit|install|run|touch) (?:it|that|this|them)\b|"
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
