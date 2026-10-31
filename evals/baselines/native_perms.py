"""Model of Claude Code's native permission rules (`permissions.deny` / `permissions.ask` in settings.json).

Semantics modelled from https://code.claude.com/docs/en/permissions (fetched 2026-10-31), sections
"Manage permissions", "Wildcard patterns", "Compound commands", "Wrappers", "What a Bash rule doesn't
match", "Redirections", "Read and Edit", "How path patterns match", "Symlinks":

* Evaluation order deny -> ask -> allow; first match wins. Anything that matches no deny/ask rule is
  treated as ALLOWED here (i.e. we model a session where the user has otherwise auto-approved Bash/edits,
  e.g. `"allow": ["Bash", "Edit"]`, acceptEdits/auto/bypass mode). In Manual mode almost every
  non-read-only call prompts anyway, which is a different (ask-everything) baseline.
* Bash rules match the *command text Claude writes*, per subcommand. Separators `&&`, `||`, `;`, `|`,
  `|&`, `&`, newline split a compound command; deny/ask rules also apply to commands nested in a
  subshell, a command substitution, or a control-flow body.
* `*` matches any text (including spaces); `X *` / `X:*` also matches bare `X`; no `*` = exact match.
* Before matching, the wrappers `timeout`, `time`, `nice`, `nohup`, `stdbuf`, `command`, `builtin`,
  `noglob` and bare `xargs` (no flags) are stripped; deny/ask rules match past ANY leading
  `VAR=value` assignment. Not stripped (documented): `env`, `sudo`, `/bin/rm`, `bash -c '...'`,
  `sh -c`, quoted program names (`git 'push'`), `git -C . push`.
* Output redirect targets (`>`, `>>`, `2>`, `&>`) and `tee` targets are checked against Edit rules.
  Edit/Read deny rules also apply to "file commands Claude Code recognizes in Bash, such as cat,
  head, tail, sed, and tee" -> we apply Edit rules to `sed -i` files.
* Edit rules use gitignore syntax: `dir/**` (single directory segment) in a deny/ask rule matches a
  directory with that name at ANY depth; a bare filename matches at any depth; `**/x` any depth;
  others anchored at the cwd. Symlinked paths are checked both as requested and as resolved.

FAVOURABLE ASSUMPTIONS (where the docs are silent we pick the reading that makes native rules catch
more; each is listed in RESULTS.md):
  F1. Edit deny rules are also applied to the path operands of cp/mv (destination), touch, mkdir, ln,
      install, truncate, rm/rmdir/unlink/shred, dd of=, perl -i (docs only name cat/head/tail/sed/tee).
  F2. A relative redirect/file target after `cd DIR` is resolved against DIR (docs say Claude Code
      *prompts* when it cannot tell; we resolve it instead and apply the rule).
  F3. `{ ...; }` groups and function bodies `f(){ ...; }` are treated like control-flow bodies (their
      inner commands are matched).
  F4. A rule with a leading `*` is matched against the subcommand both with and without its leading
      `VAR=value` assignments.
  F5. Whitespace runs inside a subcommand are collapsed to one space before matching.
  F6. `dir/**` also matches the directory itself (so `rm -rf tests` hits `Edit(tests/**)` via F1); the docs
      state this only for `Cd` rules.
"""
from __future__ import annotations

import os
import re

WRAPPERS = {"timeout", "time", "nice", "nohup", "stdbuf", "command", "builtin", "noglob"}
KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for", "in", "!", "{", "}",
            "case", "esac", "function"}
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SEPS = ("&&", "||", "|&", ";", "|", "&", "\n")


# ------------------------------------------------------------------ shell splitting
def _skip_quote(s: str, i: int) -> int:
    """s[i] is a quote char; return index after the closing quote."""
    q = s[i]
    j = i + 1
    while j < len(s):
        if q == '"' and s[j] == "\\":
            j += 2
            continue
        if s[j] == q:
            return j + 1
        j += 1
    return len(s)


def _match_paren(s: str, i: int, open_c="(", close_c=")") -> int:
    """s[i] == open_c; return index of the matching close (quotes respected)."""
    depth = 0
    j = i
    while j < len(s):
        c = s[j]
        if c in "'\"" :
            j = _skip_quote(s, j)
            continue
        if c == "\\":
            j += 2
            continue
        if c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return len(s) - 1


def split_commands(cmd: str) -> list[str]:
    """Return every simple-command text in `cmd`, including those nested in $(...), `...`, (...),
    { ...; } and control-flow bodies. Heredoc bodies are dropped."""
    out: list[str] = []
    _split(cmd, out)
    return [c for c in out if c.strip()]


def _strip_heredocs(s: str) -> str:
    lines = s.split("\n")
    res, i = [], 0
    while i < len(lines):
        line = lines[i]
        res.append(line)
        m = re.findall(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", re.sub(r"<<<", "", line))
        i += 1
        for word in m:
            while i < len(lines) and lines[i].strip() != word:
                i += 1
            i += 1  # skip terminator
    return "\n".join(res)


def _split(s: str, out: list[str]) -> None:
    s = _strip_heredocs(s)
    cur = []
    i = 0
    at_start = True
    while i < len(s):
        c = s[i]
        if c in "'\"":
            j = _skip_quote(s, i)
            seg = s[i:j]
            # command substitutions inside double quotes still run
            if c == '"':
                for m in re.finditer(r"\$\(", seg):
                    k = _match_paren(seg, m.end() - 1)
                    _split(seg[m.end():k], out)
            cur.append(seg)
            i = j
            at_start = False
            continue
        if c == "\\":
            cur.append(s[i:i + 2])
            i += 2
            continue
        if c == "#" and (i == 0 or s[i - 1] in " \t\n;&|"):
            while i < len(s) and s[i] != "\n":
                i += 1
            continue
        if s.startswith("$(", i) and not s.startswith("$((", i):
            k = _match_paren(s, i + 1)
            _split(s[i + 2:k], out)
            cur.append("$(...)")
            i = k + 1
            at_start = False
            continue
        if c == "`":
            k = s.find("`", i + 1)
            k = len(s) if k < 0 else k
            _split(s[i + 1:k], out)
            cur.append("`...`")
            i = k + 1
            continue
        if c in "<>" and s.startswith("(", i + 1):  # process substitution <(...) >(...)
            k = _match_paren(s, i + 1)
            _split(s[i + 2:k], out)
            cur.append(c + "(...)")
            i = k + 1
            continue
        if c == "(" and at_start:
            k = _match_paren(s, i)
            _split(s[i + 1:k], out)
            i = k + 1
            continue
        if c == "(" and s.startswith("()", i) and re.fullmatch(r"\s*(?:function\s+)?[A-Za-z_][\w-]*\s*", "".join(cur)):
            # function definition name() { body }  (F3)
            cur = []
            i += 2
            at_start = True
            continue
        if c == "{" and at_start and (i + 1 < len(s) and s[i + 1] in " \t\n"):
            k = _match_paren(s, i, "{", "}")
            _split(s[i + 1:k], out)
            i = k + 1
            continue
        sep = next((x for x in SEPS if s.startswith(x, i)), None)
        if sep and not (sep == "&" and (s.startswith("&>", i) or (i > 0 and s[i - 1] in "<>"))):
            out.append("".join(cur).strip())
            cur = []
            i += len(sep)
            at_start = True
            continue
        cur.append(c)
        # a reserved word keeps us at command start (`then (rm x)`, `do { ...; }`)
        at_start = all(t in KEYWORDS for t in "".join(cur).split())
        i += 1
    out.append("".join(cur).strip())


def _tokens(cmd: str) -> list[str]:
    toks, cur, i = [], "", 0
    while i < len(cmd):
        c = cmd[i]
        if c in "'\"":
            j = _skip_quote(cmd, i)
            cur += cmd[i:j]
            i = j
            continue
        if c.isspace():
            if cur:
                toks.append(cur)
                cur = ""
            i += 1
            continue
        cur += c
        i += 1
    if cur:
        toks.append(cur)
    return toks


def _unquote(t: str) -> str:
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
        return t[1:-1]
    return t


def normalize_subcommand(sub: str) -> tuple[str, str]:
    """Strip control keywords, wrappers and leading assignments. Returns (stripped, with_assignments)."""
    toks = _tokens(sub)
    while toks and toks[0] in KEYWORDS:
        toks = toks[1:]
    with_assign = list(toks)
    changed = True
    while toks and changed:
        changed = False
        while toks and ASSIGN.match(toks[0]):
            toks = toks[1:]
            changed = True
        if toks and toks[0] in WRAPPERS and not (toks[0] == "command" and len(toks) > 1 and toks[1] in ("-v", "-V")):
            w = toks[0]
            toks = toks[1:]
            # drop the wrapper's own options/arguments
            if w == "timeout":
                while toks and toks[0].startswith("-"):
                    toks = toks[1:]
                if toks:
                    toks = toks[1:]  # duration
            elif w in ("nice", "stdbuf", "time", "nohup", "command", "builtin", "noglob"):
                while toks and toks[0].startswith("-"):
                    opt = toks[0]
                    toks = toks[1:]
                    if w == "nice" and opt == "-n" and toks:
                        toks = toks[1:]
            changed = True
        if toks and toks[0] == "xargs" and len(toks) > 1 and not toks[1].startswith("-"):
            toks = toks[1:]
            changed = True
    return " ".join(toks), " ".join(with_assign)


# ------------------------------------------------------------------ rule matching
def _bash_rule_regex(pat: str) -> re.Pattern:
    if pat.endswith(":*"):
        pat = pat[:-2] + " *"
    parts = pat.split("*")
    rx = ".*".join(re.escape(p) for p in parts)
    alt = ""
    if pat.endswith(" *") and pat.count("*") == 1:
        alt = "|" + re.escape(pat[:-2])
    return re.compile(r"^(?:" + rx + alt + r")$", re.S)


def bash_rule_matches(pat: str, sub: str) -> bool:
    stripped, with_assign = normalize_subcommand(sub)
    rx = _bash_rule_regex(pat)
    cands = [re.sub(r"\s+", " ", stripped).strip()]
    if pat.startswith("*"):
        cands.append(re.sub(r"\s+", " ", with_assign).strip())   # F4
    return any(rx.match(c) for c in cands if c)


def _glob_to_rx(g: str) -> str:
    out, i = "", 0
    while i < len(g):
        if g.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif g.startswith("/**", i) and i + 3 == len(g):
            out += "/.*"
            i += 3
        elif g.startswith("**", i):
            out += ".*"
            i += 2
        elif g[i] == "*":
            out += "[^/]*"
            i += 1
        elif g[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(g[i])
            i += 1
    return out


def path_rule_matches(pat: str, relpath: str) -> bool:
    """gitignore-style match of an Edit(...) deny/ask rule against a cwd-relative (or absolute) path."""
    if pat in ("**", "*"):
        return True
    if pat.startswith("//"):
        rx = "^" + _glob_to_rx(pat[1:]) + "$"
        target = relpath
    else:
        p = pat[2:] if pat.startswith("./") else pat.lstrip("/")
        if relpath.startswith("/"):
            return False  # outside the project: relative rules never match
        stripped = p.rstrip("/")
        segs = [s for s in stripped.split("/") if s]
        if "/" not in stripped:
            rx = "^(?:.*/)?" + _glob_to_rx(stripped) + "$"           # bare name: any depth
        elif len(segs) == 2 and segs[1] == "**" and not pat.startswith("/"):
            rx = "^(?:.*/)?" + _glob_to_rx(segs[0]) + "(?:/.*)?$"    # dir/** deny/ask: any depth; F6: also dir itself
        else:
            rx = "^" + _glob_to_rx(stripped) + "$"
        target = relpath
    r = re.compile(rx, re.I if False else 0)
    # a pattern that matches a directory matches everything beneath it (gitignore)
    parts = target.split("/")
    for k in range(len(parts), 0, -1):
        if r.match("/".join(parts[:k])):
            return True
    return False


# ------------------------------------------------------------------ file targets in Bash (Edit rules)
FILE_CMDS_F1 = {"cp", "mv", "touch", "mkdir", "ln", "install", "truncate", "rm", "rmdir", "unlink", "shred"}


def _rel(path: str, cwd: str, base: str) -> list[str]:
    path = _unquote(path)
    if not path or path.startswith(("/dev/", "&")) or path == "/dev/null":
        return []
    ap = path if os.path.isabs(path) else os.path.join(base, path)
    ap = os.path.normpath(ap)
    outs = []
    for p in {ap, os.path.realpath(ap)}:
        rel = os.path.relpath(p, cwd) if (p + "/").startswith(cwd.rstrip("/") + "/") else p
        outs.append(rel)
    return outs


def bash_file_targets(cmd: str, cwd: str) -> list[str]:
    """Paths an Edit rule is checked against for a Bash command (redirects, tee, sed -i, F1 commands)."""
    targets: list[str] = []
    base = cwd
    for sub in split_commands(cmd):
        stripped, _ = normalize_subcommand(sub)
        toks = _tokens(stripped)
        if not toks:
            continue
        if toks[0] == "cd" and len(toks) > 1:                              # F2
            nb = _unquote(toks[1])
            base = os.path.normpath(nb if os.path.isabs(nb) else os.path.join(base, nb))
            continue
        # redirects
        for k, t in enumerate(toks):
            m = re.match(r"^(?:\d|&)?>>?(.*)$", t)
            if m and not t.startswith(">&") and not re.match(r"^\d?>&", t):
                tgt = m.group(1) or (toks[k + 1] if k + 1 < len(toks) else "")
                targets += _rel(tgt, cwd, base)
        prog = toks[0]
        args = [a for a in toks[1:] if not a.startswith("-") and not re.match(r"^(?:\d|&)?>", a)]
        # drop redirect operands
        clean, skip = [], False
        for a in toks[1:]:
            if skip:
                skip = False
                continue
            if re.match(r"^(?:\d|&)?>>?$", a) or a in ("<", "<<<"):
                skip = True
                continue
            if re.match(r"^(?:\d|&)?>", a) or a.startswith("<"):
                continue
            clean.append(a)
        args = [a for a in clean if not a.startswith("-")]
        if prog == "tee":
            for a in args:
                targets += _rel(a, cwd, base)
        elif prog == "sed" and any(a.startswith("-i") or a.startswith("--in-place") for a in clean):
            for a in args[1:]:
                targets += _rel(a, cwd, base)
        elif prog == "perl" and any(re.match(r"^-\w*i", a) for a in clean):      # F1
            files = [a for a in args if not a.startswith(("'", '"'))]
            for a in files:
                targets += _rel(a, cwd, base)
        elif prog in ("cp", "mv", "ln", "install") and args:                      # F1
            targets += _rel(args[-1], cwd, base)
            if prog == "mv":
                for a in args[:-1]:
                    targets += _rel(a, cwd, base)
        elif prog in FILE_CMDS_F1:                                                # F1
            for a in args:
                if prog == "truncate" and re.fullmatch(r"\d+[KMG]?", a):
                    continue
                targets += _rel(a, cwd, base)
        elif prog == "dd":
            for a in clean:
                if a.startswith("of="):
                    targets += _rel(a[3:], cwd, base)
    return targets


# ------------------------------------------------------------------ decision
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def decide(tool_name: str, tool_input: dict, cwd: str, deny: list[str], ask: list[str]) -> str:
    def rule_hits(rules: list[str]) -> bool:
        for r in rules:
            m = re.match(r"^(\w+)(?:\((.*)\))?$", r, re.S)
            tool, spec = m.group(1), m.group(2)
            if tool == "Bash":
                if tool_name != "Bash":
                    continue
                if spec is None or spec == "*":
                    return True
                if any(bash_rule_matches(spec, sub) for sub in split_commands(tool_input.get("command", ""))):
                    return True
            elif tool == "Edit":
                if tool_name in EDIT_TOOLS:
                    if spec is None:
                        return True
                    fp = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
                    if any(path_rule_matches(spec, t) for t in _rel(fp, cwd, cwd)):
                        return True
                elif tool_name == "Bash":
                    tg = bash_file_targets(tool_input.get("command", ""), cwd)
                    if spec is None and tg:
                        return True
                    if spec is not None and any(path_rule_matches(spec, t) for t in tg):
                        return True
            else:
                if tool_name == tool and spec is None:
                    return True
        return False

    if rule_hits(deny):
        return "deny"
    if rule_hits(ask):
        return "ask"
    return "allow"
