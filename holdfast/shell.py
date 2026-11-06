"""Shell command → effects.

Deliberately conservative: when we cannot tell what a command touches we emit an
effect with target UNKNOWN and certain=False (usually ``exec_opaque``), and let
the monitor's ``opaque_policy`` decide. We are not a full bash interpreter, but
everything that is *statically recoverable* is recovered, because a precise
effect gives a better block message and fewer false blocks than an opaque one:

* lexing: quotes, ``$'..'`` ANSI-C strings, escapes, comments, line
  continuations, fd redirects (``2>/dev/null``), heredocs (bodies are data
  unless fed to a shell/interpreter), command substitutions;
* a small evaluator over the token stream: ``cd``/``pushd`` and ``( .. )``
  subshell cwd scoping, ``VAR=val`` / ``export`` / ``${VAR:-default}``,
  ``for x in ..`` loop variables, shell functions, ``$(echo rm)``;
* stdin tracking through pipes: ``echo 'cmd' | sh``, ``sh <<< 'cmd'``,
  ``base64 -d | sh`` (literal payload decoded), ``curl .. | sh`` (opaque);
* embedded code: python is parsed with ``ast`` (constant folding,
  ``getattr(os, 'rem'+'ove')``, ``subprocess.run([...])`` literal argv);
  node/perl/ruby/php/awk are scanned with regexes;
* the project on disk (when ``root`` exists): symlinks are resolved, and
  script files / Makefile recipes / package.json scripts are read and
  classified (the run is still reported as ``exec_opaque``).
"""
from __future__ import annotations

import ast
import base64
import binascii
import fnmatch
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import warnings
from dataclasses import dataclass, field, replace

from .effects import Effect, EVERYTHING, UNKNOWN, normalize_path, resolve_on_disk

MAX_DEPTH = 3
MAX_SCRIPT_BYTES = 256 * 1024

WRAPPERS = {"sudo", "doas", "nohup", "time", "env", "command", "builtin", "exec", "busybox",
            "nice", "ionice", "timeout", "stdbuf", "chronic", "caffeinate", "setsid", "unbuffer", "flock", "taskset", "chrt"}
# wrappers whose following positional argument(s) are values to skip (timeout 5, flock lockfile, taskset mask)
WRAPPER_SKIP_ARGS = {"timeout": 1, "flock": 1, "taskset": 1, "chrt": 1}
# wrapper option flags that consume the next word as their value (sudo -u root, nice -n 5, ...)
WRAPPER_VALUE_FLAGS = {
    "sudo": {"-u", "-g", "-h", "-p", "-r", "-t", "-U", "-C", "-D", "-T"},
    "doas": {"-u", "-C"},
    "nice": {"-n"},
    "ionice": {"-c", "-n", "-p"},
    "env": {"-u", "-C"},
    "timeout": {"-s", "-k"},
    "flock": {"-w", "-E"},
}

INTERPRETERS = {"python", "python3", "python2", "pypy", "pypy3", "node", "nodejs", "perl", "ruby", "php", "deno", "bun"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "ash", "mksh"}
TEST_RUNNERS = re.compile(r"^(pytest|py\.test|python3? -m pytest|npm (run )?test|yarn test|pnpm test|bun test|go test|cargo test|make test|jest|vitest|mocha|rspec|phpunit|tox|nox)\b")
TEST_TOOLS = {"pytest", "py.test", "jest", "vitest", "mocha", "rspec", "phpunit", "tox", "nox", "ava", "tap", "karma", "playwright", "cypress"}

# Regex fallback for embedded code we do not parse (node/perl/ruby/php, or python that fails to parse).
INTERP_DANGER = [
    (re.compile(r"\b(os\.remove|os\.unlink|os\.rmdir|shutil\.rmtree|os\.removedirs|unlinkSync|rmSync|rmdirSync|rimraf|fs\.rm\b|fs\.unlink\b|fs\.rmdir\b|"
                r"File\.delete|File\.unlink|FileUtils\.rm\w*|FileUtils\.remove\w*|Dir\.rmdir|rmtree|remove_tree|\.unlink\(|\bunlink\b|\brmdir\b)"), "delete"),
    (re.compile(r"\bopen\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"][wax]"), "write"),
    (re.compile(r"\b(writeFileSync|writeFile|appendFileSync|appendFile|write_text|write_bytes|File\.write|IO\.write|file_put_contents|Path\([^)]*\)\.write)"), "write"),
    (re.compile(r"\b(subprocess|os\.system|os\.popen|child_process|execSync|execFileSync|spawnSync|exec\(|spawn\(|system\(|shell_exec|passthru|popen|qx[({/]|%x[({])"), "exec_opaque"),
    (re.compile(r"\b(requests\.|urllib|httpx|aiohttp|fetch\(|axios|https?\.get|https?\.request|require\(\s*['\"]https?['\"]\s*\)|Net::HTTP|open-uri|LWP|HTTP::Tiny|"
                r"IO::Socket|socket\.|curl_exec|file_get_contents\(\s*['\"]https?:)"), "network"),
]

_GIT_KNOWN = set("""add am annotate apply archive bisect blame branch bundle cat-file check-attr check-ignore check-mailmap
check-ref-format checkout checkout-index cherry cherry-pick citool clean clone column commit commit-graph commit-tree config
count-objects credential describe diff diff-files diff-index diff-tree difftool fast-export fast-import fetch fetch-pack
filter-branch filter-repo fmt-merge-msg for-each-ref format-patch fsck gc get-tar-commit-id grep gui hash-object help
index-pack init instaweb interpret-trailers log ls-files ls-remote ls-tree mailinfo mailsplit maintenance merge merge-base
merge-file merge-index merge-tree mergetool mktag mktree multi-pack-index mv name-rev notes pack-objects pack-redundant
pack-refs patch-id prune prune-packed pull push range-diff read-tree rebase reflog remote repack replace request-pull
rerere reset restore rev-list rev-parse revert rm send-email send-pack shortlog show show-branch show-index show-ref
sparse-checkout stage stash status stripspace submodule switch symbolic-ref tag unpack-file unpack-objects update-index
update-ref update-server-info var verify-commit verify-pack verify-tag version whatchanged worktree write-tree lfs""".split())

_SEPARATORS = {";", "&&", "||", "|", "&", "|&", ";;", "\n"}
_OPS = sorted(["&&", "||", ";;", ";", "&>>", "&>", "|&", "|", "&", ">>", ">|", ">&", "<&", "<<<", "<<-", "<<", "<>", "<", ">", "(", ")"],
              key=len, reverse=True)
_OPCHARS = set(";&|<>()")
_KEYWORDS_PREFIX = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "time", "coproc"}
_KEYWORDS_END = {"fi", "done", "esac", "}"}
_NOOP_BUILTINS = {":", "true", "false", "[", "[[", "test", "cd", "pushd", "popd", "export", "set", "unset", "shift", "local",
                  "declare", "typeset", "readonly", "exit", "return", "break", "continue", "wait", "shopt", "alias", "unalias",
                  "hash", "ulimit", "umask", "case", "esac", "select", "let"}


# =============================================================================== lexer

class Op(str):
    """A shell operator token (separator, redirect, paren)."""


@dataclass
class Word:
    """A shell word as segments: ('lit', s) no expansion, ('uq', s) unquoted, ('dq', s) double-quoted, ('sub', i, quoted)."""
    segs: list = field(default_factory=list)
    heredoc: int = -1

    def add(self, mode: str, text: str) -> None:
        if not text:
            return
        if self.segs and self.segs[-1][0] == mode and mode != "sub":
            self.segs[-1] = (mode, self.segs[-1][1] + text)
        else:
            self.segs.append((mode, text))

    def add_sub(self, idx: int, quoted: bool) -> None:
        self.segs.append(("sub", idx, quoted))

    @property
    def text(self) -> str:
        """Best-effort source-ish text (for keywords, details, function names)."""
        out = []
        for s in self.segs:
            out.append("$(…)" if s[0] == "sub" else s[1])
        return "".join(out)

    def is_plain(self, value: str) -> bool:
        return len(self.segs) == 1 and self.segs[0][0] == "uq" and self.segs[0][1] == value


@dataclass
class Lexed:
    tokens: list
    heredocs: list      # idx -> (body, quoted_delimiter)
    subs: list          # idx -> (inner_text, kind)   kind in $( ` <( >(


def _match_paren(s: str, i: int) -> int:
    """s[i-1] == '('; return the index just past the matching ')', respecting quotes. len(s) if unbalanced."""
    depth, n = 1, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == '"':
            i += 1
            while i < n and s[i] != '"':
                i += 2 if s[i] == "\\" else 1
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _match_brace(s: str, i: int) -> int:
    """s[i-1] == '{' of a ${...}; return index past the matching '}'."""
    depth, n = 1, len(s)
    while i < n:
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


_ANSI_SIMPLE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
                "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _decode_escapes(body: str) -> str:
    """Decode bash $'...' / echo -e / printf escapes."""
    out, i, n = [], 0, len(body)
    while i < n:
        c = body[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        d = body[i + 1]
        if d in _ANSI_SIMPLE:
            out.append(_ANSI_SIMPLE[d])
            i += 2
        elif d == "x":
            m = re.match(r"[0-9a-fA-F]{1,2}", body[i + 2:])
            if m:
                out.append(chr(int(m.group(), 16)))
                i += 2 + len(m.group())
            else:
                out.append("\\x")
                i += 2
        elif d in "uU":
            m = re.match(r"[0-9a-fA-F]{1,%d}" % (4 if d == "u" else 8), body[i + 2:])
            if m:
                out.append(chr(int(m.group(), 16)))
                i += 2 + len(m.group())
            else:
                out.append("\\" + d)
                i += 2
        elif d in "01234567":
            m = re.match(r"0?[0-7]{1,3}", body[i + 1:])
            out.append(chr(int(m.group(), 8) & 0xFF))
            i += 1 + len(m.group())
        elif d == "c" and i + 2 < n:
            out.append(chr(ord(body[i + 2]) & 0x1F))
            i += 3
        else:
            out.append("\\" + d)
            i += 2
    return "".join(out)


def lex(s: str) -> Lexed:
    toks: list = []
    heredocs: list = []
    subs: list = []
    pending: list = []          # heredocs whose body starts at the next newline: (idx, delim, strip_tabs)
    cur: Word | None = None
    i, n = 0, len(s)

    def word() -> Word:
        nonlocal cur
        if cur is None:
            cur = Word()
        return cur

    def flush() -> None:
        nonlocal cur
        if cur is not None:
            toks.append(cur)
            cur = None

    def take_heredocs(i: int) -> int:
        for idx, delim, strip in pending:
            lines = []
            while i < n:
                j = s.find("\n", i)
                line = s[i:] if j < 0 else s[i:j]
                i = n if j < 0 else j + 1
                if (line.lstrip("\t") if strip else line) == delim:
                    break
                lines.append(line.lstrip("\t") if strip else line)
            heredocs[idx] = ("\n".join(lines) + ("\n" if lines else ""), heredocs[idx][1])
        pending.clear()
        return i

    def add_sub(start: int, kind: str, quoted: bool) -> int:
        if kind == "`":
            j = start
            while j < n and s[j] != "`":
                j += 2 if s[j] == "\\" else 1
            inner, end = s[start:j].replace("\\`", "`"), min(j + 1, n)
        else:
            end = _match_paren(s, start)
            inner = s[start:end - 1] if end <= n and s[end - 1:end] == ")" else s[start:end]
        subs.append((inner, kind))
        word().add_sub(len(subs) - 1, quoted)
        return end

    while i < n:
        c = s[i]
        if c == "\\":
            if s.startswith("\\\n", i):
                i += 2
                continue
            if i + 1 < n:
                word().add("lit", s[i + 1])
            i += 2
            continue
        if c in " \t\r":
            flush()
            i += 1
            continue
        if c == "\n":
            flush()
            toks.append(Op("\n"))
            i += 1
            if pending:
                i = take_heredocs(i)
            continue
        if c == "#" and cur is None:
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "'":
            j = s.find("'", i + 1)
            j = n if j < 0 else j
            word().add("lit", s[i + 1:j])
            word()  # '' still makes a (possibly empty) word
            i = j + 1
            continue
        if c == "$" and s.startswith("$'", i):
            j = i + 2
            while j < n and s[j] != "'":
                j += 2 if s[j] == "\\" else 1
            word().add("lit", _decode_escapes(s[i + 2:j]))
            i = j + 1
            continue
        if c == "$" and s.startswith("$((", i):
            j = _match_paren(s, i + 2)
            word().add("uq", s[i:j])
            i = j
            continue
        if c == "$" and s.startswith("$(", i):
            i = add_sub(i + 2, "$(", False)
            continue
        if c == "$" and s.startswith("${", i):
            j = _match_brace(s, i + 2)
            word().add("uq", s[i:j])
            i = j
            continue
        if c == "`":
            i = add_sub(i + 1, "`", False)
            continue
        if c == '"' or (c == "$" and s.startswith('$"', i)):
            i += 2 if c == "$" else 1
            w = word()
            buf = []
            while i < n and s[i] != '"':
                ch = s[i]
                if ch == "\\" and i + 1 < n and s[i + 1] in '$`"\\\n':
                    w.add("dq", "".join(buf)); buf = []
                    if s[i + 1] != "\n":
                        w.add("lit", s[i + 1])
                    i += 2
                elif s.startswith("$(", i) and not s.startswith("$((", i):
                    w.add("dq", "".join(buf)); buf = []
                    i = add_sub(i + 2, "$(", True)
                elif ch == "`":
                    w.add("dq", "".join(buf)); buf = []
                    i = add_sub(i + 1, "`", True)
                elif s.startswith("${", i):
                    j = _match_brace(s, i + 2)
                    buf.append(s[i:j])
                    i = j
                else:
                    buf.append(ch)
                    i += 1
            w.add("dq", "".join(buf))
            i += 1
            continue
        if c in "<>" and s.startswith("(", i + 1):
            i = add_sub(i + 2, c + "(", False)
            continue
        if c in _OPCHARS:
            fd = ""
            if cur is not None and c in "<>" and len(cur.segs) == 1 and cur.segs[0][0] == "uq" and cur.segs[0][1].isdigit():
                fd, cur = cur.segs[0][1], None
            else:
                flush()
            op = next(o for o in _OPS if s.startswith(o, i))
            i += len(op)
            if op in ("<<", "<<-"):
                while i < n and s[i] in " \t":
                    i += 1
                j = i
                while j < n and s[j] not in " \t\n;&|<>()":
                    j += 1
                raw = s[i:j]
                i = j
                quoted = any(q in raw for q in "'\"\\")
                delim = raw.replace("'", "").replace('"', "").replace("\\", "")
                heredocs.append(("", quoted))
                pending.append((len(heredocs) - 1, delim, op == "<<-"))
                toks.append(Op(fd + "<<"))
                toks.append(Word(segs=[("lit", delim)], heredoc=len(heredocs) - 1))
                continue
            toks.append(Op(fd + op))
            continue
        word().add("uq", c)
        i += 1
    flush()
    if pending:
        take_heredocs(n)
    return Lexed(toks, heredocs, subs)


def _dq_substitutions(text: str) -> list[str]:
    """Command substitutions inside an unquoted heredoc body (which bash does execute)."""
    out, i = [], 0
    while i < len(text):
        if text.startswith("$(", i) and not text.startswith("$((", i):
            j = _match_paren(text, i + 2)
            out.append(text[i + 2:j - 1])
            i = j
        elif text[i] == "`":
            j = text.find("`", i + 1)
            if j < 0:
                break
            out.append(text[i + 1:j])
            i = j + 1
        else:
            i += 1
    return out


# =============================================================================== evaluation context

@dataclass
class Ctx:
    cwd: str
    root: str | None
    depth: int = 0
    fs: bool = True                                   # may we look at the project on disk?
    cwd_known: bool = True
    env: dict = field(default_factory=dict)           # name -> list[str]
    funcs: dict = field(default_factory=dict)         # name -> (Lexed, tokens)
    git_aliases: dict = field(default_factory=dict)   # name -> expansion string
    seen: frozenset = frozenset()                     # script files being classified (recursion guard)
    # absolute paths (or globs; "*" = unknown) written earlier in this command. Shared by every child
    # context: a program file written by an earlier segment must not be read from disk (stale content).
    written: set = field(default_factory=set)
    # absolute path -> exact content, for files written earlier in this command whose content is visible in the
    # command itself (`cat > gen.py <<'EOF' … EOF`): read instead of the stale on-disk copy
    contents: dict = field(default_factory=dict)

    def child(self, **kw) -> "Ctx":
        """A new process / subshell: copies of env and functions."""
        c = replace(self, env=dict(self.env), funcs=dict(self.funcs), git_aliases=dict(self.git_aliases))
        for k, v in kw.items():
            setattr(c, k, v)
        return c

    def N(self, p: str) -> str:
        if p == UNKNOWN:
            return UNKNOWN
        if not self.cwd_known and not (p.startswith("/") or p.startswith("~")):
            return UNKNOWN
        return normalize_path(p, self.cwd, self.root)

    def abs(self, p: str) -> str | None:
        """Absolute filesystem path for p, or None when unresolvable."""
        if "$" in p or "`" in p or (not self.cwd_known and not p.startswith(("/", "~"))):
            return None
        p = os.path.expanduser(p)
        return posixpath.normpath(p if posixpath.isabs(p) else posixpath.join(self.cwd, p))

    def note_written(self, effs: list[Effect]) -> None:
        base = self.root or self.cwd
        for e in effs:
            if e.action not in ("write", "edit", "delete", "move", "discard") or e.kind not in ("path", "opaque"):
                continue
            if e.target in (UNKNOWN, EVERYTHING) or "$" in e.target:
                self.written.add("*")
            else:
                self.written.add(posixpath.normpath(e.target if posixpath.isabs(e.target) else posixpath.join(base, e.target)))

    def stale(self, ap: str) -> bool:
        """Was `ap` (absolute) possibly written earlier in this command?"""
        if not self.written:
            return False
        if "*" in self.written:
            return True
        for w in self.written:
            if ap == w or ap.startswith(w.rstrip("/") + "/"):
                return True
            if any(c in w for c in "*?[") and fnmatch.fnmatchcase(ap, w):
                return True
        return False

    def read_file(self, p: str) -> str | None:
        """Read a project file (inside root, or cwd when there is no root). None if not allowed / missing,
        or if it may have been rewritten earlier in this same command (the on-disk content is stale)."""
        if not self.fs:
            return None
        ap = self.abs(p)
        if ap and ap in self.contents:
            return self.contents[ap]
        if ap and self.stale(ap):
            return None
        base = self.root or self.cwd
        if not ap or not base or not os.path.isdir(base):
            return None
        try:
            real, rbase = os.path.realpath(ap), os.path.realpath(base)
            if not (real == rbase or real.startswith(rbase.rstrip("/") + "/")):
                return None
            if not os.path.isfile(real) or os.path.getsize(real) > MAX_SCRIPT_BYTES:
                return None
            with open(real, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None


def _uncertain(effs: list[Effect], note: str = "") -> list[Effect]:
    for e in effs:
        e.certain = False
        if note and note not in e.detail:
            e.detail = f"{note}: {e.detail}" if e.detail else note
    return effs


def _opaque(target: str, detail: str, kind: str = "opaque") -> Effect:
    return Effect("exec_opaque", target, kind=kind, detail=detail, certain=False)


# =============================================================================== expansion

class _Each(list):
    """A `for` loop variable: expands to one field per item, quoted or not."""


_PARAM = re.compile(r"\$\{([^}]*)\}|\$([A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])")


def _lookup(name: str, ctx: Ctx) -> list[str] | None:
    if name in ctx.env:
        return ctx.env[name]
    if name == "HOME":
        return ["~"]
    if name == "PWD" and ctx.cwd_known:
        return [ctx.cwd]
    return None


def _param_value(m: re.Match, ctx: Ctx) -> list[str] | None:
    if m.group(2) is not None:
        return _lookup(m.group(2), ctx)
    inner = m.group(1)
    mm = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*|[0-9@*])(?:(:?[-=+])(.*))?", inner, re.S)
    if not mm:
        return None
    name, op, word = mm.group(1), mm.group(2), mm.group(3) or ""
    val = _lookup(name, ctx)
    if op is None:
        return val
    if op.endswith("+"):
        return [_expand_text(word, ctx)] if val else []
    empty = not val or (op.startswith(":") and val == [""])
    if empty:
        dv = _expand_text(word, ctx)
        if op.endswith("="):
            ctx.env[name] = [dv]
        return [dv]
    return val


def _expand_text(text: str, ctx: Ctx) -> str:
    return "".join(p for p, _ in _expand_params(text, ctx, quoted=True))


def _expand_params(text: str, ctx: Ctx, quoted: bool) -> list[tuple[str, str]]:
    """Split text into pieces: ('lit'|'split'|'fields'). Unresolvable parameters stay as literal `$...` text."""
    out: list[tuple[str, str]] = []
    pos = 0
    for m in _PARAM.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], "lit"))
        val = _param_value(m, ctx)
        if val is None:
            out.append((m.group(0), "lit"))
        elif isinstance(val, _Each):
            out.append(("\x00".join(val), "fields"))
        elif quoted and (m.group(2) == "@" or m.group(1) == "@"):
            out.append(("\x00".join(val), "fields"))
        elif quoted:
            out.append((" ".join(val), "lit"))
        else:
            out.append((" ".join(val), "split"))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], "lit"))
    return out


_BRACE_SEQ = re.compile(r"^(-?\d+|[A-Za-z])\.\.(-?\d+|[A-Za-z])$")


def _brace_expand(s: str, limit: int = 64) -> list[str]:
    """bash brace expansion: a{b,c}d -> abd acd; f{1..3} -> f1 f2 f3. Leaves ${..} and lone braces alone."""
    depth, start = 0, -1
    for i, c in enumerate(s):
        if c == "{" and (i == 0 or s[i - 1] != "$"):
            if depth == 0:
                start = i
            depth += 1
        elif c == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                body, pre, post = s[start + 1:i], s[:start], s[i + 1:]
                parts, d, cur = [], 0, ""
                for ch in body:
                    if ch == "," and d == 0:
                        parts.append(cur)
                        cur = ""
                        continue
                    d += ch == "{"
                    d -= ch == "}"
                    cur += ch
                parts.append(cur)
                if len(parts) == 1:
                    m = _BRACE_SEQ.match(body)
                    if not m:
                        continue
                    a, b = m.groups()
                    if a.lstrip("-").isdigit() and b.lstrip("-").isdigit():
                        lo, hi = int(a), int(b)
                        step = 1 if hi >= lo else -1
                        parts = [str(x) for x in range(lo, hi + step, step)][:limit]
                    elif a.isalpha() and b.isalpha():
                        step = 1 if ord(b) >= ord(a) else -1
                        parts = [chr(x) for x in range(ord(a), ord(b) + step, step)][:limit]
                    else:
                        continue
                out: list[str] = []
                for p in parts:
                    out += _brace_expand(pre + p + post, limit)
                    if len(out) >= limit:
                        break
                return out[:limit]
    return [s]


def expand(w: Word, ctx: Ctx, subvals: dict) -> list[str]:
    """Expand a word into fields (parameter expansion, static substitutions, field splitting). No globbing."""
    fields = [""]
    nonempty = False

    def put(piece: str, mode: str) -> None:
        nonlocal nonempty
        if mode == "lit":
            fields[-1] += piece
            nonempty = True
        elif mode == "fields":
            parts = piece.split("\x00")
            fields[-1] += parts[0]
            fields.extend(parts[1:])
            nonempty = nonempty or bool(piece)
        else:
            parts = piece.split()
            if not parts:
                return
            fields[-1] += parts[0]
            fields.extend(parts[1:])
            nonempty = True

    for seg in w.segs:
        if seg[0] == "lit":
            put(seg[1], "lit")
        elif seg[0] in ("uq", "dq"):
            for piece, mode in _expand_params(seg[1], ctx, quoted=seg[0] == "dq"):
                put(piece, mode)
        else:  # substitution
            val = subvals.get(seg[1])
            put("$(…)" if val is None else val, "lit" if seg[2] else "split")
    if not nonempty and fields == [""]:
        return [] if not any(s[0] in ("lit", "dq") for s in w.segs) else [""]
    if any(s[0] == "uq" and "{" in s[1] and ("," in s[1] or ".." in s[1]) for s in w.segs):
        fields = [x for f in fields for x in _brace_expand(f)]
    return fields


def _expand_one(w: Word, ctx: Ctx, subvals: dict) -> str:
    return " ".join(expand(w, ctx, subvals))


# =============================================================================== runner (token stream -> simple commands)

@dataclass
class Stdin:
    kind: str = "none"         # none | text | file | remote | unknown
    text: str = ""
    path: str = ""
    detail: str = ""


def _classify_text(cmd: str, ctx: Ctx) -> list[Effect]:
    if not cmd.strip():
        return []
    if ctx.depth > MAX_DEPTH:
        return [_opaque(UNKNOWN, cmd)]
    lexed = lex(cmd)
    return _run_tokens(lexed, lexed.tokens, ctx, Stdin())


def _find_close(tokens: list, i: int, open_w: str, close_w: str) -> int:
    """tokens[i] opens; return index of the matching close token (by literal word / op)."""
    depth = 0
    for j in range(i, len(tokens)):
        t = tokens[j]
        tv = t if isinstance(t, Op) else (t.text if isinstance(t, Word) and len(t.segs) == 1 and t.segs[0][0] == "uq" else None)
        if tv == open_w:
            depth += 1
        elif tv == close_w:
            depth -= 1
            if depth == 0:
                return j
    return len(tokens)


def _try_function_def(tokens: list, i: int) -> tuple[str, int, int] | None:
    """If a function definition starts at tokens[i], return (name, body_start, body_end_exclusive)."""
    t = tokens[i]
    if not isinstance(t, Word):
        return None
    j = i
    if t.is_plain("function") and i + 1 < len(tokens) and isinstance(tokens[i + 1], Word):
        name = tokens[i + 1].text
        j = i + 2
        if j + 1 < len(tokens) and tokens[j] == "(" and tokens[j + 1] == ")":
            j += 2
    elif i + 2 < len(tokens) and tokens[i + 1] == "(" and tokens[i + 2] == ")" and re.fullmatch(r"[\w.:-]+", t.text or ""):
        name = t.text
        j = i + 3
    else:
        return None
    while j < len(tokens) and tokens[j] == "\n":
        j += 1
    if j >= len(tokens):
        return None
    if isinstance(tokens[j], Word) and tokens[j].is_plain("{"):
        k = _find_close(tokens, j, "{", "}")
        return name, j + 1, k
    if tokens[j] == "(":
        k = _find_close(tokens, j, "(", ")")
        return name, j + 1, k
    return None


def _run_tokens(lexed: Lexed, tokens: list, ctx: Ctx, stdin0: Stdin) -> list[Effect]:
    out: list[Effect] = []
    stack: list = []
    cmd: list = []
    piped = False
    prev_out = Stdin()
    i = 0
    while True:
        t = tokens[i] if i < len(tokens) else None
        at_end = t is None
        if not at_end and isinstance(t, Word) and not cmd:
            fd = _try_function_def(tokens, i)
            if fd:
                name, a, b = fd
                ctx.funcs[name] = (lexed, tokens[a:b])
                i = b + 1
                continue
        if at_end or (isinstance(t, Op) and (t in _SEPARATORS or t in ("(", ")"))):
            if cmd:
                stdin = prev_out if piped else (stdin0 if not out and stdin0.kind != "none" else Stdin())
                effs, prev_out = _exec_simple(lexed, cmd, ctx, stdin)
                out.extend(effs)
                cmd = []
            elif not (isinstance(t, Op) and t in ("(", ")")):
                prev_out = Stdin()
            if at_end:
                break
            if t == "(":
                stack.append((ctx.cwd, ctx.cwd_known, dict(ctx.env)))
            elif t == ")":
                if stack:
                    ctx.cwd, ctx.cwd_known, ctx.env = stack.pop()
            piped = t in ("|", "|&")
            i += 1
            continue
        cmd.append(t)
        i += 1
    return out


def _static_sub_value(inner: str) -> str | None:
    """$(echo rm) -> 'rm', `which rm` -> 'rm'. None when not statically known."""
    try:
        w = shlex.split(inner.strip())
    except ValueError:
        return None
    if not w:
        return None
    if w[0] in ("echo", "printf") and len(w) >= 2 and all("$" not in x and "`" not in x for x in w[1:]):
        args = [a for a in w[1:] if a not in ("-n", "-e", "-E")] if w[0] == "echo" else [w[1].replace("%s", "").replace("\\n", "")]
        return " ".join(args).strip()
    if (w[0] in ("which", "realpath") and len(w) == 2) or (w[:2] in (["command", "-v"], ["type", "-P"], ["type", "-p"]) and len(w) == 3):
        return posixpath.basename(w[-1]) if w[0] != "realpath" else w[-1]
    return None


def _exec_simple(lexed: Lexed, toks: list, ctx: Ctx, stdin: Stdin) -> tuple[list[Effect], Stdin]:
    """Expand and classify one simple command. Returns (effects, what it writes to stdout).
    Files it writes are remembered so later segments do not trust their on-disk content."""
    effects, out = _exec_simple_inner(lexed, toks, ctx, stdin)
    ctx.note_written(effects)
    return effects, out


def _exec_simple_inner(lexed: Lexed, toks: list, ctx: Ctx, stdin: Stdin) -> tuple[list[Effect], Stdin]:
    effects: list[Effect] = []
    # 1. command substitutions run first
    subvals: dict = {}
    for t in toks:
        if isinstance(t, Word):
            for seg in t.segs:
                if seg[0] == "sub":
                    inner, kind = lexed.subs[seg[1]]
                    effects += _uncertain(_classify_text(inner, ctx.child(depth=ctx.depth + 1)))
                    subvals[seg[1]] = _static_sub_value(inner) if kind in ("$(", "`") else None
    # 2. split off redirects
    words: list[Word] = []
    redirs: list[tuple[str, Word | None]] = []
    j = 0
    while j < len(toks):
        t = toks[j]
        if isinstance(t, Op):
            nxt = toks[j + 1] if j + 1 < len(toks) and isinstance(toks[j + 1], Word) else None
            redirs.append((t, nxt))
            j += 2 if nxt is not None else 1
            continue
        words.append(t)
        j += 1
    # 3. keywords / loop headers
    while words and len(words[0].segs) == 1 and words[0].segs[0][0] == "uq" and words[0].segs[0][1] in (_KEYWORDS_PREFIX | _KEYWORDS_END):
        words = words[1:]
    if words and words[0].is_plain("for") and len(words) >= 2:
        name = words[1].text
        vals = [f for w in words[3:] for f in expand(w, ctx, subvals)] if len(words) > 2 and words[2].is_plain("in") else ["$@"]
        ctx.env[name] = _Each(vals)
        return effects, Stdin()
    # 4. expand
    fields = [f for w in words for f in expand(w, ctx, subvals)]
    assigns: list[str] = []
    k = 0
    while k < len(fields) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*\+?=", fields[k]):
        assigns.append(fields[k])
        k += 1
    argv = fields[k:]
    if not argv:
        for a in assigns:
            n, _, v = a.partition("=")
            ctx.env[n.rstrip("+")] = [v]
    # 5. redirects
    r_effs, stdin, truncating = _redirect_effects(redirs, lexed, ctx, subvals, stdin, argv)
    effects += r_effs
    if truncating is not None:
        effects += truncating
        return effects, Stdin()
    if not argv:
        return effects, Stdin()
    # 6. classify
    effs, out = _classify_argv(argv, ctx, stdin, assigns)
    effects += effs
    # `cat > f <<'EOF' … EOF` (or `cat <<EOF > f`): the new content of f is known exactly
    outs = [(op, w) for op, w in redirs if op.lstrip("0123456789") in (">", ">>", ">|", "&>", "&>>")]
    if argv[:1] == ["cat"] and all(a == "-" for a in argv[1:]) and stdin.kind == "text" and len(outs) == 1 \
            and outs[0][0].lstrip("0123456789") in (">", ">|") and outs[0][1] is not None:
        ap = ctx.abs(_expand_one(outs[0][1], ctx, subvals))
        quoted = all(not (w is not None and w.heredoc >= 0 and not lexed.heredocs[w.heredoc][1]) for op, w in redirs)
        if ap and quoted:
            ctx.contents[ap] = stdin.text
    return effects, out


_DEVNULLS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")


def _is_zeroing_command(argv: list[str]) -> bool:
    if not argv:
        return True
    cmd, args = posixpath.basename(argv[0]), argv[1:]
    return (cmd in (":", "true", "false") and not args) or (cmd == "cat" and args == ["/dev/null"]) \
        or (cmd == "echo" and args == ["-n"]) or (cmd == "printf" and args in ([""], []))


def _redirect_effects(redirs, lexed: Lexed, ctx: Ctx, subvals: dict, stdin: Stdin, argv: list[str]):
    effs: list[Effect] = []
    trunc: list[Effect] | None = None
    zeroing = _is_zeroing_command(argv)
    detail = " ".join(argv)
    for op, w in redirs:
        core = op.lstrip("0123456789")
        if core in (">", ">>", "&>", "&>>", ">|", ">&", "<>") and w is not None:
            tgt = _expand_one(w, ctx, subvals)
            if core in (">&",) and (tgt.isdigit() or tgt == "-"):
                continue
            if tgt in _DEVNULLS or tgt.startswith("/dev/fd/"):
                continue
            if core == "<>":
                continue
            p = ctx.N(tgt)
            d = f"redirect {op} {tgt}" + (f" ({detail})" if detail else "")
            if zeroing and core in (">", ">|", "&>"):
                trunc = (trunc or []) + [Effect("delete", p, detail=d + " truncates"), Effect("write", p, detail=d)]
            else:
                effs.append(Effect("write", p, detail=d))
        elif core == "<" and w is not None:
            tgt = _expand_one(w, ctx, subvals)
            stdin = Stdin("file", path=tgt, detail=f"< {tgt}")
        elif core == "<<" and w is not None and w.heredoc >= 0:
            body, quoted = lexed.heredocs[w.heredoc]
            if not quoted:
                for inner in _dq_substitutions(body):
                    effs += _uncertain(_classify_text(inner, ctx.child(depth=ctx.depth + 1)))
            stdin = Stdin("text", text=body, detail="heredoc")
        elif core == "<<<" and w is not None:
            stdin = Stdin("text", text=_expand_one(w, ctx, subvals) + "\n", detail="here-string")
    if trunc is not None and argv and not zeroing:
        trunc = None
    return effs, stdin, trunc


# =============================================================================== simple-command classification

def _strip_wrappers(words: list[str]) -> tuple[list[str], list[str]]:
    """Remove sudo/env/nohup/timeout/... wrappers. Returns (assignments seen, remaining words)."""
    assigns: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            assigns.append(w)
            i += 1
            continue
        b = posixpath.basename(w)
        if b in WRAPPERS:
            if b == "command" and i + 1 < len(words) and words[i + 1] in ("-v", "-V"):
                return assigns, words[i:]          # a lookup, not an execution
            i += 1
            while i < len(words) and words[i].startswith("-") and words[i] != "-":
                flag = words[i]
                i += 1
                split = None
                if b == "env" and flag in ("-S", "--split-string") and i < len(words):
                    split, i = words[i], i + 1
                elif b == "env" and (flag.startswith("--split-string=") or (flag.startswith("-S") and len(flag) > 2)):
                    split = flag.split("=", 1)[1] if flag.startswith("--") else flag[2:]
                if split is not None:          # env -S 'cmd args': the string is the command
                    try:
                        parts = shlex.split(split)
                    except ValueError:
                        parts = split.split()
                    more_assigns, rest = _strip_wrappers(parts + words[i:])
                    return assigns + more_assigns, rest
                if flag in WRAPPER_VALUE_FLAGS.get(b, ()) and i < len(words):
                    i += 1
            i += WRAPPER_SKIP_ARGS.get(b, 0)
            continue
        break
    return assigns, words[i:]


def _paths_after_opts(args: list[str]) -> list[str]:
    """Positional (non-option) arguments; everything after a bare `--` is positional."""
    if "--" in args:
        i = args.index("--")
        return [a for a in args[:i] if not a.startswith("-")] + list(args[i + 1:])
    return [a for a in args if not a.startswith("-") or a == "-"]


def _positionals(args: list[str], value_flags: set[str] = frozenset()) -> list[str]:
    """Positionals, skipping the values of flags that take one."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out += args[i + 1:]
            break
        if a.startswith("-") and a != "-":
            if a in value_flags:
                i += 1
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def _basename(cmd: str) -> str:
    return cmd.rsplit("/", 1)[-1]


def _everything_or(paths: list[str], ctx: Ctx) -> list[str]:
    inside = ctx.cwd_known and ctx.root is not None and not normalize_path(".", ctx.cwd, ctx.root).startswith("/")
    if any(p in ("/", "~", "~/") or p.startswith("/*") for p in paths):
        return [EVERYTHING]
    if (inside or ctx.root is None) and any(p in ("*", ".", "./", "./*") for p in paths):
        return [EVERYTHING]
    return paths


def _pathify(paths: list[str], ctx: Ctx) -> list[str]:
    return [p if p in (EVERYTHING, UNKNOWN) else ctx.N(p) for p in paths]


def _classify_argv(argv: list[str], ctx: Ctx, stdin: Stdin, assigns0: list[str] | None = None) -> tuple[list[Effect], Stdin]:
    """Classify one expanded simple command. Returns (effects, stdout description for a following pipe)."""
    assigns, words = _strip_wrappers(argv)
    assigns = list(assigns0 or []) + assigns
    if not words:
        return [], Stdin()
    head = words[0]
    detail = " ".join(assigns + words)
    if "$" in head or "`" in head or head == "":
        # command name produced by an unknown variable / substitution
        return [_opaque(UNKNOWN, detail)], Stdin("unknown")
    cmd = _basename(head)
    args = words[1:]
    if cmd in ctx.funcs and ctx.depth <= MAX_DEPTH:
        lexed, body = ctx.funcs[cmd]
        sub = ctx.child(depth=ctx.depth + 1)
        for k, v in enumerate(args, 1):
            sub.env[str(k)] = [v]
        sub.env["@"] = sub.env["*"] = list(args)
        sub.env["#"] = [str(len(args))]
        del sub.funcs[cmd]                      # no recursion
        return _run_tokens(lexed, body, sub, stdin), Stdin("unknown")
    if cmd == "coreutils" and args:
        # multicall binary: `coreutils --coreutils-prog=rm …` / `coreutils rm …`
        if args[0].startswith("--coreutils-prog="):
            return _classify_argv([args[0].split("=", 1)[1]] + args[1:], ctx, stdin, assigns)
        if not args[0].startswith("-"):
            return _classify_argv(args, ctx, stdin, assigns)
    if re.fullmatch(r"pip\d(?:\.\d+)?", cmd):
        cmd = "pip"
    if args and all(a in ("--help", "--version") or a.startswith("-") for a in args) and any(a in ("--help", "--version") for a in args) \
            and cmd not in INTERPRETERS and cmd not in SHELLS and not re.fullmatch(r"python\d(\.\d+)?|pypy\d?", cmd) \
            and not head.startswith(("./", "/")) and "/" not in head:
        return [Effect("run", detail, kind="command", detail=detail)], Stdin("unknown")     # help / version probe
    handler = _HANDLERS.get(cmd)
    if handler is None:
        if cmd in INTERPRETERS or re.fullmatch(r"python\d(\.\d+)?|pypy\d?", cmd):
            handler = _interpreter
        elif cmd in SHELLS:
            handler = _shell
    if handler is not None:
        res = handler(cmd, args, ctx, stdin, detail)
        if isinstance(res, tuple):
            return res
        return res, Stdin("unknown")
    return _generic(cmd, head, args, ctx, stdin, detail), Stdin("unknown")


def _generic(cmd: str, head: str, args: list[str], ctx: Ctx, stdin: Stdin, detail: str) -> list[Effect]:
    joined = detail
    pos = _paths_after_opts(args)
    if TEST_RUNNERS.match(" ".join([cmd] + args)) or cmd in TEST_TOOLS:
        return [Effect("test", joined, kind="command", detail=joined)]
    if cmd in _READERS:
        tgt = ctx.N(pos[0]) if pos and cmd not in ("echo", "printf", "grep", "rg", "ag", "egrep", "fgrep") else "."
        return [Effect("read", tgt, detail=joined)]
    if cmd in _NOOP_BUILTINS:
        return _builtin(cmd, args, ctx)
    if "/" in head:
        return _script_file(head, ctx, detail, lang=None)
    return [Effect("run", joined, kind="command", detail=joined)]


_READERS = {"cat", "ls", "head", "tail", "less", "more", "wc", "grep", "egrep", "fgrep", "rg", "ag", "echo", "printf", "pwd",
            "which", "type", "file", "stat", "du", "df", "tree", "diff", "cmp", "sort", "uniq", "cut", "tr", "jq", "yq", "date",
            "printenv", "sleep", "basename", "dirname", "realpath", "readlink", "md5sum", "sha256sum", "sha1sum", "history",
            "nl", "od", "xxd", "hexdump", "strings", "column", "comm", "join", "paste", "fold", "fmt", "expand", "rev", "tac",
            "whoami", "id", "uname", "hostname", "ps", "top", "env", "seq", "yes", "base64", "man", "info", "help", "whatis",
            "apropos", "lsof", "free", "uptime", "locale", "tput", "clear", "true"}


def _builtin(cmd: str, args: list[str], ctx: Ctx) -> list[Effect]:
    if cmd in ("cd", "pushd"):
        if not args or args[0] in ("~", "-"):
            ctx.cwd, ctx.cwd_known = (os.path.expanduser("~"), True) if not args or args[0] == "~" else (ctx.cwd, False)
            return []
        tgt = args[-1] if not args[-1].startswith("-") else (args[-2] if len(args) > 1 else "")
        ap = ctx.abs(tgt)
        if ap is None:
            ctx.cwd_known = False
        else:
            ctx.cwd = ap
        return []
    if cmd in ("export", "local", "declare", "typeset", "readonly"):
        for a in args:
            if "=" in a and not a.startswith("-"):
                n, _, v = a.partition("=")
                ctx.env[n] = [v]
        return []
    if cmd == "unset":
        for a in args:
            ctx.env.pop(a, None)
        return []
    return []


# ----------------------------------------------------------------------------- file ops

def _h_delete(cmd, args, ctx, stdin, detail):
    if "--help" in args or "--version" in args:
        return [Effect("run", detail, kind="command", detail=detail)]
    targets = _everything_or(_paths_after_opts(args), ctx) or [UNKNOWN]
    return [Effect("delete", t, detail=detail) for t in _pathify(targets, ctx)]


def _h_find(cmd, args, ctx, stdin, detail):
    roots = []
    for a in args:
        if a.startswith("-") or a in ("(", "!", ")"):
            break
        roots.append(a)
    roots = roots or ["."]
    root_t = _pathify(_everything_or(roots[:1], ctx), ctx)[0]
    names = [args[k + 1] for k, a in enumerate(args) if a in ("-name", "-iname") and k + 1 < len(args)]
    effs: list[Effect] = [Effect("write", ctx.N(args[k + 1]), detail=detail)
                          for k, a in enumerate(args) if a in ("-fprint", "-fprint0", "-fls", "-fprintf") and k + 1 < len(args)]

    def with_filter(action: str) -> list[Effect]:
        """With a name filter: root/**/<name> per filter (the root itself would over-block rules on unrelated
        paths, e.g. `find . -name __pycache__ -delete` under "never delete data/"). Otherwise the root."""
        if names and root_t != UNKNOWN:
            return [Effect(action, f"**/{n}" if root_t == EVERYTHING else f"{root_t}/**/{n}", detail=detail, certain=False) for n in names]
        return [Effect(action, root_t, detail=detail, certain=False)]
    if "-delete" in args:
        effs += with_filter("delete")
    i = 0
    while i < len(args):
        if args[i] in ("-exec", "-execdir", "-ok", "-okdir"):
            j = i + 1
            while j < len(args) and args[j] not in (";", "\\;", "+"):
                j += 1
            inner = [UNKNOWN if a == "{}" else a for a in args[i + 1:j]]
            if inner:
                sub, _ = _classify_argv(inner, ctx.child(depth=ctx.depth + 1), Stdin())
                for e in sub:
                    if e.action in ("read", "run"):
                        continue
                    if e.target in (UNKNOWN,) or e.kind == "path":
                        effs += [x for x in with_filter(e.action) if (x.action, x.target) not in {(y.action, y.target) for y in effs}]
                        continue
                    e.certain = False
                    e.detail = detail
                    effs.append(e)
            i = j
        i += 1
    return effs or [Effect("read", root_t if root_t != EVERYTHING else ".", detail=detail)]


def _target_dir(args: list[str]) -> str | None:
    """Value of -t DIR / --target-directory DIR / --target-directory=DIR."""
    for k, a in enumerate(args):
        if a in ("-t", "--target-directory") and k + 1 < len(args):
            return args[k + 1]
        if a.startswith("--target-directory="):
            return a.split("=", 1)[1]
        if a.startswith("-t") and len(a) > 2 and not a.startswith("--"):
            return a[2:]
    return None


def _in_dir(d: str, src: str) -> str:
    return posixpath.join(d, posixpath.basename(src.rstrip("/")))


def _h_mv(cmd, args, ctx, stdin, detail):
    pos = _positionals(args, {"-t", "--target-directory", "-S", "--suffix"})
    tdir = _target_dir(args)
    if tdir:
        return [Effect("move", ctx.N(s), detail=detail) for s in pos] + [Effect("write", ctx.N(_in_dir(tdir, s)), detail=detail) for s in pos]
    if len(pos) >= 2:
        srcs, dst = pos[:-1], pos[-1]
    else:
        return [Effect("move", UNKNOWN, detail=detail, certain=False)]
    if dst in _DEVNULLS:
        return [Effect("delete", ctx.N(s), detail=detail) for s in srcs]
    return [Effect("move", ctx.N(s), detail=detail) for s in srcs] + [Effect("write", ctx.N(dst), detail=detail)]


def _h_cp(cmd, args, ctx, stdin, detail):
    if cmd == "rsync":
        return _h_rsync(args, ctx, detail)
    if cmd == "cp" and any(a in ("--link", "--symbolic-link") or (a.startswith("-") and not a.startswith("--") and ("l" in a or "s" in a))
                           for a in args):
        # cp -l / -al / -s makes links, not copies: the sources become writable under the new names (REVIEW-2 B4)
        effs = _ln([a for a in args if not (a.startswith("-") and not a.startswith("--") and set(a[1:]) <= set("alsrRfvpPdHLnuix"))
                    and a not in ("--link", "--symbolic-link", "--recursive", "--archive")], ctx, detail)
        if any("r" in a.lower() or a in ("-a", "--archive", "--recursive") for a in args if a.startswith("-")):
            effs = [replace(e, target=(e.target.rstrip("/") + "/**") if e.action == "edit" and e.target not in (UNKNOWN, EVERYTHING) else e.target)
                    for e in effs] + [e for e in effs if e.action == "edit"]
        return effs
    vals = {"-t", "--target-directory", "-S", "--suffix"} | ({"-m", "--mode", "-o", "-g", "--owner", "--group"} if cmd == "install" else set())
    pos = _positionals(args, vals)
    tdir = _target_dir(args)
    if tdir:
        return [Effect("write", ctx.N(_in_dir(tdir, s)), detail=detail) for s in pos] or [Effect("write", ctx.N(tdir), detail=detail)]
    if len(pos) < 2:
        return [Effect("run", detail, kind="command", detail=detail)]
    dst = ctx.N(pos[-1])
    if all(p in _DEVNULLS for p in pos[:-1]):
        return [Effect("delete", dst, detail=detail + " (zeroes the file)"), Effect("write", dst, detail=detail)]
    return [Effect("write", dst, detail=detail)]


_REMOTE_PATH = re.compile(r"^(?:[\w.+-]+@)?([\w.-]+):(?!//)")


def _h_rsync(args, ctx, detail):
    pos = _positionals(args, {"-e", "--rsh", "--exclude", "--include", "--filter", "-f", "--port", "--password-file", "--chmod", "--rsync-path"})
    if len(pos) < 2:
        return [Effect("run", detail, kind="command", detail=detail)]
    effs = []
    remote = [p for p in pos if _REMOTE_PATH.match(p) and not p.startswith("/")]
    for r in remote:
        effs.append(Effect("network", _REMOTE_PATH.match(r).group(1), kind="url", detail=detail))
    dst = pos[-1]
    if dst not in remote:
        effs.append(Effect("write", ctx.N(dst), detail=detail))
        if any(a == "--del" or a.startswith("--delete") for a in args):
            effs.append(Effect("delete", ctx.N(dst), detail=detail))
    if "--remove-source-files" in args or "--remove-sent-files" in args:
        effs += [Effect("delete", ctx.N(s), detail=detail) for s in pos[:-1] if s not in remote]
    return effs


def _h_truncate(cmd, args, ctx, stdin, detail):
    size, files, i = None, [], 0
    while i < len(args):
        a = args[i]
        if a in ("-s", "--size") and i + 1 < len(args):
            size = args[i + 1]
            i += 2
            continue
        if a.startswith("--size="):
            size = a.split("=", 1)[1]
        elif a.startswith("-s") and len(a) > 2:
            size = a[2:]
        elif a in ("-r", "--reference") and i + 1 < len(args):
            i += 2
            continue
        elif not a.startswith("-"):
            files.append(a)
        i += 1
    if not files:
        return [Effect("write", UNKNOWN, detail=detail)]
    out = []
    for f in files:
        p = ctx.N(f)
        if size is not None and re.fullmatch(r"0+[KMGTPE]?i?B?", size):
            out.append(Effect("delete", p, detail=detail + " (zeroes the file)"))
        out.append(Effect("write", p, detail=detail))
    return out


def _h_dd(cmd, args, ctx, stdin, detail):
    kv = dict(a.split("=", 1) for a in args if "=" in a)
    if "of" not in kv:
        return [Effect("write", UNKNOWN, detail=detail)]
    p = ctx.N(kv["of"])
    zero = kv.get("if") == "/dev/null" or kv.get("count") == "0" or kv.get("count", "").strip("0") == "" and "count" in kv
    return ([Effect("delete", p, detail=detail + " (zeroes the file)")] if zero else []) + [Effect("write", p, detail=detail)]


def _h_tee(cmd, args, ctx, stdin, detail):
    return [Effect("write", ctx.N(p), detail=detail) for p in _paths_after_opts(args)], stdin


_SED_WIPE = re.compile(r"(?:(?:0|1)\s*,\s*\$|,\s*\$)?\s*d\s*;?|/(?:\^|\$|\.\*|\^\.\*|\^\.\*\$|)/\s*d\s*;?|\$!d\s*;\s*d")


def _h_sed(cmd, args, ctx, stdin, detail):
    inplace = any(a == "-i" or (a.startswith("-i") and not a.startswith("--")) or a.startswith("--in-place") or
                  (re.fullmatch(r"-[a-zA-Z]*i[a-zA-Z]*", a) is not None and not a.startswith("--")) for a in args)
    script_vals = {args[i + 1] for i, a in enumerate(args) if a in ("-e", "-f", "--expression", "--file") and i + 1 < len(args)}
    pos = [p for p in _paths_after_opts(args) if p not in script_vals]
    if not script_vals and pos:
        pos = pos[1:]   # first positional is the script
    if inplace:
        # `sed -i '/x/d' f` is a content edit; a script that removes *every* line truncates the file, which is
        # destruction exactly like `: > f` or `truncate -s0 f` (red team del-31, REVIEW B20)
        scripts = list(script_vals) if script_vals else ([_paths_after_opts(args)[0]] if _paths_after_opts(args) else [])
        quiet = any(a in ("-n", "--quiet", "--silent") or re.fullmatch(r"-[a-zA-Z]*n[a-zA-Z]*", a or "") for a in args)
        wipes = any(_SED_WIPE.fullmatch(sc.strip()) for sc in scripts) or \
            (quiet and not any(re.search(r"(?:^|[;}\s/])[pPw]\b|[pPw]\s*$", sc) for sc in scripts))
        effs = [Effect("edit", ctx.N(f), detail=detail) for f in pos] or [Effect("edit", UNKNOWN, detail=detail, certain=False)]
        if wipes:
            effs += [Effect("delete", ctx.N(f), detail=detail + " (script deletes every line: empties the file)") for f in pos]
        return effs
    return [Effect("read", ctx.N(p), detail=detail) for p in pos] or [Effect("read", ".", detail=detail)]


def _h_attr(cmd, args, ctx, stdin, detail):
    act = "write" if cmd in ("touch", "mkdir", "ln") else "edit"
    vals = {"-m", "--mode"} if cmd == "mkdir" else {"-t", "-r", "-d", "--date", "--reference"} if cmd == "touch" else set()
    pos = _positionals(args, vals)
    if cmd in ("chmod", "chown", "chgrp") and pos:
        pos = pos[1:]                               # the mode / owner
    if cmd == "ln":
        return _ln(args, ctx, detail)
    return [Effect(act, ctx.N(p), detail=detail) for p in pos] or [Effect(act, UNKNOWN, detail=detail)]


def _ln(args: list[str], ctx: Ctx, detail: str) -> list[Effect]:
    """ln [-s] SRC.. DST: writes the link name, and (uncertain) the source, which becomes writable
    under another name (hard link) or through the symlink."""
    pos = _positionals(args, {"-t", "--target-directory", "-S", "--suffix"})
    symbolic = any(a == "--symbolic" or (a.startswith("-") and not a.startswith("--") and "s" in a) for a in args)
    tdir = _target_dir(args)
    if tdir:
        pairs_ = [(s, _in_dir(tdir, s)) for s in pos]
    elif len(pos) >= 2:
        dst = pos[-1]
        pairs_ = [(s, _in_dir(dst, s) if len(pos) > 2 else dst) for s in pos[:-1]]
    elif len(pos) == 1:
        pairs_ = [(pos[0], posixpath.basename(pos[0].rstrip("/")))]
    else:
        return [Effect("write", UNKNOWN, detail=detail)]
    effs: list[Effect] = []
    for src, link in pairs_:
        effs.append(Effect("write", ctx.N(link), detail=detail))
        real_src = src if not symbolic or posixpath.isabs(src) else posixpath.join(posixpath.dirname(link), src)
        effs.append(Effect("edit", ctx.N(real_src), detail=f"{detail} (link makes it writable as {link})", certain=False))
    return effs


def _h_sort_like(cmd, args, ctx, stdin, detail):
    """sort -o F, uniq IN OUT, sponge F, split/csplit PREFIX: output files."""
    if cmd == "sort":
        outs = [args[k + 1] for k, a in enumerate(args) if a == "-o" and k + 1 < len(args)]
        outs += [a.split("=", 1)[1] for a in args if a.startswith("--output=")]
        outs += [a[2:] for a in args if a.startswith("-o") and len(a) > 2 and not a.startswith("--")]
        reads = _positionals(args, {"-o", "-k", "-t", "-T", "-S", "--parallel", "--key", "--field-separator"})
    elif cmd == "uniq":
        pos = _positionals(args, {"-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"})
        outs, reads = pos[1:2], pos[:1]
    elif cmd == "sponge":
        outs, reads = _positionals(args), []
    elif cmd == "split":
        pos = _positionals(args, {"-l", "-b", "-C", "-n", "-a", "-t", "--additional-suffix", "--lines", "--bytes", "--number", "--filter"})
        outs, reads = [(pos[1] if len(pos) > 1 else "x") + "*"], pos[:1]
    else:  # csplit
        pre = next((args[k + 1] for k, a in enumerate(args) if a in ("-f", "--prefix") and k + 1 < len(args)), "xx")
        pos = _positionals(args, {"-f", "--prefix", "-b", "--suffix-format", "-n", "--digits"})
        outs, reads = [pre + "*"], pos[:1]
    effs = [Effect("write", ctx.N(o), detail=detail) for o in outs if o not in _DEVNULLS and o != "-"]
    return effs or [Effect("read", ctx.N(reads[0]) if reads and reads[0] != "-" else ".", detail=detail)]


_COMPRESSORS = {"gzip": ".gz", "bzip2": ".bz2", "xz": ".xz", "lzma": ".lzma", "lz4": ".lz4", "zstd": ".zst", "compress": ".Z",
                "pigz": ".gz", "pbzip2": ".bz2", "lzip": ".lz", "brotli": ".br"}
_DECOMPRESSORS = {"gunzip": "gzip", "bunzip2": "bzip2", "unxz": "xz", "unlzma": "lzma", "unlz4": "lz4", "unzstd": "zstd",
                  "uncompress": "compress", "unpigz": "pigz"}


def _h_compress(cmd, args, ctx, stdin, detail):
    """gzip f -> f.gz and f is removed (unless -k/-c); gunzip f.gz -> f. zstd/lz4/brotli keep unless --rm."""
    tool = _DECOMPRESSORS.get(cmd, cmd)
    ext = _COMPRESSORS[tool]
    longs = {a for a in args if a.startswith("--")}
    shorts = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--") and len(a) > 1)
    if "l" in shorts or "t" in shorts or longs & {"--list", "--test", "--help", "--version"} or "h" in shorts or "V" in shorts:
        return [Effect("read", ".", detail=detail)]
    decompress = cmd in _DECOMPRESSORS or "d" in shorts or longs & {"--decompress", "--uncompress"}
    to_stdout = "c" in shorts or longs & {"--stdout", "--to-stdout"}
    keeps_by_default = tool in ("zstd", "lz4", "brotli")
    keep = to_stdout or "k" in shorts or "--keep" in longs or (keeps_by_default and "--rm" not in longs)
    out_flag = next((args[k + 1] for k, a in enumerate(args) if a == "-o" and k + 1 < len(args)), None) if tool in ("zstd", "lz4", "brotli") else None
    files = [f for f in _positionals(args, {"-o", "-S", "--suffix", "-T", "-b"}) if f != "-"]
    effs: list[Effect] = []
    for f in files:
        if out_flag:
            out = out_flag
        elif decompress:
            out = f[: -len(ext)] if f.endswith(ext) else f + ".out"
        else:
            out = f + ext
        if not to_stdout:
            effs.append(Effect("write", ctx.N(out), detail=detail))
        if not keep:
            effs.append(Effect("delete", ctx.N(f), detail=f"{detail} (removes the original)"))
    return effs or [Effect("run", detail, kind="command", detail=detail)]


def _h_zip(cmd, args, ctx, stdin, detail):
    pos = _positionals(args, {"-P", "--password", "-O", "--output-file", "-b", "-n", "-t", "-tt", "-x", "-i"})
    if not pos:
        return [Effect("run", detail, kind="command", detail=detail)]
    shorts = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
    archive, files = pos[0], pos[1:]
    effs = [Effect("write" if "d" not in shorts else "edit", ctx.N(archive), detail=detail)]
    if "m" in shorts or "--move" in args:
        effs += [Effect("delete", ctx.N(f), detail=f"{detail} (moves files into the archive)") for f in files]
    return effs


def _h_patch(cmd, args, ctx, stdin, detail):
    if "--dry-run" in args:
        return [Effect("run", detail, kind="command", detail=detail)]
    return [Effect("edit", UNKNOWN, detail=detail, certain=False)]


def _h_tar(cmd, args, ctx, stdin, detail):
    first = args[0] if args else ""
    flags = first if first and not first.startswith("-") else ""
    shorts = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--")) + flags
    create = "c" in shorts or "--create" in args or "r" in shorts or "u" in shorts
    extract = "x" in shorts or "--extract" in args or "--get" in args
    archive = None
    for k, a in enumerate(args):
        if a.startswith("--file="):
            archive = a.split("=", 1)[1]
        elif (a == "-f" or a == "--file" or (a.startswith("-") and not a.startswith("--") and a.endswith("f"))) and k + 1 < len(args):
            archive = args[k + 1]
    if archive is None and flags.endswith("f") and len(args) > 1:
        archive = args[1]
    cdir = next((args[k + 1] for k, a in enumerate(args) if a in ("-C", "--directory") and k + 1 < len(args)), None)
    pos = [p for p in _positionals(args[1:] if flags else args, {"-f", "--file", "-C", "--directory", "-T", "--exclude", "-X"}) if p != archive]
    effs: list[Effect] = []
    if create and archive and archive != "-":
        effs.append(Effect("write", ctx.N(archive), detail=detail))
    if extract:
        effs.append(Effect("write", ctx.N(cdir) if cdir else ctx.N("."), detail=detail, certain=False))
    if "--remove-files" in args:
        effs += [Effect("delete", ctx.N(p), detail=detail) for p in pos]
    return effs or [Effect("read", ctx.N(archive) if archive else ".", detail=detail)]


def _h_unzip(cmd, args, ctx, stdin, detail):
    if "-l" in args or "-t" in args:
        return [Effect("read", ".", detail=detail)]
    d = next((args[k + 1] for k, a in enumerate(args) if a == "-d" and k + 1 < len(args)), ".")
    return [Effect("write", ctx.N(d), detail=detail, certain=False)]


def _h_editor(cmd, args, ctx, stdin, detail):
    vals = {"-c", "-S", "-u", "-i", "--cmd", "-T", "-o", "-O", "-p"} if cmd in ("vim", "vi", "nvim", "ex", "view") else set()
    pos = [p for p in _positionals(args, vals) if not p.startswith("+")]
    if cmd == "view":
        return [Effect("read", ctx.N(p), detail=detail) for p in pos]
    return [Effect("edit", ctx.N(p), detail=detail) for p in pos] or [Effect("edit", UNKNOWN, detail=detail, certain=False)]


_FORMATTERS = {
    # cmd: (flags that make it rewrite, flags that make it read-only); None = rewrites by default
    "black": (None, {"--check", "--diff"}), "isort": (None, {"--check", "--check-only", "-c", "--diff"}),
    "autopep8": ({"-i", "--in-place"}, set()), "autoflake": ({"-i", "--in-place"}, set()), "yapf": ({"-i", "--in-place"}, set()),
    "prettier": ({"--write", "-w"}, set()), "eslint": ({"--fix"}, set()), "gofmt": ({"-w"}, set()), "rustfmt": (None, {"--check"}),
    "clang-format": ({"-i"}, set()), "pyupgrade": (None, set()), "dos2unix": (None, set()), "unix2dos": (None, set()),
    "stylelint": ({"--fix"}, set()), "rubocop": ({"-a", "-A", "--auto-correct", "--autocorrect", "--autocorrect-all"}, set()),
    "shfmt": ({"-w"}, set()), "terraform-fmt": (None, set()),
}


def _h_formatter(cmd, args, ctx, stdin, detail):
    if cmd == "ruff":
        sub = args[0] if args else ""
        rewrites = (sub == "format" and not {"--check", "--diff"} & set(args)) or "--fix" in args or ("--fix" not in args and sub not in ("check", "format") and "--fix" in args)
        pos = _paths_after_opts(args[1:] if sub in ("check", "format") else args)
    else:
        on, off = _FORMATTERS[cmd]
        rewrites = (on is None or bool(on & set(args))) and not (off & set(args))
        pos = _paths_after_opts(args)
    if not rewrites:
        return [Effect("read", ctx.N(pos[0]) if pos else ".", detail=detail)]
    targets = _everything_or(pos, ctx) or [EVERYTHING]
    return [Effect("edit", t, detail=detail, certain=t not in (EVERYTHING, UNKNOWN)) for t in _pathify(targets, ctx)]


# ----------------------------------------------------------------------------- shells / eval / source / xargs

def _stdin_script(stdin: Stdin, ctx: Ctx) -> str | None:
    """The literal program text a shell/interpreter would read from stdin, if statically known."""
    if stdin.kind == "text":
        return stdin.text
    if stdin.kind == "file":
        return ctx.read_file(stdin.path)
    return None


def _shell(cmd, args, ctx, stdin, detail):
    inline, script, i = None, None, 0
    positional = []
    reads_stdin = False
    while i < len(args):
        a = args[i]
        if a == "-c" or (a.startswith("-") and not a.startswith("--") and "c" in a[1:] and a[1:].isalpha()):
            inline = args[i + 1] if i + 1 < len(args) else ""
            positional = args[i + 2:]
            break
        if a in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            i += 2
            continue
        if a == "-s" or (a.startswith("-") and "s" in a[1:] and a[1:].isalpha()):
            reads_stdin = True
        if a == "--":
            script = args[i + 1] if i + 1 < len(args) else None
            positional = args[i + 2:]
            break
        if not a.startswith(("-", "+")):
            script = a
            positional = args[i + 1:]
            break
        i += 1
    sub = ctx.child(depth=ctx.depth + 1)
    for k, v in enumerate(positional, 1):
        sub.env[str(k)] = [v]
    if inline is not None:
        return _classify_text(inline, sub)
    if script is not None and not reads_stdin:
        return _script_file(script, ctx, detail, lang="sh")
    text = _stdin_script(stdin, ctx)
    if text is not None:
        return _uncertain(_classify_text(text, sub), "") if stdin.kind == "file" else _classify_text(text, sub)
    if stdin.kind != "none":
        return [_opaque(ctx.N(stdin.path) if stdin.path else UNKNOWN, f"{detail} (program read from {stdin.detail or 'a pipe'})")]
    return [Effect("run", detail, kind="command", detail=detail)]


def _h_eval(cmd, args, ctx, stdin, detail):
    if ctx.depth > MAX_DEPTH:
        return [_opaque(UNKNOWN, detail)]
    if cmd == "eval":
        text = " ".join(args)
        sub = replace(ctx, depth=ctx.depth + 1)    # eval shares the shell's state
        return _classify_text(text, sub)
    if not args:
        return [_opaque(UNKNOWN, detail)]
    return _script_file(args[0], ctx, detail, lang="sh", share=True)


def _h_trap(cmd, args, ctx, stdin, detail):
    if len(args) >= 2 and not args[0].startswith("-"):
        return _classify_text(args[0], ctx.child(depth=ctx.depth + 1))
    return []


def _h_watch(cmd, args, ctx, stdin, detail):
    rest = _positionals(args, {"-n", "--interval", "-d"})
    if not rest:
        return [Effect("run", detail, kind="command", detail=detail)]
    return _classify_text(" ".join(rest), ctx.child(depth=ctx.depth + 1)) if len(rest) == 1 else _classify_argv(rest, ctx.child(depth=ctx.depth + 1), Stdin())[0]


def _h_su(cmd, args, ctx, stdin, detail):
    for k, a in enumerate(args):
        if a in ("-c", "--command") and k + 1 < len(args):
            return _classify_text(args[k + 1], ctx.child(depth=ctx.depth + 1))
    return [Effect("run", detail, kind="command", detail=detail)]


def _run_per_item(inner: list[str], items: list[str] | None, repl: str | None, ctx: Ctx, detail: str) -> list[Effect]:
    """Run `inner` once per input item (xargs / parallel). items None = input not statically known."""
    if not inner or ctx.depth > MAX_DEPTH:
        return [_opaque(UNKNOWN, detail)]
    sub = ctx.child(depth=ctx.depth + 1)

    def argv_for(item: str) -> list[str]:
        if repl and any(repl in a for a in inner):
            return [a.replace(repl, item) for a in inner]
        return inner + [item]
    if items is not None:
        effs: list[Effect] = []
        for it in items[:64]:
            effs += _classify_argv(argv_for(it), sub, Stdin())[0]
        return effs or _classify_argv(inner, sub, Stdin())[0]
    effs, _ = _classify_argv(argv_for(UNKNOWN), sub, Stdin())
    if not effs:
        return [_opaque(UNKNOWN, detail)]
    for e in effs:
        e.certain = False
        e.detail = detail
    return effs


def _h_xargs(cmd, args, ctx, stdin, detail):
    vals = {"-n", "-I", "-d", "-P", "-L", "-l", "-s", "-a", "-E", "-e", "--max-args", "--max-procs", "--delimiter", "--arg-file"}
    i, repl, lines = 0, None, False
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a == "-I" and i + 1 < len(args):
            repl, lines = args[i + 1], True
        elif a.startswith("-I") and len(a) > 2:
            repl, lines = a[2:], True
        elif a == "-i" or a.startswith("--replace"):
            repl, lines = (a.split("=", 1)[1] if "=" in a else (a[2:] if a.startswith("-i") and len(a) > 2 else "{}")), True
        if a in vals:
            i += 1
        i += 1
    items = None
    if stdin.kind == "text":
        items = [l for l in stdin.text.splitlines() if l.strip()] if lines else stdin.text.split()
        if not repl:
            return _classify_argv(args[i:] + items, ctx.child(depth=ctx.depth + 1), Stdin())[0] if args[i:] else [_opaque(UNKNOWN, detail)]
    return _run_per_item(args[i:], items, repl, ctx, detail)


_PARALLEL_VALS = {"-j", "--jobs", "-N", "-P", "-S", "--sshlogin", "--colsep", "-C", "-d", "--delimiter", "-a", "--arg-file",
                  "--timeout", "--halt", "-L", "-n", "--max-args", "--tmpdir", "--joblog", "--results", "-I", "--workdir", "--wd"}


def _h_parallel(cmd, args, ctx, stdin, detail):
    """GNU parallel CMD ::: ARGS (or ARGS from stdin); without CMD the args are commands."""
    i, repl = 0, "{}"
    while i < len(args) and args[i].startswith("-") and args[i] not in (":::", "::::"):
        if args[i] == "-I" and i + 1 < len(args):
            repl = args[i + 1]
        if args[i] in _PARALLEL_VALS:
            i += 1
        i += 1
    rest = args[i:]
    if ":::" in rest or ":::+" in rest:
        k = next(j for j, a in enumerate(rest) if a in (":::", ":::+"))
        inner = rest[:k]
        items = [a for a in rest[k + 1:] if a not in (":::", ":::+", "::::")]
        if not inner:      # parallel ::: 'cmd1' 'cmd2'
            return [e for it in items for e in _classify_text(it, ctx.child(depth=ctx.depth + 1))]
        return _run_per_item(inner, items, repl, ctx, detail)
    if "::::" in rest:
        return _run_per_item(rest[:rest.index("::::")], None, repl, ctx, detail)
    items = [l for l in stdin.text.splitlines() if l.strip()] if stdin.kind == "text" else None
    if not rest:
        if items is not None:
            return [e for it in items for e in _classify_text(it, ctx.child(depth=ctx.depth + 1))]
        return [_opaque(UNKNOWN, detail)]
    return _run_per_item(rest, items, repl, ctx, detail)


# ----------------------------------------------------------------------------- stdout producers (for pipes)

def _printf(fmt: str, args: list[str]) -> str:
    fmt = _decode_escapes(fmt)
    if not re.search(r"%[-+ 0#]*\d*(?:\.\d+)?[sdbiq]", fmt):
        return fmt
    out, k = [], 0
    while True:            # the format is reused while arguments remain (bash semantics)
        res, j = [], 0
        for m in re.finditer(r"%[-+ 0#]*\d*(?:\.\d+)?([sdbiq%])", fmt):
            res.append(fmt[j:m.start()])
            if m.group(1) == "%":
                res.append("%")
            else:
                v = args[k] if k < len(args) else ""
                res.append(_decode_escapes(v) if m.group(1) == "b" else v)
                k += 1
            j = m.end()
        res.append(fmt[j:])
        out.append("".join(res))
        if k >= len(args):
            break
    return "".join(out)


def _h_echo(cmd, args, ctx, stdin, detail):
    effs = [Effect("read", ".", detail=detail)]
    if cmd == "echo":
        flags, rest = set(), list(args)
        while rest and re.fullmatch(r"-[neE]+", rest[0]):
            flags |= set(rest.pop(0)[1:])
        text = " ".join(rest)
        if "e" in flags:
            text = _decode_escapes(text)
        text += "" if "n" in flags else "\n"
    else:
        args2 = [a for a in args if a != "--"]
        text = _printf(args2[0], args2[1:]) if args2 else ""
    if "$" in text and re.search(r"\$\(…\)|\$[A-Za-z_{]", text):
        return effs, Stdin("unknown", detail=detail)
    return effs, Stdin("text", text=text, detail=detail)


def _h_base64(cmd, args, ctx, stdin, detail):
    decode = any(a in ("-d", "--decode", "-D") or (a.startswith("-") and not a.startswith("--") and "d" in a) for a in args)
    effs = [Effect("run", detail, kind="command", detail=detail)]
    files = _paths_after_opts(args)
    src = stdin.text if stdin.kind == "text" else (ctx.read_file(files[0]) if files else None)
    if not decode:
        return effs, Stdin("unknown")
    if src is None:
        return effs, Stdin("unknown", detail=detail)
    try:
        dec = base64.b64decode("".join(src.split()), validate=False).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return effs, Stdin("unknown")
    return effs, Stdin("text", text=dec, detail=f"base64-decoded literal ({detail})")


def _h_cat(cmd, args, ctx, stdin, detail):
    pos = _paths_after_opts(args)
    if not pos or pos == ["-"]:
        return [Effect("read", ".", detail=detail)], stdin
    return [Effect("read", ctx.N(pos[0]), detail=detail)], (Stdin("file", path=pos[0], detail=detail) if len(pos) == 1 else Stdin("unknown"))


def _h_rev_tr(cmd, args, ctx, stdin, detail):
    effs = [Effect("read", ".", detail=detail)]
    if stdin.kind != "text":
        return effs, Stdin("unknown")
    if cmd == "rev":
        return effs, Stdin("text", text="\n".join(l[::-1] for l in stdin.text.split("\n")), detail=detail)
    return effs, Stdin("unknown")


# ----------------------------------------------------------------------------- git

def _git_dir_of(start: str) -> str | None:
    d = start
    for _ in range(40):
        g = posixpath.join(d, ".git")
        if os.path.isdir(g):
            return g
        if os.path.isfile(g):           # worktree / submodule: "gitdir: <path>"
            try:
                m = re.match(r"gitdir:\s*(.+)", open(g).read().strip())
                if m:
                    p = m.group(1)
                    return p if posixpath.isabs(p) else posixpath.normpath(posixpath.join(d, p))
            except OSError:
                return None
        nd = posixpath.dirname(d)
        if nd == d:
            return None
        d = nd
    return None


def _git_current_branch(gcwd: str, ctx: Ctx) -> str | None:
    if not ctx.fs or not ctx.cwd_known:
        return None
    g = _git_dir_of(gcwd)
    if not g:
        return None
    try:
        head = open(posixpath.join(g, "HEAD")).read().strip()
    except OSError:
        return None
    m = re.match(r"ref:\s*refs/heads/(.+)", head)
    return m.group(1) if m else None


def _git_config_alias(name: str, gcwd: str, ctx: Ctx) -> str | None:
    if not ctx.fs:
        return None
    g = _git_dir_of(gcwd)
    cfgs = [posixpath.join(g, "config")] if g else []
    if any(ctx.stale(c) for c in cfgs):
        return None
    cfgs.append(os.path.expanduser("~/.gitconfig"))
    for cfg in cfgs:
        try:
            text = open(cfg).read()
        except OSError:
            continue
        sect = None
        for line in text.splitlines():
            s = line.strip()
            m = re.match(r"\[\s*([\w.-]+)(?:\s+\"[^\"]*\")?\s*\]", s)
            if m:
                sect = m.group(1).lower()
                continue
            if sect == "alias":
                m = re.match(r"([\w.-]+)\s*=\s*(.*)", s)
                if m and m.group(1) == name:
                    v = m.group(2).strip()
                    if len(v) >= 2 and v[0] == v[-1] == '"':
                        v = v[1:-1]
                    return v
    return None


def _h_git(cmd, args, ctx, stdin, detail):
    gctx = ctx
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a == "-C" and i + 1 < len(args):
            ap = gctx.abs(args[i + 1])
            gctx = ctx.child(cwd=ap) if ap else ctx.child(cwd_known=False)
            i += 2
            continue
        if a == "-c" and i + 1 < len(args):
            kv = args[i + 1]
            if "=" in kv and not kv.lower().startswith("alias."):
                hit = _git_exec_key_effect(kv, detail)
                if hit:
                    return hit
            if kv.lower().startswith("alias.") and "=" in kv:
                n, _, v = kv[6:].partition("=")
                gctx = gctx.child() if gctx is ctx else gctx
                gctx.git_aliases[n] = v
            i += 2
            continue
        if a in ("--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix", "--config-env", "--list-cmds"):
            i += 2
            continue
        i += 1
    sub = args[i] if i < len(args) else ""
    rest = args[i + 1:]
    if not sub:
        return [Effect("run", detail, kind="command", detail=detail)]
    if sub not in _GIT_KNOWN:
        alias = gctx.git_aliases.get(sub) or _git_config_alias(sub, gctx.cwd, gctx)
        if alias is not None and gctx.depth <= MAX_DEPTH:
            sub_ctx = gctx.child(depth=gctx.depth + 1)
            if alias.startswith("!"):
                return _uncertain(_classify_text(alias[1:] + " " + " ".join(shlex.quote(r) for r in rest), sub_ctx), f"git alias {sub}")
            try:
                expansion = shlex.split(alias)
            except ValueError:
                expansion = alias.split()
            effs = _h_git("git", expansion + rest, sub_ctx, stdin, detail)
            return [replace(e, detail=f"{detail} (git alias {sub} = {alias})") for e in effs]
        return [_opaque(UNKNOWN, f"{detail} (unknown git subcommand: alias or extension)")]
    if "--help" in rest or (rest[:1] == ["-h"] and len(rest) == 1):
        return [Effect("read", detail, kind="command", detail=detail)]          # `git push --help`: opens the manual
    if sub in ("commit-tree", "update-ref", "mktag") or (sub == "notes" and rest[:1] in (["add"], ["append"], ["edit"])):
        return [Effect("commit", "HEAD", kind="branch", detail=detail + " (plumbing that records history)")]
    return _GIT_SUBS.get(sub, _git_run)(sub, rest, gctx, detail)


_GIT_READONLY = {"log", "show", "diff", "status", "grep", "blame", "shortlog", "describe", "rev-parse", "ls-files", "ls-tree",
                 "cat-file", "whatchanged", "name-rev", "merge-base", "rev-list", "for-each-ref", "count-objects", "check-ignore",
                 "show-ref", "annotate", "diff-tree", "diff-index", "diff-files", "range-diff", "var", "version", "help"}


def _git_run(sub, rest, ctx, detail):
    if sub in _GIT_READONLY and not any(a.startswith(("--output", "--ext-diff", "--textconv")) or a == "-o" for a in rest):
        # read-only inspection: a "never touch prod" run rule must not fire on `git log --grep production` (O5)
        return [Effect("read", detail, kind="command", detail=detail)]
    return [Effect("run", detail, kind="command", detail=detail)]


def _git_push(sub, rest, ctx, detail):
    forced = dry = delete = mirror = allrefs = False
    vals = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
    pos, i = [], 0
    while i < len(rest):
        a = rest[i]
        if a == "--":
            pos += rest[i + 1:]
            break
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            if name.startswith("--force") :
                forced = True
            elif name == "--mirror":
                mirror = True
            elif name in ("--dry-run",):
                dry = True
            elif name == "--delete":
                delete = True
            elif name in ("--all", "--branches", "--tags"):
                allrefs = True
            elif name in vals and "=" not in a:
                i += 1
        elif a.startswith("-") and len(a) > 1:
            cl = a[1:]
            forced |= "f" in cl
            dry |= "n" in cl
            delete |= "d" in cl
            if cl[-1] == "o":
                i += 1
        else:
            pos.append(a)
        i += 1
    if dry:
        return [Effect("run", detail, kind="command", detail=detail)]
    remote = pos[0] if pos else "origin"
    specs = pos[1:]
    current = _git_current_branch(ctx.cwd, ctx)
    effs: list[Effect] = []
    if mirror or (allrefs and not specs):
        return [Effect("force_push" if (mirror or forced) else "push", f"{remote}/*", kind="branch",
                       detail=detail + (" (local remote)" if _remote_is_local(remote, ctx) else "") + " [src=*]")]
    if not specs:
        specs = [current or "HEAD"]
    for spec in specs:
        plus = spec.startswith("+")
        spec = spec.lstrip("+")
        src, colon, dst = spec.partition(":")
        if not colon:
            dst = src
        removal = bool(colon) and src == ""
        if dst in ("HEAD", "@") and current:
            dst = current
        dst = re.sub(r"^refs/(heads|tags)/", "", dst)
        tgt = f"{remote}/{dst}"
        rewrite = forced or plus or removal or delete
        srcref = src if (colon or src) and not removal else (current or "HEAD")
        effs.append(Effect("force_push" if rewrite else "push", tgt, kind="branch",
                           detail=detail + (" (local remote)" if _remote_is_local(remote, ctx) else "") + f" [src={srcref}]"))
        if delete or removal:
            effs.append(Effect("delete", tgt, kind="branch", detail=detail))
    return effs


def _remote_is_local(remote: str, ctx: Ctx) -> bool:
    """A remote that is a local path (or a remote name whose URL is one) involves no network I/O."""
    url = remote
    if not re.match(r"^(?:[a-z][a-z0-9+.-]*://|[\w.-]+@[\w.-]+:|/|\./|\.\./|~)", remote):
        out = _git_out(ctx, "config", "--get", f"remote.{remote}.url")
        if out is None:
            return False
        url = out.strip()
    return bool(re.match(r"^(?:file://|/|\./|\.\./|~)", url))


def _git_reset(sub, rest, ctx, detail):
    if any(a in ("--hard", "--merge", "--keep") for a in rest):
        return [Effect("discard", EVERYTHING, detail=detail)]
    if "--" in rest:
        return [Effect("run", detail, kind="command", detail=detail)]
    return [Effect("run", detail, kind="command", detail=detail)]


def _git_checkout(sub, rest, ctx, detail):
    rpos = _paths_after_opts(rest)
    if "--" in rest:
        k = rest.index("--")
        before = [a for a in rest[:k] if not a.startswith("-")]
        paths = _everything_or(rest[k + 1:], ctx)
        effs = [Effect("discard", p, detail=detail, certain=False) for p in _pathify(paths, ctx)] or [Effect("discard", UNKNOWN, detail=detail, certain=False)]
        if before:     # checkout <rev> -- paths: also writes the old content
            effs += [Effect("write", p, detail=detail, certain=False) for p in _pathify(paths, ctx)]
        return effs
    if any(a in ("-f", "--force") for a in rest) or (sub == "switch" and "--discard-changes" in rest):
        return [Effect("discard", EVERYTHING, detail=detail), Effect("run", detail, kind="command", detail=detail)]
    if any(a in ("-p", "--patch", "--orphan") for a in rest):
        return [Effect("run", detail, kind="command", detail=detail)]
    if sub != "checkout" or any(a in ("-b", "-B", "-c", "-C", "--create", "--force-create", "-t", "--track", "--detach") for a in rest):
        # switching to another commit rewrites every path that differs from HEAD (REVIEW-2 B7)
        vals = {"-b", "-B", "-c", "-C", "--create", "--force-create", "--conflict"}
        pos = _positionals(rest, vals)
        new_branch = any(a in vals for a in rest)
        start = pos[0] if pos and new_branch else (pos[0] if pos and not new_branch else None)
        if start is None:
            return [Effect("run", detail, kind="command", detail=detail)]
        return _tree_change(ctx, detail, [["diff", "--name-status", "HEAD", start]], fallback_run=True)
    rpos = _positionals(rest, {"--conflict", "--pathspec-from-file"})
    if len(rpos) >= 2:          # checkout <rev> <path>...: overwrites the paths with <rev>'s content
        paths = _pathify(_everything_or(rpos[1:], ctx), ctx)
        return [Effect("discard", p, detail=detail, certain=False) for p in paths] + \
               [Effect("write", p, detail=detail, certain=False) for p in paths]
    if rpos:
        p0 = rpos[0]
        ap = ctx.abs(p0)
        on_disk = ctx.fs and ctx.root is not None and os.path.isdir(ctx.root)
        is_path = p0 in (".", "*") or (os.path.exists(ap) if (on_disk and ap) else ("/" in p0 or "." in p0))
        if is_path and not (on_disk and _looks_like_ref(p0, ctx) and not os.path.exists(ap or "")):
            return [Effect("discard", p, detail=detail, certain=False) for p in _pathify(_everything_or(rpos, ctx), ctx)]
        return _tree_change(ctx, detail, [["diff", "--name-status", "HEAD", p0]], fallback_run=True)
    return [Effect("run", detail, kind="command", detail=detail)]


def _git_out(ctx: Ctx, *a: str) -> str | None:
    if not ctx.fs or not ctx.cwd_known or not ctx.cwd:
        return None
    try:
        p = subprocess.run(["git", "-C", ctx.cwd, *a], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def _tree_change(ctx: Ctx, detail: str, diffs: list[list[str]], fallback_run: bool = False) -> list[Effect]:
    """Paths a tree-rewriting git command (merge, rebase, cherry-pick, stash pop, checkout <branch>...) will change,
    computed from git itself before it runs (REVIEW-2 B7). Unknown -> an uncertain edit of an unknown target."""
    top = _git_out(ctx, "rev-parse", "--show-toplevel")
    effs: list[Effect] = []
    for d in diffs:
        out = _git_out(ctx, *d) if top else None
        if out is None:
            if fallback_run:
                return [Effect("run", detail, kind="command", detail=detail)]
            return [Effect("edit", UNKNOWN, detail=detail + " (rewrites the working tree)", certain=False)]
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            st, paths = parts[0], parts[1:]
            ab = [ctx.N(posixpath.join(top.strip(), x)) for x in paths]
            if st.startswith("R"):
                effs += [Effect("delete", ab[0], detail=detail), Effect("write", ab[-1], detail=detail)]
            elif st.startswith("D"):
                effs.append(Effect("delete", ab[0], detail=detail))
            else:
                effs.append(Effect("write", ab[0], detail=detail))
    return effs or [Effect("run", detail, kind="command", detail=detail)]


_CONTROL = {"--abort", "--quit", "--skip", "--continue", "--edit-todo", "--show-current-patch"}


def _git_plumbing_tree(sub, rest, ctx, detail):
    """read-tree -u / checkout-index -f|-a / worktree add: write the working tree from the index or a commit."""
    if (sub == "read-tree" and any(a in ("-u", "--reset") for a in rest)) or \
            (sub == "checkout-index" and any(a in ("-f", "--force", "-a", "--all") for a in rest)):
        return [Effect("edit", UNKNOWN, detail=detail + " (rewrites the working tree)", certain=False)]
    if sub == "worktree" and rest[:1] == ["remove"]:
        pos = _positionals(rest[1:], set())
        return [Effect("delete", ctx.N(p), detail=detail) for p in pos] or [Effect("delete", UNKNOWN, detail=detail, certain=False)]
    if sub == "worktree" and rest[:1] == ["add"]:
        pos = _positionals(rest[1:], {"-b", "-B", "--reason"})
        return [Effect("write", ctx.N(pos[0]) if pos else UNKNOWN, detail=detail)]
    return [Effect("run", detail, kind="command", detail=detail)]


def _git_merge_like(sub, rest, ctx, detail):
    if any(a in _CONTROL for a in rest):
        return [Effect("edit", UNKNOWN, detail=detail + " (rewrites the working tree)", certain=False)] if "--abort" in rest or "--skip" in rest \
            else [Effect("run", detail, kind="command", detail=detail)]
    vals = {"-m", "-s", "--strategy", "-X", "--strategy-option", "-F", "--file", "--onto", "--exec", "-x", "-S", "--gpg-sign",
            "--mainline", "--cleanup", "--into-name"}
    pos = _positionals(rest, vals)
    if sub in ("cherry-pick", "revert"):
        diffs = []
        for c in pos or ["HEAD"]:
            if ".." in c:
                a, _, b = c.partition("...") if "..." in c else c.partition("..")
                diffs.append(["diff", "--name-status", a or "HEAD", b or "HEAD"])
            else:
                diffs.append(["diff-tree", "-r", "--no-commit-id", "--name-status", "--root", c])
        effs = _tree_change(ctx, detail, diffs)
        if not any(a in ("-n", "--no-commit") for a in rest):
            effs.append(Effect("commit", "HEAD", kind="branch", detail=detail + f" ({sub} records a commit)"))
        return effs
    if sub in ("cherry-pick", "revert") and not any(a in ("-n", "--no-commit") for a in rest):
        pass  # handled below with the tree diff
    onto = next((rest[k + 1] for k, a in enumerate(rest) if a == "--onto" and k + 1 < len(rest)), None)
    up = onto or (pos[0] if pos else "@{u}")
    effs = _tree_change(ctx, detail, [["diff", "--name-status", f"HEAD...{up}"]])
    if sub == "rebase":
        effs.append(Effect("history_rewrite", "HEAD", kind="branch", detail=detail))
    elif sub == "merge" and not any(a in ("--no-commit", "--squash", "--ff-only") for a in rest):
        # a merge records a merge commit (a fast-forward does not, but that is decided at run time)
        effs.append(Effect("commit", "HEAD", kind="branch", detail=detail + " (records a merge commit unless it fast-forwards)",
                           certain="--no-ff" in rest))
    return effs


def _looks_like_ref(name: str, ctx: Ctx) -> bool:
    g = _git_dir_of(ctx.cwd) if ctx.cwd_known else None
    if not g:
        return False
    return any(os.path.exists(posixpath.join(g, d, name)) for d in ("refs/heads", "refs/tags", "refs/remotes"))


def _git_restore(sub, rest, ctx, detail):
    if ("--staged" in rest or "-S" in rest) and not ("--worktree" in rest or "-W" in rest):
        return [Effect("run", detail, kind="command", detail=detail)]
    paths = _everything_or(_positionals(rest, {"-s", "--source"}), ctx) or [UNKNOWN]
    return [Effect("discard", p, detail=detail) for p in _pathify(paths, ctx)]


_GUARD_UNTRACKED = (".claude/settings.json", ".claude/settings.local.json", ".claude/hooks", ".holdfast")


def _untracked_guard_files(ctx: Ctx, ignored: bool) -> list[str]:
    """Hook configuration / guard files that are untracked here (with ignored=True: untracked *or* ignored).
    `git stash -u|-a` and `git clean -f[x]` remove them from the working tree, which uninstalls the guard."""
    top = _git_out(ctx, "rev-parse", "--show-toplevel")
    if top is None:
        return []
    args = ["ls-files", "--others", "--directory"] + ([] if ignored else ["--exclude-standard"])
    out = _git_out(ctx, *args, "--", *_GUARD_UNTRACKED)
    if out is None:
        return []
    return [ctx.N(posixpath.join(top.strip(), l.strip().rstrip("/"))) for l in out.splitlines() if l.strip()]


def _git_clean(sub, rest, ctx, detail):
    if any("n" in a for a in rest if a.startswith("-") and not a.startswith("--")) or "--dry-run" in rest:
        return [Effect("read", ".", detail=detail)]
    if any("f" in a for a in rest if a.startswith("-") and not a.startswith("--")) or "--force" in rest:
        paths = _everything_or(_positionals(rest, {"-e", "--exclude"}), ctx) or ["."]
        effs = [Effect("delete", p, detail=detail) for p in _pathify(paths, ctx)]
        ignored = any(("x" in a or "X" in a) for a in rest if a.startswith("-") and not a.startswith("--"))
        effs += [Effect("delete", g, detail=detail + " (removes the untracked hook configuration)")
                 for g in _untracked_guard_files(ctx, ignored) if any(p in (".", EVERYTHING) or g.startswith(p.rstrip("/") + "/") or g == p
                                                                       for p in _pathify(paths, ctx))]
        return effs
    return [Effect("read", ".", detail=detail)]


def _git_branch(sub, rest, ctx, detail):
    rpos = _paths_after_opts(rest)
    if any(a in ("-D", "-d", "--delete") for a in rest):
        return [Effect("delete", f"{sub}/{p}", kind="branch", detail=detail) for p in rpos]
    return [Effect("run", detail, kind="command", detail=detail)]


def _git_stash(sub, rest, ctx, detail):
    rpos = _paths_after_opts(rest)
    if rpos[:1] in (["drop"], ["clear"]):
        return [Effect("discard", "stash", kind="branch", detail=detail)]
    if rpos[:1] in (["pop"], ["apply"]):
        ref = rpos[1] if len(rpos) > 1 else "stash@{0}"
        return _tree_change(ctx, detail, [["stash", "show", "--name-status", "--include-untracked", ref]])
    if rpos[:1] in ([], ["push"], ["save"]) and any(a in ("-u", "--include-untracked", "-a", "--all") or
                                                      (a.startswith("-") and not a.startswith("--") and ("u" in a or "a" in a)) for a in rest):
        allf = any(a in ("-a", "--all") or (a.startswith("-") and not a.startswith("--") and "a" in a) for a in rest)
        guard = _untracked_guard_files(ctx, allf)
        if guard:
            return [Effect("move", g, detail=detail + " (stashes away the untracked hook configuration)") for g in guard] + \
                   [Effect("run", detail, kind="command", detail=detail)]
    return [Effect("run", detail, kind="command", detail=detail)]


def _git_history(sub, rest, ctx, detail):
    if sub == "commit" and any(a in ("--dry-run", "--help", "-h") for a in rest):
        return [Effect("read", detail, kind="command", detail=detail)]
    if sub == "commit" and "--amend" not in rest:
        return [Effect("commit", "HEAD", kind="branch", detail=detail)]
    if sub == "commit":          # --amend records a new commit *and* rewrites the previous one
        return [Effect("commit", "HEAD", kind="branch", detail=detail), Effect("history_rewrite", "HEAD", kind="branch", detail=detail)]
    return [Effect("history_rewrite", "HEAD", kind="branch", detail=detail)]


def _git_rm(sub, rest, ctx, detail):
    rpos = _paths_after_opts(rest)
    if "--cached" in rest or "-n" in rest or "--dry-run" in rest:
        return [Effect("run", detail, kind="command", detail=detail)]
    return [Effect("delete", ctx.N(p), detail=detail) for p in rpos] or [Effect("delete", UNKNOWN, detail=detail, certain=False)]


def _git_mv(sub, rest, ctx, detail):
    rpos = _paths_after_opts(rest)
    if len(rpos) < 2:
        return [Effect("move", UNKNOWN, detail=detail, certain=False)]
    return [Effect("move", ctx.N(p), detail=detail) for p in rpos[:-1]] + [Effect("write", ctx.N(rpos[-1]), detail=detail)]


def _git_apply(sub, rest, ctx, detail):
    if any(a in ("--check", "--stat", "--numstat", "--summary") for a in rest):
        return [Effect("run", detail, kind="command", detail=detail)]
    effs = [Effect("edit", UNKNOWN, detail=detail, certain=False)]
    if sub == "am":
        effs.append(Effect("commit", "HEAD", kind="branch", detail=detail))
    return effs


def _git_network(sub, rest, ctx, detail):
    rpos = _paths_after_opts(rest)
    if sub == "lfs" and rpos[:1] == ["push"]:
        return _git_push("push", rest[rest.index("push") + 1:], ctx, detail) + [Effect("network", "git lfs", kind="url", detail=detail)]
    if sub == "submodule" and rpos[:1] == ["foreach"]:
        k = rest.index("foreach")
        inner = [a for a in rest[k + 1:] if a not in ("--recursive", "-q", "--quiet")]
        sub_ctx = ctx.child(depth=ctx.depth + 1, cwd_known=False)
        effs = _classify_text(inner[0], sub_ctx) if len(inner) == 1 else _classify_argv(inner, sub_ctx, Stdin())[0]
        return _uncertain(effs, "git submodule foreach") or [Effect("run", detail, kind="command", detail=detail)]
    if sub == "remote" and rpos[:1] not in (["update"], ["prune"], ["show"]):
        return [Effect("run", detail, kind="command", detail=detail)]
    if sub == "submodule" and rpos[:1] not in (["update"], ["add"], ["sync"], ["foreach"]):
        return [Effect("run", detail, kind="command", detail=detail)]
    if sub == "lfs" and rpos[:1] not in (["fetch"], ["pull"], ["push"], ["install"]):
        return [Effect("run", detail, kind="command", detail=detail)]
    url = next((p for p in rpos if "://" in p or re.match(r"[\w.-]+@[\w.-]+:", p)), None)
    rname = url or (rpos[0] if rpos and sub in ("fetch", "pull", "ls-remote", "clone") else ("origin" if sub in ("fetch", "pull") else None))
    local = rname is not None and _remote_is_local(rname, ctx)
    effs = [] if local else [Effect("network", url or ("git " + sub), kind="url", detail=detail)]
    if sub == "clone":
        target = None
        if url and len(rpos) > rpos.index(url) + 1:
            target = rpos[rpos.index(url) + 1]
        elif url:
            target = re.sub(r"\.git$", "", url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1])
        effs.append(Effect("write", ctx.N(target) if target else UNKNOWN, detail=detail))
    elif sub == "submodule" and rpos[:1] == ["add"] and len(rpos) >= 3:
        effs.append(Effect("write", ctx.N(rpos[2]), detail=detail))
    elif sub == "pull":
        # merges/rebases remote content that is not known until it is fetched (REVIEW-2 B7)
        effs.append(Effect("edit", UNKNOWN, detail=detail + " (merges fetched changes into the working tree)", certain=False))
    return effs


# git config keys whose value git later executes as a program (hooks, filters, drivers, pagers, editors...)
_GIT_EXEC_KEYS = re.compile(r"^(?:core\.(?:hookspath|fsmonitor|sshcommand|editor|pager|askpass|gitproxy)|sequence\.editor"
                            r"|filter\..+\.(?:clean|smudge|process)|diff\..+\.(?:textconv|command)|merge\..+\.driver"
                            r"|credential\.helper|gpg\.program|gpg\..+\.program|pager\..+|alias\..+|include\.path|includeif\..+\.path"
                            r"|uploadpack\.packobjectshook|remote\..+\.(?:uploadpack|receivepack)|protocol\..+\.allow)$", re.I)


def _git_exec_key_effect(key: str, detail: str) -> list[Effect]:
    if key.lower().startswith("alias.") and not key.split("=", 1)[-1].lstrip().startswith("!") and "=" in key:
        return []
    if _GIT_EXEC_KEYS.match(key.split("=", 1)[0].strip()):
        return [Effect("edit", ".git/config", detail=f"{detail} (sets {key.split('=', 1)[0]}: git will execute its value)")]
    return []


def _git_config(sub, rest, ctx, detail):
    rpos = _paths_after_opts(rest)
    getters = {"--get", "--get-all", "--get-regexp", "-l", "--list", "--unset", "--unset-all", "--show-origin",
               "--get-urlmatch", "--remove-section", "--rename-section"}
    if rpos and not any(a in getters for a in rest) and (len(rpos) >= 2 or "--add" in rest or "--replace-all" in rest):
        key = rpos[0]
        if not (key.lower().startswith("alias.") and not " ".join(rpos[1:]).lstrip().startswith("!")):
            effs = _git_exec_key_effect(key + "=", detail)
            if effs:
                return effs
    if rpos and rpos[0].lower().startswith("alias.") and len(rpos) >= 2:
        ctx.git_aliases[rpos[0][6:]] = " ".join(rpos[1:])
    return [Effect("run", detail, kind="command", detail=detail)]


_GIT_SUBS = {
    "push": _git_push, "reset": _git_reset, "checkout": _git_checkout, "switch": _git_checkout, "restore": _git_restore,
    "clean": _git_clean, "branch": _git_branch, "tag": _git_branch, "stash": _git_stash, "rebase": _git_merge_like,
    "merge": _git_merge_like, "cherry-pick": _git_merge_like, "revert": _git_merge_like,
    "read-tree": _git_plumbing_tree, "checkout-index": _git_plumbing_tree, "worktree": _git_plumbing_tree,
    "commit": _git_history, "filter-branch": _git_history, "filter-repo": _git_history, "rm": _git_rm, "mv": _git_mv,
    "apply": _git_apply, "am": _git_apply, "fetch": _git_network, "pull": _git_network, "clone": _git_network,
    "ls-remote": _git_network, "remote": _git_network, "submodule": _git_network, "lfs": _git_network,
    "send-email": _git_network, "request-pull": _git_run, "config": _git_config, "send-pack": _git_push,
}


# ----------------------------------------------------------------------------- network

_LOOPBACK = re.compile(r"^(?:[a-z+]+://)?(?:[^@/]*@)?(localhost|127(?:\.\d+){3}|\[?::1\]?|0\.0\.0\.0)(?::\d+)?(?:/|$)", re.I)


def _h_curl(cmd, args, ctx, stdin, detail):
    if any(a in ("--version", "-V", "--help", "-h", "--manual", "-M") for a in args) and not any("://" in a for a in args):
        return [Effect("run", detail, kind="command", detail=detail)]
    if cmd in ("curl", "wget"):
        vals = ({"-o", "--output", "-d", "--data", "-H", "--header", "-X", "--request", "-u", "--user", "-A", "-e", "-F", "--form",
                 "-T", "--upload-file", "-x", "--proxy", "-m", "--max-time", "-w", "--write-out", "-b", "-c", "--cookie", "-K",
                 "--retry", "--connect-timeout", "-r", "-C", "--data-raw", "--data-binary", "--cacert", "--cert", "--key", "-E"}
                if cmd == "curl" else {"-O", "--output-document", "-o", "--output-file", "-a", "-P", "--directory-prefix", "-U",
                                       "--user-agent", "--header", "-t", "--tries", "-T", "--timeout", "-e", "-i", "--input-file"})
    else:
        vals = {"-p", "-P", "-i", "-l", "-o", "-F", "-J", "-L", "-R", "-D", "-b", "-c", "-E", "-e", "-m", "-O", "-Q", "-S", "-W", "-w", "-q"}
    outputs: list[str] = []
    if cmd in ("curl", "wget"):
        for flag in (("-o", "--output") if cmd == "curl" else ("-O", "--output-document")):
            if flag in args and args.index(flag) + 1 < len(args):
                outputs.append(args[args.index(flag) + 1])
    pos = _positionals(args, vals)
    url = next((p for p in pos if "://" in p), None) or next((p for p in pos if p not in outputs and ("." in p or "@" in p or ":" in p)), UNKNOWN)
    if cmd in ("ssh", "scp", "sftp", "rsync") and url != UNKNOWN:
        m = _REMOTE_PATH.match(url) if cmd != "ssh" else None
        url = m.group(1) if m else (url.split("@", 1)[-1].split(":", 1)[0] if cmd in ("ssh", "sftp") else url)
        if cmd == "scp":
            remote = next((p for p in pos if _REMOTE_PATH.match(p) and not p.startswith("/")), None)
            url = _REMOTE_PATH.match(remote).group(1) if remote else UNKNOWN
    effs: list[Effect] = []
    if url.startswith("file://"):
        effs.append(Effect("read", ctx.N(url[7:]), detail=detail))
    elif _LOOPBACK.match(url):
        effs.append(Effect("run", detail, kind="command", detail=detail + " (loopback)"))
    else:
        effs.append(Effect("network", url, kind="url", detail=detail))
    effs += [Effect("write", ctx.N(o), detail=detail) for o in outputs if o != "-"]
    if cmd == "scp" and pos and not _REMOTE_PATH.match(pos[-1]):
        effs.append(Effect("write", ctx.N(pos[-1]), detail=detail))
    out = Stdin("remote", detail=detail) if cmd in ("curl", "wget") and not [o for o in outputs if o != "-"] else Stdin("unknown")
    return effs, out


_SSH_VALS = {"-p", "-i", "-l", "-o", "-F", "-J", "-L", "-R", "-D", "-b", "-c", "-E", "-e", "-m", "-O", "-Q", "-S", "-W", "-w", "-B", "-I"}


def _h_ssh(cmd, args, ctx, stdin, detail):
    """ssh host [cmd]: network to host; a remote command's effects are reported with kind 'remote' and
    target 'host:path' (they are not local file effects, but rules on actions/patterns can still see them)."""
    pos = _positionals(args, _SSH_VALS)
    if not pos:
        return [Effect("run", detail, kind="command", detail=detail)]
    host = pos[0].split("@", 1)[-1]
    effs = [Effect("network", host, kind="url", detail=detail)]
    remote = " ".join(pos[1:])
    if remote and ctx.depth <= MAX_DEPTH:
        rctx = Ctx(cwd="/\0remote", root="/\0remote", depth=ctx.depth + 1, fs=False, cwd_known=False)
        for e in _classify_text(remote, rctx):
            if e.action in ("read", "run"):
                continue
            t = "*" if e.target in (UNKNOWN, EVERYTHING) else e.target
            effs.append(Effect(e.action, f"{host}:{t}", kind="remote", detail=f"{detail} [remote: {e.detail}]", certain=e.certain))
    return effs


def _h_net_simple(cmd, args, ctx, stdin, detail):
    pos = _paths_after_opts(args)
    host = next((p for p in pos if "." in p or ":" in p), pos[0] if pos else UNKNOWN)
    if _LOOPBACK.match(host):
        return [Effect("run", detail, kind="command", detail=detail)]
    return [Effect("network", host, kind="url", detail=detail)]


def _h_gh(cmd, args, ctx, stdin, detail):
    sub = args[0] if args else ""
    if sub in ("", "help", "--help", "--version", "version", "completion", "alias", "config"):
        return [Effect("run", detail, kind="command", detail=detail)]
    effs = [Effect("network", "github.com", kind="url", detail=detail)]
    if sub == "pr" and args[1:2] == ["merge"]:
        effs.append(Effect("push", "origin/*", kind="branch", detail=detail, certain=False))
    if sub == "api":
        method = next((args[k + 1].upper() for k, a in enumerate(args) if a in ("-X", "--method") and k + 1 < len(args)), None)
        method = method or next((a.split("=", 1)[1].upper() for a in args if a.startswith("--method=")), None)
        if method is None and any(a in ("-f", "-F", "--field", "--raw-field", "--input") or a.startswith(("--field=", "--raw-field="))
                                  for a in args):
            method = "POST"
        if method and method not in ("GET", "HEAD"):
            # a REST mutation can merge PRs, move refs or write files on the remote (REVIEW-2 B8)
            effs.append(Effect("push", "origin/*", kind="branch", detail=detail + f" ({method} via the GitHub API)", certain=False))
    if sub == "repo" and args[1:2] == ["clone"] and len(args) > 2:
        effs.append(Effect("write", ctx.N(args[3] if len(args) > 3 else args[2].rsplit("/", 1)[-1]), detail=detail))
    return effs


# ----------------------------------------------------------------------------- packages

_PIP_VALS = {"-i", "--index-url", "--extra-index-url", "-f", "--find-links", "-t", "--target", "--prefix", "--root",
             "--src", "-d", "--dest", "--platform", "--python-version", "--implementation", "--abi", "--log", "--cache-dir",
             "--trusted-host", "--progress-bar", "--proxy", "--timeout", "--retries", "--python"}
_REGISTRY = {"pip": "pypi.org", "bundle": "rubygems.org", "snap": "snapcraft.io", "uv": "pypi.org", "pipx": "pypi.org", "poetry": "pypi.org", "pipenv": "pypi.org",
             "npm": "registry.npmjs.org", "yarn": "registry.npmjs.org", "pnpm": "registry.npmjs.org", "bun": "registry.npmjs.org"}


def _install_effects(pkgs: list[str], registry: str, detail: str, network: bool = True, certain: bool = True) -> list[Effect]:
    effs = [Effect("install", " ".join(pkgs) if pkgs else UNKNOWN, kind="package", detail=detail, certain=certain)]
    if network:
        effs.append(Effect("network", registry, kind="url", detail=detail))
    return effs


def _pip(args: list[str], who: str, ctx: Ctx, detail: str) -> list[Effect]:
    """`pip ARGS` (also python -m pip, uv pip)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("--python", "--log", "--proxy", "--cache-dir"):
            i += 1
        i += 1
    verb = args[i] if i < len(args) else ""
    rest = args[i + 1:]
    if not verb or verb in ("list", "show", "freeze", "check", "help", "config", "cache", "debug", "inspect", "hash", "completion", "index") \
            or any(a in ("--help", "-h", "--version", "-V") for a in args):
        return [Effect("run", detail, kind="command", detail=detail)]
    pkgs = _positionals(rest, _PIP_VALS - {"-r", "--requirement", "-e", "--editable", "-c", "--constraint"})
    offline = "--no-index" in rest
    if verb in ("install", "sync", "compile") or (verb == "wheel"):
        return _install_effects(pkgs, _REGISTRY.get(who, who), detail, network=not offline)
    if verb == "download":
        dest = next((rest[k + 1] for k, a in enumerate(rest) if a in ("-d", "--dest") and k + 1 < len(rest)), None)
        return [Effect("network", _REGISTRY.get(who, who), kind="url", detail=detail)] + ([Effect("write", ctx.N(dest), detail=detail)] if dest else [])
    if verb == "uninstall":
        return [Effect("delete", "package/" + " ".join(pkgs), kind="package", detail=detail)]
    return [Effect("run", detail, kind="command", detail=detail)]


def _h_pip(cmd, args, ctx, stdin, detail):
    return _pip(args, "pip", ctx, detail)


def _fetch_and_run(pkg: str, rest: list[str], registry: str, ctx: Ctx, detail: str) -> list[Effect]:
    """npx / pipx run / uvx: download a package and execute it."""
    if not pkg:
        return [_opaque(UNKNOWN, detail)]
    name = re.sub(r"@[^/@]*$", "", pkg) if not pkg.startswith("@") or pkg.count("@") > 1 else pkg
    if posixpath.basename(name) in TEST_TOOLS:
        return [Effect("test", detail, kind="command", detail=detail)]
    effs = [Effect("install", name, kind="package", detail=detail + " (fetches the package unless already installed)", certain=False),
            Effect("network", registry, kind="url", detail=detail)]
    inner, _ = _classify_argv([posixpath.basename(name)] + rest, ctx.child(depth=ctx.depth + 1), Stdin())
    precise = [e for e in inner if e.action not in ("run", "read")]
    effs += precise if precise else [_opaque(name, detail, kind="package")]
    return effs


def _h_npx(cmd, args, ctx, stdin, detail):
    vals = {"-p", "--package", "-c", "--call"}
    i, pkg = 0, None
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-p", "--package") and i + 1 < len(args):
            pkg = args[i + 1]
        if args[i] in ("-c", "--call") and i + 1 < len(args):
            return [Effect("network", "registry.npmjs.org", kind="url", detail=detail)] + _classify_text(args[i + 1], ctx.child(depth=ctx.depth + 1))
        if args[i] in vals:
            i += 1
        i += 1
    rest = args[i:]
    if not rest:
        return [_opaque(UNKNOWN, detail)]
    return _fetch_and_run(pkg or rest[0], rest[1:], "registry.npmjs.org", ctx, detail)


def _package_script(mgr: str, script: str, extra: list[str], ctx: Ctx, detail: str, test: bool = False) -> list[Effect]:
    """npm/yarn/pnpm/bun run <script>: read package.json, classify the script body (and pre/post hooks)."""
    text = ctx.read_file("package.json")
    body_effs: list[Effect] = []
    found = False
    if text:
        try:
            scripts = (json.loads(text) or {}).get("scripts", {}) or {}
        except (ValueError, AttributeError):
            scripts = {}
        for name in ("pre" + script, script, "post" + script):
            body = scripts.get(name)
            if isinstance(body, str) and ctx.depth <= MAX_DEPTH:
                found = True
                body_effs += _classify_text(body + (" " + " ".join(shlex.quote(a) for a in extra) if name == script and extra else ""),
                                            ctx.child(depth=ctx.depth + 1))
    for e in body_effs:
        e.detail = f"{detail} -> {e.detail}"
    if test:
        return [Effect("test", detail, kind="command", detail=detail)] + [e for e in body_effs if e.action not in ("run", "read", "test", "exec_opaque")]
    if not found:
        return [_opaque(UNKNOWN, f"{detail} (script body not visible)")]
    # the body is literal shell, as visible as `bash -c '...'`: report what it does (anything it runs
    # that is itself opaque, e.g. `node build.js`, is already an exec_opaque in body_effs)
    return body_effs or [Effect("run", detail, kind="command", detail=detail)]


def _h_node_pm(cmd, args, ctx, stdin, detail):
    if cmd in ("pnpx", "bunx"):
        return _h_npx(cmd, args, ctx, stdin, detail)
    flags = [a for a in args if a.startswith("-")]
    pos = [a for a in args if not a.startswith("-")]
    verb = pos[0] if pos else ""
    rest = pos[1:]
    reg = "registry.npmjs.org"
    if not verb:
        if cmd in ("yarn", "bun") and not flags:
            return _install_effects([], reg, detail)
        return [Effect("run", detail, kind="command", detail=detail)]
    offline = "--offline" in flags
    if verb in ("install", "i", "add", "ci", "update", "up", "upgrade", "in", "isntall", "dedupe", "link") or (verb == "audit" and rest[:1] == ["fix"]):
        pkgs = [] if verb == "audit" else rest
        return _install_effects(pkgs, reg, detail, network=not offline)
    if verb in ("uninstall", "remove", "rm", "un", "r", "unlink"):
        return [Effect("delete", "package/" + " ".join(rest), kind="package", detail=detail)]
    if verb in ("publish",):
        return [Effect("deploy", detail, kind="command", detail=detail), Effect("network", reg, kind="url", detail=detail)]
    if verb in ("test", "t", "tst"):
        return _package_script(cmd, "test", rest, ctx, detail, test=True)
    if verb in ("run", "run-script", "rum", "urn"):
        if not rest:
            return [Effect("run", detail, kind="command", detail=detail)]
        if rest[0] == "test":
            return _package_script(cmd, "test", rest[1:], ctx, detail, test=True)
        return _package_script(cmd, rest[0], rest[1:], ctx, detail)
    if verb in ("start", "stop", "restart", "build", "dev", "serve", "lint", "prepare"):
        if cmd == "npm" and verb not in ("start", "stop", "restart"):
            return [Effect("run", detail, kind="command", detail=detail)]
        return _package_script(cmd, verb, rest, ctx, detail)
    if verb in ("exec", "x", "dlx"):
        return _h_npx("npx", args[args.index(verb) + 1:], ctx, stdin, detail)
    if verb == "pkg" and rest[:1] in (["set"], ["delete"]):
        return [Effect("edit", ctx.N("package.json"), detail=detail)]
    if verb in ("outdated", "view", "info", "show", "search", "audit", "login", "whoami", "ping", "doctor"):
        return [Effect("network", reg, kind="url", detail=detail)]
    if verb in ("ls", "list", "help", "config", "version", "-v", "root", "prefix", "bin", "why", "explain", "cache"):
        return [Effect("run", detail, kind="command", detail=detail)]
    if cmd in ("yarn", "pnpm", "bun"):
        if cmd == "bun" and (verb.endswith((".js", ".ts", ".mjs", ".cjs", ".tsx", ".jsx")) or "/" in verb):
            return _script_file(verb, ctx, detail, lang="node")
        return _package_script(cmd, verb, rest, ctx, detail)      # `yarn clean` runs the `clean` script
    return [Effect("run", detail, kind="command", detail=detail)]


def _h_pkg_other(cmd, args, ctx, stdin, detail):
    pos = [a for a in args if not a.startswith("-")]
    verb = pos[0] if pos else ""
    rest = _positionals(args[args.index(verb) + 1:] if verb in args else [], {"-p", "--manifest-path", "--version", "--git", "--branch", "--tag"})
    if any(a in ("--help", "-h", "--version", "-V") for a in args) and verb in ("", "help"):
        return [Effect("run", detail, kind="command", detail=detail)]
    if cmd == "pacman":
        if any(re.fullmatch(r"-S[a-zA-Z]*", a) and not re.search(r"[sigcp]", a[2:]) or a in ("-U", "--sync", "--upgrade") for a in args):
            return _install_effects([p for p in pos], "archlinux.org", detail)
        if any(re.fullmatch(r"-R[a-zA-Z]*", a) or a == "--remove" for a in args):
            return [Effect("delete", "package/" + " ".join(pos), kind="package", detail=detail)]
        return [Effect("run", detail, kind="command", detail=detail)]
    if cmd == "nix-env" and any(a in ("-i", "--install", "-iA") or re.fullmatch(r"-i[A-Za-z]*", a) for a in args):
        return _install_effects(pos, "nixos.org", detail)
    if cmd == "rustup" and (pos[:2] in (["toolchain", "install"], ["component", "add"], ["target", "add"]) or pos[:1] in (["install"], ["update"])):
        return _install_effects(pos[2:] or pos[1:], "rust-lang.org", detail)
    if cmd == "corepack" and pos[:1] in (["enable"], ["prepare"], ["install"], ["use"]):
        return _install_effects(pos[1:] or ["corepack"], "registry.npmjs.org", detail)
    if cmd == "mvn" and any(a.startswith(("dependency:get", "dependency:copy", "install:install-file")) for a in args):
        return _install_effects([a for a in args if a.startswith("-Dartifact=")] or ["maven"], "repo.maven.apache.org", detail)
    if cmd == "dotnet" and pos[:1] == ["add"] and "package" in pos:
        return _install_effects(pos[pos.index("package") + 1:], "nuget.org", detail)
    installs = {"pip": ("install",), "pip3": ("install",), "bundle": ("add", "install", "update"), "snap": ("install", "refresh"),
                "gem": ("install",), "cargo": ("add", "install"), "gem": ("install",), "apt": ("install", "upgrade", "full-upgrade", "dist-upgrade"),
                "apt-get": ("install", "upgrade", "dist-upgrade"), "brew": ("install", "upgrade", "reinstall", "tap"), "poetry": ("add", "install", "update", "lock"),
                "conda": ("install", "update", "create"), "go": ("get", "install"), "pipenv": ("install", "update", "sync"), "composer": ("require", "install", "update"),
                "dnf": ("install", "upgrade"), "yum": ("install", "update"), "apk": ("add",), "pacman": ("-S",), "dotnet": ("add",), "mamba": ("install",)}
    nets = {"apt": ("update",), "apt-get": ("update",), "brew": ("update", "fetch"), "cargo": ("fetch", "search", "publish", "update"),
            "go": ("mod",), "conda": ("search",), "gem": ("fetch", "push"), "dnf": ("makecache",), "apk": ("update",)}
    removes = ("uninstall", "remove", "purge", "autoremove", "rm")
    if verb in installs.get(cmd, ()):
        return _install_effects(rest, _REGISTRY.get(cmd, "*"), detail, network="--offline" not in args)
    if verb in nets.get(cmd, ()):
        if cmd == "go" and rest[:1] not in (["download"], ["tidy"], ["vendor"]):
            return [Effect("run", detail, kind="command", detail=detail)]
        return [Effect("network", _REGISTRY.get(cmd, cmd), kind="url", detail=detail)]
    if verb in removes:
        return [Effect("delete", "package/" + " ".join(rest), kind="package", detail=detail)]
    if verb in ("publish",):
        return [Effect("deploy", detail, kind="command", detail=detail), Effect("network", cmd, kind="url", detail=detail)]
    if cmd == "go" and verb == "test" or cmd == "cargo" and verb == "test":
        return [Effect("test", detail, kind="command", detail=detail)]
    return [Effect("run", detail, kind="command", detail=detail)]


def _h_uv(cmd, args, ctx, stdin, detail):
    if cmd == "uvx":
        return _uv_tool_run(args, ctx, detail)
    pos = [a for a in args if not a.startswith("-")]
    verb = pos[0] if pos else ""
    if verb == "pip":
        return _pip(args[args.index("pip") + 1:], "uv", ctx, detail)
    if verb in ("add", "sync", "lock") or (verb == "tool" and pos[1:2] in (["install"], ["upgrade"])):
        pkgs = pos[2:] if verb == "tool" else pos[1:]
        return _install_effects(pkgs if verb != "lock" else [], "pypi.org", detail, network="--offline" not in args)
    if verb == "tool" and pos[1:2] == ["run"]:
        return _uv_tool_run(args[args.index("run") + 1:], ctx, detail)
    if verb == "remove":
        return [Effect("delete", "package/" + " ".join(pos[1:]), kind="package", detail=detail)]
    if verb == "run" and ctx.depth <= MAX_DEPTH:
        inner = args[args.index("run") + 1:]
        while inner and inner[0].startswith("-"):
            flag = inner.pop(0)
            if flag in ("--with", "--python", "-p", "--group", "--extra", "--env-file", "--directory", "--project", "--package", "--with-requirements") and inner:
                inner.pop(0)
        if inner:
            return _classify_argv(inner, ctx.child(depth=ctx.depth + 1), stdin)[0]
    if verb in ("publish",):
        return [Effect("deploy", detail, kind="command", detail=detail)]
    return [Effect("run", detail, kind="command", detail=detail)]


def _uv_tool_run(args, ctx, detail):
    i, pkg = 0, None
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "--from" and i + 1 < len(args):
            pkg = args[i + 1]
        if args[i] in ("--from", "--with", "--python", "-p"):
            i += 1
        i += 1
    rest = args[i:]
    if not rest:
        return [_opaque(UNKNOWN, detail)]
    return _fetch_and_run(pkg or rest[0], rest[1:], "pypi.org", ctx, detail)


def _h_pipx(cmd, args, ctx, stdin, detail):
    pos = [a for a in args if not a.startswith("-")]
    verb = pos[0] if pos else ""
    if verb in ("install", "inject", "upgrade", "reinstall", "upgrade-all", "install-all"):
        return _install_effects(pos[1:], "pypi.org", detail)
    if verb == "run":
        return _uv_tool_run(args[args.index("run") + 1:], ctx, detail)
    if verb in ("uninstall", "uninstall-all"):
        return [Effect("delete", "package/" + " ".join(pos[1:]), kind="package", detail=detail)]
    return [Effect("run", detail, kind="command", detail=detail)]


# ----------------------------------------------------------------------------- make

def _make_rules(text: str) -> tuple[dict, str | None]:
    """Parse a Makefile into {target: (prereqs, recipe_lines)} and the default target."""
    rules: dict = {}
    default = None
    cur: list[str] | None = None
    lines = text.replace("\\\n", " ").splitlines()
    for line in lines:
        if line.startswith("\t") and cur is not None:
            for t in cur:
                rules[t][1].append(line[1:])
            continue
        m = re.match(r"^([^\s:=#][^:=#]*?)\s*::?(?!=)\s*([^=;#]*?)\s*(?:;\s*(.*))?$", line)
        if m and not line.startswith((" ", "\t")):
            targets = m.group(1).split()
            cur = targets
            for t in targets:
                rules.setdefault(t, ([], []))
                rules[t][0].extend(m.group(2).split())
                if m.group(3):
                    rules[t][1].append(m.group(3))
                if default is None and not t.startswith(".") and "%" not in t:
                    default = t
            continue
        if line.strip() and not line.startswith("\t"):
            cur = None
    return rules, default


def _h_make(cmd, args, ctx, stdin, detail):
    if any(a in ("-n", "--dry-run", "--just-print", "--recon", "-q", "--question", "-p", "--print-data-base") or
           re.fullmatch(r"-[a-zA-Z]*n[a-zA-Z]*", a) for a in args if a.startswith("-") and not a.startswith("--file")):
        return [Effect("read", detail, kind="command", detail=detail)]       # prints recipes, runs nothing
    if any(a in ("-n", "--dry-run", "--just-print", "--recon", "-q", "--question", "-p", "--print-data-base", "--help", "-h", "-v", "--version") for a in args):
        return [Effect("run", detail, kind="command", detail=detail)]
    mctx = ctx
    mfile = None
    targets, i = [], 0
    while i < len(args):
        a = args[i]
        if a in ("-C", "--directory") and i + 1 < len(args):
            ap = ctx.abs(args[i + 1])
            mctx = ctx.child(cwd=ap) if ap else ctx.child(cwd_known=False)
            i += 2
            continue
        if a in ("-f", "--file", "--makefile") and i + 1 < len(args):
            mfile = args[i + 1]
            i += 2
            continue
        if a in ("-j", "-l", "-o", "-W", "-I") and i + 1 < len(args) and not args[i + 1].startswith("-"):
            i += 2
            continue
        if not a.startswith("-") and "=" not in a:
            targets.append(a)
        i += 1
    if targets[:1] == ["test"] or targets[:1] == ["check"]:
        body = _make_recipe_effects(mctx, mfile, targets[:1], detail) or []
        return [Effect("test", detail, kind="command", detail=detail)] + [e for e in body if e.action not in ("run", "read", "test", "exec_opaque")]
    body = _make_recipe_effects(mctx, mfile, targets, detail)
    if body is None:
        return [_opaque(UNKNOWN, detail + " (recipe not visible)")]
    return body or [Effect("run", detail, kind="command", detail=detail)]


def _make_recipe_effects(ctx: Ctx, mfile: str | None, targets: list[str], detail: str) -> list[Effect] | None:
    """Effects of the recipes make would run (targets + prerequisites). None if the recipe is not visible
    (no Makefile, unknown target, pattern/implicit rules, include directives)."""
    text = None
    for name in ([mfile] if mfile else ["GNUmakefile", "makefile", "Makefile"]):
        text = ctx.read_file(name)
        if text is not None:
            break
    if text is None or ctx.depth > MAX_DEPTH:
        return None
    rules, default = _make_rules(text)
    todo = list(targets or ([default] if default else []))
    if not todo or any(t not in rules for t in todo) or re.search(r"^\s*-?include\s", text, re.M):
        return None
    seen: set = set()
    effs: list[Effect] = []
    sub = ctx.child(depth=ctx.depth + 1)
    while todo and len(seen) < 50:
        t = todo.pop(0)
        if t in seen or t not in rules:
            continue
        seen.add(t)
        prereqs, recipe = rules[t]
        if any(p not in rules for p in prereqs) and any("%" in r for r in rules):
            return None                     # a pattern rule may build that prerequisite: recipe not visible
        todo = [p for p in prereqs if p in rules] + todo
        for line in recipe:
            line = line.lstrip("@-+ \t").replace("$(MAKE)", "make").replace("${MAKE}", "make").replace("$$", "\x00")
            line = re.sub(r"\$[({][^)}]*[)}]|\$[@<^*?%]", "$MAKEVAR", line).replace("\x00", "$")
            for e in _classify_text(line, sub):
                e.detail = f"{detail} -> {e.detail}"
                effs.append(e)
    return effs


# ----------------------------------------------------------------------------- infra / deploy

_INFRA = {"terraform", "tofu", "kubectl", "helm", "aws", "gcloud", "gsutil", "az", "docker", "docker-compose", "podman", "fly", "flyctl",
          "vercel", "heroku", "pulumi", "ansible-playbook", "ansible", "psql", "mysql", "dropdb", "redis-cli", "mongo", "mongosh",
          "netlify", "serverless", "sls", "eksctl", "kustomize", "skaffold", "argocd", "oc", "doctl", "wrangler",
          "sqlite3", "duckdb", "clickhouse-client", "cqlsh", "pg_restore", "mongorestore"}


def _h_infra(cmd, args, ctx, stdin, detail):
    joined = detail
    if cmd in ("docker", "podman") and args[:1] == ["rm"]:
        return [Effect("run", joined, kind="command", detail=joined)]
    destructive = any(a in ("destroy", "delete", "rm", "down", "drop", "prune", "uninstall", "FLUSHALL", "flushall", "rb", "terminate-instances") for a in args) \
        or cmd == "dropdb" or re.search(r"\b(drop|truncate|delete\s+from)\b", joined, re.I) is not None
    deploys = ("apply", "up", "run", "push", "deploy", "exec", "create", "scale", "upgrade", "install", "rollout", "patch", "replace",
               "set", "edit", "sync", "release", "publish", "cp", "mv", "put", "update", "import", "start", "restart")
    act = "deploy_destroy" if destructive else ("deploy" if any(a in deploys for a in args) else "run")
    effs = [Effect(act, joined, kind="command", detail=joined)]
    if cmd in ("aws", "gcloud", "gsutil", "az", "doctl", "heroku", "fly", "flyctl", "vercel", "netlify", "wrangler") or \
            (cmd in ("docker", "podman") and args[:1] in (["pull"], ["push"], ["login"], ["search"])):
        effs.append(Effect("network", cmd, kind="url", detail=joined))
    return effs


def _h_twine(cmd, args, ctx, stdin, detail):
    if args[:1] == ["upload"]:
        return [Effect("deploy", detail, kind="command", detail=detail), Effect("network", "upload.pypi.org", kind="url", detail=detail)]
    return [Effect("run", detail, kind="command", detail=detail)]


# ----------------------------------------------------------------------------- interpreters / embedded code

def _local_module(mod: str, ctx: Ctx, extra_dirs: tuple = ()) -> bool:
    if not ctx.fs or not mod or mod in sys.stdlib_module_names:
        return False
    top = mod.split(".")[0]
    dirs = [d for d in ((ctx.cwd if ctx.cwd_known else None), ctx.root, (posixpath.join(ctx.root, "src") if ctx.root else None)) + tuple(extra_dirs) if d]
    for d in dirs:
        if os.path.isfile(posixpath.join(d, top + ".py")) or os.path.isdir(posixpath.join(d, top)):
            return True
    return False


MAX_IMPORT_FILES = 64
_PY_CHECKERS = {"mypy", "pyright", "pylint", "flake8", "pyflakes", "pycodestyle", "pydocstyle", "bandit", "vulture", "radon",
                "coverage", "pip_audit", "safety", "interrogate", "xenon"}


def _module_files(mod: str, level: int, names: list[str], ctx: "Ctx", script_dir: str | None) -> list[str] | None:
    """Source files a (project) import executes: the module (or package __init__) and imported submodules."""
    parts = [p for p in mod.split(".") if p]
    if level > 0:
        if not script_dir:
            return None
        base = script_dir
        for _ in range(level - 1):
            base = posixpath.dirname(base)
        bases = [base]
    else:
        bases = [d for d in ((ctx.cwd if ctx.cwd_known else None), ctx.root,
                             (posixpath.join(ctx.root, "src") if ctx.root else None), script_dir) if d]
    for b in dict.fromkeys(bases):
        p = posixpath.join(b, *parts) if parts else b
        out = []
        if os.path.isfile(p + ".py"):
            out.append(p + ".py")
        elif os.path.isdir(p):
            if os.path.isfile(posixpath.join(p, "__init__.py")):
                out.append(posixpath.join(p, "__init__.py"))
            out += [posixpath.join(p, n + ".py") for n in names if n != "*" and os.path.isfile(posixpath.join(p, n + ".py"))]
            if not out and not parts:
                continue
        if out:
            # also the parent packages' __init__ files, which run first
            for k in range(1, len(parts)):
                ini = posixpath.join(b, *parts[:k], "__init__.py")
                if os.path.isfile(ini):
                    out.insert(0, ini)
            return out
        if os.path.isdir(p):
            return []                                  # namespace package, nothing executes
    return None


_PY_DELETE = {"os.remove", "os.unlink", "os.rmdir", "os.removedirs", "shutil.rmtree", "os.truncate", "send2trash.send2trash"}
_PY_MOVE = {"os.rename", "os.replace", "shutil.move", "os.renames"}
_PY_COPY = {"shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree", "shutil.copyfileobj"}
_PY_SHELL = {"os.system", "os.popen", "subprocess.getoutput", "subprocess.getstatusoutput", "asyncio.create_subprocess_shell", "commands.getoutput"}
_PY_EXEC = {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "subprocess.Popen", "pty.spawn"}
_PY_ARGV_STAR = {"asyncio.create_subprocess_exec", "os.execlp", "os.execl", "os.spawnlp", "os.spawnl"}
_PY_DYNAMIC = {"eval", "exec", "compile", "__import__", "importlib.import_module", "runpy.run_path", "runpy.run_module", "ctypes.CDLL",
               "ctypes.cdll.LoadLibrary", "os.execv", "os.execve", "os.execvp", "os.execvpe", "os.spawnv", "os.spawnve", "os.spawnvp",
               "os.posix_spawn", "os.posix_spawnp", "os.fork"}
_PY_NET_SAFE = {"socket.gethostname", "socket.htons", "socket.ntohs", "socket.htonl", "socket.ntohl", "socket.inet_aton",
                "socket.inet_ntoa", "socket.inet_pton", "socket.inet_ntop"}       # local, no I/O
_PY_NET = ("requests.", "httpx.", "aiohttp.", "urllib.request.", "urllib3.", "http.client.", "socket.", "ftplib.", "smtplib.",
           "paramiko.", "websocket.", "websockets.", "urllib2.", "pycurl.", "telnetlib.", "xmlrpc.client.", "grpc.")
_PATH_DELETE_METHODS = {"unlink", "rmdir", "rmtree"}
_PATH_WRITE_METHODS = {"write_text", "write_bytes", "touch", "mkdir", "symlink_to", "hardlink_to"}
_PATH_MOVE_METHODS = {"rename", "replace"}
_PIP_MAIN = {"pip.main", "pip._internal.main", "pip._internal.cli.main.main"}


class _PyScan:
    """AST-based effects of python source. Conservative where dynamic; precise on literals."""

    def __init__(self, code: str, ctx: Ctx, detail: str, script_dir: str | None = None):
        self.ctx, self.detail, self.script_dir = ctx, detail, script_dir
        with warnings.catch_warnings():          # odd escapes in scanned code must not print to the hook's stderr
            warnings.simplefilter("ignore")
            self.tree = ast.parse(code)
        self.aliases: dict[str, str] = {}
        self.consts: dict[str, str] = {}
        self.pathvars: dict[str, str | None] = {}
        self.effects: list[Effect] = []

    # -- helpers
    def const(self, node) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
            a, b = self.const(node.left), self.const(node.right)
            if a is not None and b is not None:
                return a + b if isinstance(node.op, ast.Add) else posixpath.join(a, b)
            if isinstance(node.op, ast.Div):
                base = self.path_of(node.left)
                if base is not None and b is not None:
                    return posixpath.join(base, b)
        if isinstance(node, ast.JoinedStr):
            parts = [self.const(v) if not isinstance(v, ast.FormattedValue) else self.const(v.value) for v in node.values]
            return "".join(parts) if all(p is not None for p in parts) else None
        if isinstance(node, ast.Name):
            return self.consts.get(node.id)
        if isinstance(node, ast.Call) and self.qual(node.func) in ("os.path.join", "posixpath.join") and node.args:
            parts = [self.const(a) for a in node.args]
            return posixpath.join(*parts) if all(p is not None for p in parts) else None
        if isinstance(node, ast.Call) and self.qual(node.func) in ("str",) and node.args:
            return self.const(node.args[0]) or self.path_of(node.args[0])
        return None

    def path_of(self, node) -> str | None:
        """Literal path of a pathlib.Path(...) expression, or of a name bound to one."""
        if isinstance(node, ast.Call) and (self.qual(node.func) or "").rsplit(".", 1)[-1] in ("Path", "PurePath", "PosixPath", "WindowsPath"):
            if not node.args:
                return "."
            parts = [self.const(a) for a in node.args]
            return posixpath.join(*parts) if all(p is not None for p in parts) else None
        if isinstance(node, ast.Name):
            return self.pathvars.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self.const(node)
        return None

    def qual(self, node) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self.qual(node.value)
            return f"{base}.{node.attr}" if base else None
        if isinstance(node, ast.Call):
            fq = self.qual(node.func)
            if fq == "getattr" and len(node.args) >= 2:
                base, name = self.qual(node.args[0]), self.const(node.args[1])
                return f"{base}.{name}" if base and name else "?dynamic"
            if fq in ("__import__", "importlib.import_module") and node.args and self.const(node.args[0]):
                return self.const(node.args[0])
            return f"{fq}()" if fq else None
        if isinstance(node, ast.Subscript):
            return "?dynamic"
        return None

    def N(self, p: str | None) -> str:
        return UNKNOWN if p is None else self.ctx.N(p)

    def add(self, action: str, target: str, kind: str = "path", note: str = "") -> None:
        self.effects.append(Effect(action, target, kind=kind if target != UNKNOWN or kind != "path" else "opaque",
                                   detail=self.detail + (f" [{note}]" if note else ""), certain=False))

    def argv_of(self, node) -> list[str] | None:
        if isinstance(node, (ast.List, ast.Tuple)):
            vals = [self.const(e) for e in node.elts]
            return [v if v is not None else "$UNKNOWN" for v in vals]
        return None

    def run_shell(self, s: str, note: str) -> None:
        if self.ctx.depth > MAX_DEPTH:
            self.add("exec_opaque", UNKNOWN, note=note)
            return
        for e in _classify_text(s, self.ctx.child(depth=self.ctx.depth + 1)):
            e.certain = False
            e.detail = f"{self.detail} [{note}: {e.detail}]"
            self.effects.append(e)

    def run_argv(self, argv: list[str], note: str) -> None:
        if not argv or "$" in argv[0]:
            self.add("exec_opaque", UNKNOWN, note=note)
            return
        effs, _ = _classify_argv(argv, self.ctx.child(depth=self.ctx.depth + 1), Stdin())
        for e in effs:
            e.certain = False
            e.detail = f"{self.detail} [{note}: {e.detail}]"
            self.effects.append(e)

    def follow_import(self, mod: str, level: int, names: list[str]) -> None:
        top = mod.split(".")[0] if mod else ""
        if level == 0 and top == "holdfast":
            self.add("edit", ".git/holdfast", note="imports Holdfast's own state/hook modules")
            return
        if level > 0 and not self.script_dir:
            return                                    # `python -c 'from . import x'` raises before running anything
        if level == 0 and not _local_module(mod, self.ctx, (self.script_dir,) if self.script_dir else ()):
            return                                    # stdlib / installed third-party: not project code
        files = _module_files(mod, level, names, self.ctx, self.script_dir)
        if files is None:
            self.add("exec_opaque", UNKNOWN, note=f"imports project module {'.' * level}{mod} (source not found)")
            return
        for f in files:
            if f in self.ctx.seen:
                continue
            if len(self.ctx.seen) >= MAX_IMPORT_FILES:
                self.add("exec_opaque", UNKNOWN, note=f"imports project module {mod} (import graph too large to scan)")
                return
            text = self.ctx.read_file(f)
            sub = self.ctx.child(seen=self.ctx.seen | {f})
            effs = _python_effects(text, sub, self.detail, script_dir=posixpath.dirname(f)) if text is not None else None
            # the child ctx's seen set grows inside the recursion; carry it back so siblings are not rescanned
            self.ctx.seen = sub.seen | self.ctx.seen
            if effs is None:
                self.add("exec_opaque", UNKNOWN, note=f"imports project module {mod} ({f} unreadable or not python)")
                continue
            for e in effs:
                if e.action in ("run", "read"):
                    continue
                e.certain = False
                e.detail = f"{e.detail} [via import of {posixpath.relpath(f, self.ctx.root) if self.ctx.root else f}]"
                self.effects.append(e)

    # -- passes
    def collect(self) -> None:
        for node in ast.walk(self.tree):
            # Importing (project) modules and calling their functions is a plain run: only concrete
            # filesystem / subprocess / network / dynamic-exec calls in this code produce effects.
            # (A dotted module name is not a path; reporting it as an opaque target false-blocked real runs.)
            # Project modules are not opaque either: their source is resolved and scanned (REVIEW-2 N6), so
            # `python -c 'import src.fixup'` reports what src/fixup.py does.
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
                    self.follow_import(a.name, 0, [])
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if node.module:
                        self.aliases[a.asname or a.name] = f"{node.module}.{a.name}"
                self.follow_import(node.module or "", node.level, [a.name for a in node.names])
        for _ in range(2):   # two passes so that chained constants resolve
            for node in ast.walk(self.tree):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                        and isinstance(node.value, (ast.Name, ast.Attribute)):
                    # f = open / o = os / r = os.remove: the name is an alias of a callable or module
                    q = self.qual(node.value)
                    if q and q != node.targets[0].id:
                        self.aliases[node.targets[0].id] = re.sub(r"^(?:__builtins__|builtins)\.", "", q)
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    v = self.const(node.value)
                    if v is not None:
                        self.consts[node.targets[0].id] = v
                    pv = self.path_of(node.value) if isinstance(node.value, ast.Call) else None
                    if pv is not None or (isinstance(node.value, ast.Call) and (self.qual(node.value.func) or "").endswith("Path")):
                        self.pathvars[node.targets[0].id] = pv

    def scan(self) -> list[Effect]:
        self.collect()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                self.call(node)
        return self.effects

    def call(self, node: ast.Call) -> None:
        q = re.sub(r"^(?:__builtins__|builtins)\.", "", self.qual(node.func) or "")
        args = node.args
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        a0 = args[0] if args else None
        if q == "?dynamic" or "?dynamic" in q:
            self.add("exec_opaque", UNKNOWN, note="dynamic attribute call")
            return
        if q in _PY_DELETE:
            self.add("delete", self.N(self.const(a0) or self.path_of(a0)) if a0 is not None else UNKNOWN, note=q)
            return
        if q in _PY_MOVE and len(args) >= 2:
            self.add("move", self.N(self.const(args[0]) or self.path_of(args[0])), note=q)
            self.add("write", self.N(self.const(args[1]) or self.path_of(args[1])), note=q)
            return
        if q in ("os.link", "os.symlink") and len(args) >= 2:
            self.add("write", self.N(self.const(args[1]) or self.path_of(args[1])), note=q)
            self.add("edit", self.N(self.const(args[0]) or self.path_of(args[0])), note=f"{q}: the source becomes writable via the link")
            return
        if q in _PY_COPY and len(args) >= 2:
            self.add("write", self.N(self.const(args[1]) or self.path_of(args[1])), note=q)
            return
        if q in ("open", "io.open", "codecs.open", "builtins.open", "os.open"):
            mode = self.const(args[1]) if len(args) > 1 else (self.const(kw["mode"]) if "mode" in kw else ("r" if q != "os.open" else None))
            target = a0 if a0 is not None else kw.get("file")
            if mode is None or any(c in mode for c in "wax+"):
                self.add("write", self.N(self.const(target) or self.path_of(target)) if target is not None else UNKNOWN, note=q)
            return
        if isinstance(node.func, ast.Attribute):
            meth = node.func.attr
            base = node.func.value
            bq = self.qual(base) or ""
            pathy = self.path_of(base)
            is_path = pathy is not None or bq.rsplit(".", 1)[-1].rstrip("()") in ("Path", "PurePath", "PosixPath") or \
                (isinstance(base, ast.Name) and base.id in self.pathvars)
            if meth in _PATH_DELETE_METHODS and (is_path or not bq.startswith(("os", "shutil"))):
                self.add("delete", self.N(pathy), note=f".{meth}()")
                return
            if meth in _PATH_WRITE_METHODS and (is_path or meth in ("write_text", "write_bytes")):
                self.add("write", self.N(pathy), note=f".{meth}()")
                return
            if meth in _PATH_MOVE_METHODS and is_path:
                self.add("move", self.N(pathy), note=f".{meth}()")
                if args:
                    self.add("write", self.N(self.const(args[0]) or self.path_of(args[0])), note=f".{meth}()")
                return
            if meth == "open" and is_path:
                mode = self.const(args[0]) if args else (self.const(kw["mode"]) if "mode" in kw else "r")
                if mode is None or any(c in mode for c in "wax+"):
                    self.add("write", self.N(pathy), note=".open()")
                return
        if q in _PY_SHELL:
            s = self.const(a0) if a0 is not None else None
            self.run_shell(s, q) if s is not None else self.add("exec_opaque", UNKNOWN, note=f"{q} with a non-literal command")
            return
        if q in _PY_EXEC:
            shell = "shell" in kw and isinstance(kw["shell"], ast.Constant) and kw["shell"].value is True
            target = a0 if a0 is not None else kw.get("args")
            sub_ctx_cwd = self.const(kw["cwd"]) if "cwd" in kw else None
            saved = self.ctx
            if sub_ctx_cwd is not None:
                ap = self.ctx.abs(sub_ctx_cwd)
                self.ctx = self.ctx.child(cwd=ap) if ap else self.ctx.child(cwd_known=False)
            try:
                argv = self.argv_of(target) if target is not None else None
                s = self.const(target) if target is not None else None
                if argv is not None:
                    self.run_shell(" ".join(argv), q) if shell and len(argv) == 1 else self.run_argv(argv, q)
                elif s is not None:
                    self.run_shell(s, q) if shell else self.run_argv(shlex.split(s) if " " not in s.strip() else [s], q)
                else:
                    self.add("exec_opaque", UNKNOWN, note=f"{q} with a non-literal command")
            finally:
                self.ctx = saved
            return
        if q in _PY_ARGV_STAR:
            vals = [self.const(a) for a in args]
            argv = [v if v is not None else "$UNKNOWN" for v in vals]
            if q.startswith("os.exec") or q.startswith("os.spawn"):
                argv = argv[1:] if q.startswith("os.exec") else argv[2:]
            self.run_argv(argv, q)
            return
        if q in _PIP_MAIN:
            argv = self.argv_of(a0) if a0 is not None else None
            self.run_argv(["pip"] + argv, q) if argv is not None else self.add("install", UNKNOWN, kind="package", note=q)
            return
        if q in ("exec", "eval") and a0 is not None and self.const(a0) is not None and self.ctx.depth <= MAX_DEPTH:
            try:
                inner = _PyScan(self.const(a0), self.ctx.child(depth=self.ctx.depth + 1), self.detail, self.script_dir).scan()
                self.effects += inner
                return
            except SyntaxError:
                pass
        if q in _PY_DYNAMIC or q.startswith("ctypes.") or (q in ("getattr",) and len(args) >= 2 and self.const(args[1]) is None):
            self.add("exec_opaque", UNKNOWN, note=f"dynamic: {q}")
            return
        if (q.startswith(_PY_NET) and q not in _PY_NET_SAFE) or q in ("urllib.urlopen", "urlopen", "urlretrieve") or q.endswith((".urlopen", ".urlretrieve")):
            url = self.const(a0) if a0 is not None else None
            self.add("network", url or UNKNOWN, kind="url", note=q)
            if q.endswith("urlretrieve") and len(args) > 1:
                self.add("write", self.N(self.const(args[1])), note=q)
            return


def _python_effects(code: str, ctx: Ctx, detail: str, script_dir: str | None = None) -> list[Effect] | None:
    try:
        return _PyScan(code, ctx, detail, script_dir).scan()
    except (SyntaxError, ValueError, RecursionError):
        return None


_STR = r"""(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")"""
_LIT_ARG = re.compile(r"\s*\(?\s*(?:my\s+)?" + _STR)
_LIT_LIST = re.compile(_STR)


def _lit_args_after(code: str, pos: int) -> list[str] | None:
    """Literal string arguments of the call starting at code[pos] (`(` or a space), flattening [..] lists."""
    m = re.match(r"\s*\(?", code[pos:])
    i = pos + m.end()
    depth, out = 0, []
    while i < len(code):
        c = code[i]
        if c in "'\"":
            sm = _LIT_LIST.match(code, i)
            if not sm:
                return out or None
            out.append(sm.group(1) if sm.group(1) is not None else sm.group(2))
            i = sm.end()
            continue
        if c in "[":
            depth += 1
        elif c == "]":
            depth -= 1
        elif c in ");\n" and depth <= 0:
            break
        elif c not in " ,\t" and depth >= 0:
            if c.isalnum() or c in "$@{":
                out.append("$UNKNOWN")
                while i < len(code) and code[i] not in ",)];\n":
                    i += 1
                continue
        i += 1
    return out


def _looks_like_path(s: str) -> bool:
    return bool(s) and " " not in s and ("/" in s or re.search(r"\.\w{1,6}$", s) is not None) and "://" not in s


def _regex_code_effects(code: str, ctx: Ctx, detail: str) -> list[Effect]:
    """Regex scan of embedded code (node/perl/ruby/php/awk, or unparseable python)."""
    effs: list[Effect] = []
    sub = ctx.child(depth=ctx.depth + 1)

    def add(action: str, target: str, kind: str = "path", note: str = "") -> None:
        effs.append(Effect(action, target, kind=kind if target != UNKNOWN else "opaque", detail=f"{detail} [{note}]" if note else detail, certain=False))

    def run_cmd(argv: list[str], note: str) -> None:
        if ctx.depth > MAX_DEPTH or not argv:
            add("exec_opaque", UNKNOWN, note=note)
            return
        inner = _classify_text(argv[0], sub) if len(argv) == 1 else _classify_argv(argv, sub, Stdin())[0]
        if not inner:
            return
        for e in inner:
            e.certain = False
            e.detail = f"{detail} [{note}: {e.detail}]"
        effs.extend(inner)

    # 1. process execution with literal commands
    for m in re.finditer(r"\b(system|exec|execSync|execFileSync|spawnSync|spawn|execFile|shell_exec|passthru|popen|proc_open|Open3\.\w+|IO\.popen|"
                         r"subprocess\.\w+|os\.system|os\.popen|Kernel\.system|%x|qx)\s*(?=[(\[{'\"\s])", code):
        if m.group(1) in ("exec",) and re.match(r"\s*\(\s*(?:compile|open)", code[m.end():]):
            continue
        rest = code[m.end():]
        if m.group(1) in ("%x", "qx"):
            mm = re.match(r"\s*[({\[/](.*?)[)}\]/]", rest, re.S)
            run_cmd([mm.group(1)] if mm else [], m.group(1))
            continue
        lits = _lit_args_after(code, m.end())
        if lits and "$UNKNOWN" not in lits[:1]:
            run_cmd(lits if len(lits) > 1 else lits[:1], m.group(1))
        else:
            add("exec_opaque", UNKNOWN, note=f"{m.group(1)} with a non-literal command")
    for m in re.finditer(r"`([^`\n]+)`", code):            # ruby/perl backticks (shell strings)
        run_cmd([m.group(1)], "backticks")
    for m in re.finditer(r"\"([^\"]+)\"\s*\|\s*getline", code):  # awk "cmd" | getline
        run_cmd([m.group(1)], "awk getline")
    # 2. deletes / writes with literal targets
    for rx, act in INTERP_DANGER:
        if act not in ("delete", "write"):
            continue
        for m in rx.finditer(code):
            tgt = None
            if m.groups() and m.lastindex and act == "write" and m.group(1) and not m.group(1).startswith(("write", "Path", "append", "File", "IO")):
                tgt = m.group(1)
            else:
                lm = _LIT_ARG.match(code, m.end())
                if lm:
                    tgt = lm.group(1) if lm.group(1) is not None else lm.group(2)
            if tgt is None:
                # perl / ruby open with a mode: open(F, '>', 'x'), open(F, ">x"), File.open('x', 'w')
                lits = _lit_args_after(code, m.end()) or []
                tgt = next((l for l in lits if _looks_like_path(l)), None)
            add(act, ctx.N(tgt) if tgt else UNKNOWN, note=m.group(0).strip("("))
    for m in re.finditer(r"\bopen\s*\(\s*(?:my\s+)?[\$\w]+\s*,\s*" + _STR + r"(?:\s*,\s*" + _STR + r")?", code):   # perl open
        mode = m.group(1) if m.group(1) is not None else m.group(2)
        fname = m.group(3) if m.group(3) is not None else m.group(4)
        if mode and mode.lstrip().startswith((">", "+<", "+>")):
            path = fname if fname is not None else mode.lstrip("+<> ")
            add("write", ctx.N(path) if path else UNKNOWN, note="perl open")
    for m in re.finditer(r"\bprint[f]?\b[^;>|}]*?(>>?)\s*" + _STR, code):        # awk print > "file"
        path = m.group(2) if m.group(2) is not None else m.group(3)
        add("write", ctx.N(path), note="awk redirect")
    # 3. network
    for rx, act in INTERP_DANGER:
        if act == "network" and rx.search(code):
            url = re.search(r"['\"]((?:https?|ftp)://[^'\"]+)['\"]", code)
            add("network", url.group(1) if url else UNKNOWN, kind="url", note="network API")
            break
    # 4. a dangerous call with an unresolved target: the code's literal paths are the best guess
    if any(e.action in ("delete", "write") and e.target == UNKNOWN for e in effs):
        for lit in {(a or b) for a, b in _LIT_LIST.findall(code)}:
            if _looks_like_path(lit):
                for act in {e.action for e in effs if e.action in ("delete", "write") and e.target == UNKNOWN}:
                    add(act, ctx.N(lit), note="literal path in code")
    # 5. de-duplicate
    seen, out = set(), []
    for e in effs:
        k = (e.action, e.target)
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def _code_effects(lang: str, code: str, ctx: Ctx, detail: str, script_dir: str | None = None) -> list[Effect]:
    """Effects of a program in `lang`. Python is parsed; others are regex-scanned. Never empty."""
    if lang == "python":
        effs = _python_effects(code, ctx, detail, script_dir)
        if effs is not None:
            return effs or [Effect("run", detail, kind="command", detail=detail)]
    effs = _regex_code_effects(code, ctx, detail)
    if lang == "awk":
        return effs
    return effs or [_opaque(UNKNOWN, detail)]


_LANG_OF = {"python": "python", "node": "node", "nodejs": "node", "deno": "node", "bun": "node", "perl": "perl", "ruby": "ruby", "php": "php"}
_INLINE_FLAGS = {"python": ("-c",), "node": ("-e", "--eval", "-p", "--print"), "perl": ("-e", "-E"), "ruby": ("-e",), "php": ("-r",)}
_VALUE_FLAGS = {"python": {"-m", "-W", "-X", "-Q"}, "node": {"-r", "--require", "--import", "--loader", "--input-type", "--env-file", "-C", "--conditions"},
                "perl": {"-M", "-m", "-I", "-x"}, "ruby": {"-r", "-I", "-C", "-E", "-F"}, "php": {"-c", "-d", "-z", "-t"}}


_PROBE_FLAGS = {"--version", "-V", "-VV", "--help", "-h", "-?", "--help-all", "--help-env", "--help-xoptions"}


def _interpreter(cmd, args, ctx, stdin, detail):
    lang = "python" if cmd.startswith(("python", "pypy")) else _LANG_OF.get(cmd, cmd)
    if args and all(a in _PROBE_FLAGS or (a == "-v" and lang != "python") for a in args):
        return [Effect("run", detail, kind="command", detail=detail)]       # version / help probe
    if lang in ("perl", "ruby") and any((a.startswith("-p") or a.startswith("-n")) and "i" in a or a.startswith("-i") for a in args):
        files = [p for p in _paths_after_opts(args) if not re.match(r"^[sy]/|^tr/", p)]
        inline = [args[k + 1] for k, a in enumerate(args) if a in ("-e", "-pe", "-pie", "-E") and k + 1 < len(args)]
        files = [f for f in files if f not in inline]
        return [Effect("edit", ctx.N(f), detail=detail) for f in files] or [Effect("edit", UNKNOWN, detail=detail, certain=False)]
    if cmd == "deno" and args[:1] in (["run"], ["eval"]):
        if args[0] == "eval" and len(args) > 1:
            return _code_effects("node", args[-1], ctx, detail)
        args = args[1:]
    if cmd == "bun" and args[:1] and args[0] in ("run", "x", "install", "i", "add", "test", "remove", "rm", "update", "pm", "link", "publish"):
        return _h_node_pm("bun", args, ctx, stdin, detail)
    inline = []
    module = None
    script = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in _INLINE_FLAGS.get(lang, ()) and i + 1 < len(args):
            inline.append(args[i + 1])
            i += 2
            if lang in ("python",):
                break
            continue
        if lang == "python" and a.startswith("-c") and len(a) > 2:
            inline.append(a[2:])
            break
        if lang == "python" and a == "-m" and i + 1 < len(args):
            module = args[i + 1]
            break
        if lang == "python" and a.startswith("-m") and len(a) > 2:
            module = a[2:]
            args = args[:i] + ["-m", module] + args[i + 1:]
            break
        if a in _VALUE_FLAGS.get(lang, ()):
            i += 2
            continue
        if a == "-":
            break
        if not a.startswith("-"):
            script = a
            break
        i += 1
    if inline:
        return _code_effects(lang, "\n".join(inline), ctx, detail)
    if module is not None:
        rest = args[args.index("-m") + 2:] if "-m" in args else []
        if module in ("pytest", "unittest", "nose", "nose2", "tox", "doctest"):
            return [Effect("test", detail, kind="command", detail=detail)]
        if module == "pip":
            return _pip(rest, "pip", ctx, detail)
        if module == "http.server":
            return [Effect("network", "serve", kind="url", detail=detail)]
        if module in ("venv", "virtualenv"):
            pos = _paths_after_opts(rest)
            return [Effect("write", ctx.N(pos[0]) if pos else UNKNOWN, detail=detail)]
        if module in ("black", "isort", "autopep8", "autoflake", "yapf", "pyupgrade") or module == "ruff":
            return _classify_argv([module] + rest, ctx.child(depth=ctx.depth + 1), Stdin())[0]
        if module in ("json.tool", "pydoc", "this", "timeit", "py_compile", "compileall", "site", "platform", "sysconfig", "tokenize", "ast", "dis"):
            return [Effect("run", detail, kind="command", detail=detail)]
        if module.split(".")[0] in _PY_CHECKERS and not (module == "coverage" and rest[:1] != ["report"]):
            return [Effect("run", detail, kind="command", detail=detail)]      # read-only checkers (O1)
        if module.split(".")[0] == "holdfast":
            return [Effect("edit", ".git/holdfast", detail=f"{detail} (runs Holdfast's own modules)")]
        if _local_module(module, ctx):
            files = _module_files(module, 0, [], ctx, None)
            if files:
                main = files[-1]
                if main.endswith("__init__.py") and os.path.isfile(posixpath.join(posixpath.dirname(main), "__main__.py")):
                    main = posixpath.join(posixpath.dirname(main), "__main__.py")
                return _script_file(main, ctx, detail, lang="python")
            return [_opaque(module, detail)]
        if module not in sys.stdlib_module_names:
            return [_opaque(module, detail)]
        return [Effect("run", detail, kind="command", detail=detail)]
    if script is not None:
        if lang == "python" and posixpath.basename(script) == "setup.py":
            verbs = args[args.index(script) + 1:]
            if any(v in ("install", "develop") for v in verbs):
                return _install_effects(["."], "pypi.org", detail)
        return _script_file(script, ctx, detail, lang=lang)
    text = _stdin_script(stdin, ctx)
    if text is not None:
        return _code_effects(lang, text, ctx, f"{detail} <<{stdin.detail}")
    if stdin.kind not in ("none",):
        return [_opaque(ctx.N(stdin.path) if stdin.path else UNKNOWN, f"{detail} (program read from {stdin.detail or 'a pipe'})")]
    return [_opaque(UNKNOWN, detail)]


def _h_awk(cmd, args, ctx, stdin, detail):
    inplace = any(a == "inplace" for a in args) and "-i" in args
    i, prog, files = 0, None, []
    while i < len(args):
        a = args[i]
        if a in ("-f",) and i + 1 < len(args):
            prog = ctx.read_file(args[i + 1]) or ""
            i += 2
            continue
        if a in ("-F", "-v", "-i", "--include", "-l", "--load") and i + 1 < len(args):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        if prog is None:
            prog = a
        else:
            files.append(a)
        i += 1
    effs = _code_effects("awk", prog or "", ctx, detail)
    if inplace:
        effs += [Effect("edit", ctx.N(f), detail=detail) for f in files]
    return effs or [Effect("read", ctx.N(files[0]) if files else ".", detail=detail)]


def _script_file(path: str, ctx: Ctx, detail: str, lang: str | None, share: bool = False) -> list[Effect]:
    """Running a file (bash x.sh, ./x, python x.py, source x). Always exec_opaque; plus whatever its body visibly does."""
    target = ctx.N(path)
    opaque = _opaque(target, detail)
    ap = ctx.abs(path)
    if ap is None or ap in ctx.seen or ctx.depth > MAX_DEPTH:
        return [opaque]
    text = ctx.read_file(path)
    if text is None:
        return [opaque]
    if lang is None:
        first = text.split("\n", 1)[0]
        m = re.match(r"#!\s*(?:/usr/bin/env\s+(?:-S\s+)?)?(\S+)", first)
        interp = posixpath.basename(m.group(1)) if m else "sh"
        lang = "sh" if interp in SHELLS else ("python" if interp.startswith(("python", "pypy")) else _LANG_OF.get(interp, interp))
    note = f"{path}: "
    if lang == "sh":
        sub = ctx if share else ctx.child()
        sub.seen = ctx.seen | {ap}
        sub.depth = ctx.depth + 1
        body = _classify_text(text, sub)
        if share:
            sub.depth = ctx.depth
    elif lang in ("python", "node", "perl", "ruby", "php", "awk"):
        body = _code_effects(lang, text, ctx.child(depth=ctx.depth + 1, seen=ctx.seen | {ap}), detail, script_dir=posixpath.dirname(ap)) \
            if lang == "python" else _code_effects(lang, text, ctx.child(depth=ctx.depth + 1, seen=ctx.seen | {ap}), detail)
    else:
        return [opaque]
    precise = []
    for e in body:
        if e.action in ("run", "read") or (e.action == "exec_opaque" and e.target == UNKNOWN):
            continue
        e.detail = note + e.detail
        precise.append(e)
    if lang in ("sh", "python") and not any(e.action == "exec_opaque" for e in body):
        # fully analysed body: what it does is known, no blanket "could do anything" (O1). The invocation itself is
        # still a `run` with its arguments and environment (`NODE_ENV=production ./deploy.sh`, `deploy.sh prod`), so
        # command-pattern rules such as "never touch production" still see it (review3 Critical)
        return precise + [Effect("run", detail, kind="command", detail=detail)]
    return precise + [opaque]


# ----------------------------------------------------------------------------- handler table

_HANDLERS = {
    "link": lambda cmd, args, ctx, stdin, detail: _ln(args, ctx, detail),
    "rm": _h_delete, "unlink": _h_delete, "shred": _h_delete, "rmdir": _h_delete, "srm": _h_delete, "trash": _h_delete, "trash-put": _h_delete,
    "find": _h_find, "mv": _h_mv, "cp": _h_cp, "rsync": _h_cp, "install": _h_cp,
    "truncate": _h_truncate, "dd": _h_dd, "tee": _h_tee, "sed": _h_sed, "gsed": _h_sed,
    "chmod": _h_attr, "chown": _h_attr, "chgrp": _h_attr, "touch": _h_attr, "mkdir": _h_attr, "ln": _h_attr,
    "patch": _h_patch, "tar": _h_tar, "unzip": _h_unzip,
    "vim": _h_editor, "vi": _h_editor, "nvim": _h_editor, "ex": _h_editor, "ed": _h_editor, "emacs": _h_editor, "nano": _h_editor, "view": _h_editor,
    "ruff": _h_formatter, **{k: _h_formatter for k in _FORMATTERS},
    "eval": _h_eval, "source": _h_eval, ".": _h_eval, "trap": _h_trap, "watch": _h_watch, "su": _h_su, "xargs": _h_xargs,
    "echo": _h_echo, "printf": _h_echo, "base64": _h_base64, "cat": _h_cat, "rev": _h_rev_tr,
    "git": _h_git, "hub": _h_git,
    "sort": _h_sort_like, "uniq": _h_sort_like, "sponge": _h_sort_like, "split": _h_sort_like, "csplit": _h_sort_like,
    **{k: _h_compress for k in list(_COMPRESSORS) + list(_DECOMPRESSORS)}, "zip": _h_zip,
    "parallel": _h_parallel,
    "curl": _h_curl, "wget": _h_curl, "http": _h_curl, "https": _h_curl, "ssh": _h_ssh, "scp": _h_curl, "sftp": _h_curl,
    "nc": _h_net_simple, "ncat": _h_net_simple, "netcat": _h_net_simple, "telnet": _h_net_simple, "ftp": _h_net_simple,
    "ping": _h_net_simple, "ping6": _h_net_simple, "dig": _h_net_simple, "nslookup": _h_net_simple, "host": _h_net_simple,
    "traceroute": _h_net_simple, "whois": _h_net_simple, "mosh": _h_net_simple,
    "gh": _h_gh, "glab": _h_gh,
    "pip": _h_pip, "pip3": _h_pip,
    "npm": _h_node_pm, "yarn": _h_node_pm, "pnpm": _h_node_pm, "pnpx": _h_node_pm, "bunx": _h_node_pm, "npx": _h_npx,
    "cargo": _h_pkg_other, "gem": _h_pkg_other, "apt": _h_pkg_other, "apt-get": _h_pkg_other, "brew": _h_pkg_other,
    "poetry": _h_pkg_other, "conda": _h_pkg_other, "mamba": _h_pkg_other, "go": _h_pkg_other, "pipenv": _h_pkg_other,
    "composer": _h_pkg_other, "dnf": _h_pkg_other, "yum": _h_pkg_other, "apk": _h_pkg_other, "pacman": _h_pkg_other,
    "bundle": _h_pkg_other, "snap": _h_pkg_other, "nix-env": _h_pkg_other, "rustup": _h_pkg_other, "corepack": _h_pkg_other,
    "mvn": _h_pkg_other, "dotnet": _h_pkg_other,
    "uv": _h_uv, "uvx": _h_uv, "pipx": _h_pipx,
    "make": _h_make, "gmake": _h_make,
    "twine": _h_twine,
    "awk": _h_awk, "gawk": _h_awk, "mawk": _h_awk, "nawk": _h_awk,
    **{k: _h_infra for k in _INFRA},
}


# =============================================================================== public API

def classify_command(cmd: str, cwd: str, root: str | None = None, depth: int = 0, fs: bool = True) -> list[Effect]:
    """Shell command string -> effects. `fs=False` never touches the disk (no symlink / script / Makefile lookups)."""
    if not cmd.strip():
        return []
    if depth > MAX_DEPTH:
        return [_opaque(UNKNOWN, cmd)]
    ctx = Ctx(cwd=cwd, root=root, depth=depth, fs=fs)
    try:
        effs = _classify_text(cmd, ctx)
    except Exception as exc:  # noqa: BLE001 - a guard must never crash on odd input; unknown = opaque
        effs = [_opaque(UNKNOWN, f"{cmd} (classifier error: {type(exc).__name__})")]
    if depth == 0 and fs:
        effs = resolve_on_disk(effs, cwd, root)
    return effs


_PYTEST_SUMMARY = re.compile(r"^[=\s]*((?:\d+ (?:failed|passed|errors?|skipped|xfailed|xpassed|deselected|warnings?|"
                             r"rerun)(?:, )?)+)\s+in [\d.]+s", re.M)


def is_test_success(command: str, stdout: str, stderr: str) -> bool | None:
    """Did a test command succeed? None if the command is not a test runner. The runner's own summary line decides
    when there is one (so a green run that prints 'ValueError:' or a test named test_failing_input is green);
    otherwise conservative text heuristics."""
    joined = command.strip()
    effs = classify_command(joined, "/", "/", fs=False)
    if not any(e.action == "test" for e in effs):
        return None
    text = (stdout or "") + "\n" + (stderr or "")
    summ = _PYTEST_SUMMARY.findall(text)
    if summ:
        counts = dict((w.rstrip("s") if w.startswith(("error", "warning")) else w, int(n))
                      for n, w in re.findall(r"(\d+) (\w+)", summ[-1]))
        return counts.get("failed", 0) == 0 and counts.get("error", 0) == 0 and counts.get("passed", 0) > 0
    jest = re.findall(r"^Tests:\s+(.*)$", text, re.M)
    if jest:
        return "failed" not in jest[-1] and bool(re.search(r"\d+ passed", jest[-1]))
    cargo = re.findall(r"^test result: (ok|FAILED)\.", text, re.M)
    if cargo:
        return all(c == "ok" for c in cargo)
    mocha_fail = re.search(r"^\s*(\d+) failing", text, re.M)
    mocha_pass = re.search(r"^\s*(\d+) passing", text, re.M)
    if mocha_pass or mocha_fail:
        return bool(mocha_pass) and not (mocha_fail and int(mocha_fail.group(1)) > 0)
    if re.search(r"\b\d+ (?:failed|errors?)\b", text) or re.search(r"\bFAILED\b|\bERROR\b|Error:|failing|✗|✕|FAIL\b", text):
        return False
    if re.search(r"\b\d+ passed\b|\bpassed\b|\bPASS\b|✓|ok\b|OK\b", text):
        return True
    return False
    return False
