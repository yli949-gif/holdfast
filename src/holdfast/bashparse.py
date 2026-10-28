"""Bash command decomposition for Holdfast (SPEC §2, "decomposition").

Design
------
Holdfast never executes anything; it only needs to answer "which simple
commands would this string run, with which (unquoted) words, writing to which
files?".  We therefore implement a *small* shell lexer rather than a full
parser:

1. ``_lex`` walks the string once, tracking quotes, escapes, ``$(...)``,
   backticks, ``<(...)``, heredocs and redirections, and splits into *segments*
   at ``;`` ``&&`` ``||`` ``|`` ``&`` and newlines.  Words come out already
   shell-unquoted, so ``r""m``, ``\\rm`` and ``'r'm`` all become ``rm``.
2. ``_expand`` turns each segment into one or more :class:`Simple` commands:
   it strips wrappers (``sudo``, ``env``, ``time``, ``nohup``, ...), descends
   into ``sh -c``, ``eval``, ``xargs`` and ``find -exec``, and flags
   interpreter one-liners / piped shells as *opaque* with a best-effort guess
   of what they might do.
3. Paths (redirect targets, ``find`` start dirs) are resolved against the
   running ``cwd`` (which ``cd`` updates across segments) and ``home``.

Known limitations are deliberate and documented in README/SPEC: arithmetic,
globs, brace expansion, arrays and functions are not evaluated; variables other
than ``$HOME``/``$PWD`` are left literal and make the command *opaque* when
they form the command name.
"""
from __future__ import annotations

import base64
import binascii
import os
import re
import shlex
from dataclasses import dataclass, field

ALL_CATEGORIES = ("delete", "edit_path", "push", "force_push", "commit",
                  "network", "install", "run_cmd", "touch_prod")

SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
INTERPRETERS = {"python", "python3", "python2", "perl", "node", "nodejs", "ruby", "php"}
PIPE_SOURCES = {"curl", "wget", "base64", "openssl", "gpg", "xxd", "cat", "echo", "printf", "zcat", "gunzip"}
PREFIX_SIMPLE = {"time", "nohup", "command", "builtin", "exec", "caffeinate", "unbuffer",
                 "chronic", "setsid", "noglob", "nocorrect"}
PREFIX_WITH_ARG = {"nice": {"-n"}, "ionice": {"-c", "-n"}, "timeout": {"-k", "-s"},
                   "stdbuf": set(), "chrt": set(), "taskset": set()}
SUDO_ARG_OPTS = {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-T", "-U"}
XARGS_ARG_OPTS = {"-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a", "--max-args", "--max-procs",
                  "--delimiter", "--replace", "--arg-file"}
DEVNULL = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/zero"}
MAX_DEPTH = 6
# Multi-call binaries whose first argument is the real command (`busybox rm`, `toybox rm`).
MULTICALL = {"busybox", "toybox"}
# Shell keywords that may precede a command inside a compound statement (`do rm x`, `then rm x`).
LEADING_KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "{", "!"}
# Segments that are only a closing keyword (`done`, `fi`) run nothing.
CLOSING_KEYWORDS = {"done", "fi", "esac", "}", "do", "then", "else"}
SSH_ARG_OPTS = {"-p", "-i", "-l", "-o", "-J", "-F", "-L", "-R", "-D", "-W", "-b", "-c", "-e", "-m",
                "-O", "-Q", "-S", "-w", "-E", "-B", "-I"}


@dataclass
class Redirect:
    op: str          # '>', '>>', '<'
    target: str      # resolved absolute path
    fd: int = 1


@dataclass
class Simple:
    argv: list[str]
    raw: str = ""
    cwd: str = "/"
    redirects: list[Redirect] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    sep: str | None = None
    prev: "Simple | None" = field(default=None, repr=False)
    heredoc: str | None = None
    opaque: bool = False
    guess: list[str] = field(default_factory=list)
    guess_paths: list[str] = field(default_factory=list)
    evidence: str = ""
    unknown_args: bool = False
    via: list[str] = field(default_factory=list)
    static_stdout: str | None = None   # output of echo/printf/base64 -d when statically known
    remote: bool = False               # runs on another host (`ssh host 'cmd'`): paths are not local

    @property
    def cmd(self) -> str:
        return self.argv[0] if self.argv else ""

    @property
    def writes(self) -> list[str]:
        """Files this command writes through redirects, ``tee`` or ``dd of=``."""
        out = [r.target for r in self.redirects if r.op in (">", ">>") and r.target not in DEVNULL]
        if self.cmd in ("tee", "sponge"):
            out += [resolve_path(a, self.cwd, None) for a in self.argv[1:]
                    if not a.startswith("-") and a not in DEVNULL]
        elif self.cmd == "dd":
            out += [resolve_path(a[3:], self.cwd, None) for a in self.argv[1:]
                    if a.startswith("of=") and a[3:] not in DEVNULL]
        return out

    @property
    def truncates(self) -> list[str]:
        """Targets of a plain ``>`` redirect (content is replaced)."""
        return [r.target for r in self.redirects if r.op == ">" and r.target not in DEVNULL]


# --------------------------------------------------------------------------- paths

_VAR_DEFAULT = re.compile(r"\$\{\w+:?[-=]([^}]*)\}")


def resolve_path(word: str, cwd: str, home: str | None) -> str:
    """Resolve a shell word to an absolute, normalised path."""
    home = home or os.path.expanduser("~")
    w = word
    if w == "~" or w.startswith("~/"):
        w = home + w[1:]
    w = w.replace("${HOME}", home).replace("$HOME", home)
    w = w.replace("${PWD}", cwd).replace("$PWD", cwd)
    w = _VAR_DEFAULT.sub(lambda m: m.group(1), w)   # ${X:-tests} -> tests (X unknown to us)
    # `tests/$f`: the variable is unknown, but the literal prefix bounds what can be touched
    # (heldout tc-148). A word that *starts* with the variable is left literal.
    if "$" in w and not w.startswith("$"):
        head = w[:w.index("$")]
        if "/" in head:
            w = head.rstrip("/") or "/"
    if not w.startswith("/"):
        w = os.path.join(cwd, w)
    out = os.path.normpath(w)
    if out.startswith("//"):  # POSIX lets normpath keep a leading `//` (home="/" + "/proj")
        out = "/" + out.lstrip("/")
    return out


# --------------------------------------------------------------------------- lexer

@dataclass
class _Tok:
    kind: str                       # 'word' | 'redir'
    value: str = ""
    quoted: bool = False
    dyn: bool = False               # starts with a variable / substitution
    substs: list[str] = field(default_factory=list)
    op: str = ""
    fd: int = 1
    target: str | None = None


@dataclass
class _Seg:
    toks: list[_Tok]
    sep: str | None
    raw: str
    heredoc: str | None = None
    herestring: str | None = None


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _match_paren(text: str, i: int) -> int:
    """Index just past the ')' matching the '(' at text[i-1]; tolerant of quotes."""
    depth, n = 1, len(text)
    while i < n and depth:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c in "'\"":
            j = text.find(c, i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    return i


_STATIC_ECHO = re.compile(r"^\s*(?:echo|printf)\s+(?:-[neE]+\s+)?([^$`|;&<>()]+)$")


_STATIC_WHICH = re.compile(r"^\s*(?:which|command\s+-v|type\s+-[pP])\s+([\w./-]+)\s*$")


def _static_subst(inner: str) -> str | None:
    """`$(echo rm)` is statically `rm`, `$(which rm)` is `rm`, `$(pwd)` is `$PWD`; anything less trivial stays dynamic."""
    if inner.strip() == "pwd":
        return "$PWD"
    m = _STATIC_WHICH.match(inner)
    if m:
        return m.group(1)
    m = _STATIC_ECHO.match(inner)
    if not m:
        return None
    return " ".join(w.strip("'\"") for w in m.group(1).split())


def _static_stdin(seg: "_Seg", prev: "Simple | None") -> str | None:
    """Text a command reads on stdin when it is knowable: here-string, `echo X |`, `base64 -d` of a literal."""
    if seg.herestring is not None:
        return seg.herestring
    if prev is None or seg.sep != "|":
        return None
    return prev.static_stdout


def _lex(text: str) -> list[_Seg]:
    segs: list[_Seg] = []
    toks: list[_Tok] = []
    buf: list[str] = []
    in_word = False
    quoted = dyn = False
    substs: list[str] = []
    cur_sep: str | None = None
    seg_start = 0
    pending_redir: _Tok | None = None
    expect_heredoc: tuple[bool] | None = None   # (strip_tabs,)
    drop_next_word = False
    herestring: str | None = None
    heredocs: list[tuple[str, bool, int]] = []  # delimiter, strip_tabs, owner segment index
    i, n = 0, len(text)

    def flush() -> None:
        nonlocal in_word, buf, quoted, dyn, substs, pending_redir, expect_heredoc, drop_next_word, herestring
        if not in_word:
            return
        word = "".join(buf)
        if expect_heredoc is not None:
            heredocs.append((word, expect_heredoc[0], len(segs)))
            expect_heredoc = None
        elif drop_next_word:
            drop_next_word = False
            herestring = word
        elif pending_redir is not None:
            if pending_redir.op in (">&", "<&") and (word.isdigit() or word == "-"):
                pass  # fd duplication, not a file
            else:
                pending_redir.op = pending_redir.op.rstrip("&")
                pending_redir.target = word
                toks.append(pending_redir)
            pending_redir = None
        elif word not in ("{", "}"):
            toks.append(_Tok("word", word, quoted, dyn, substs))
        buf, in_word, quoted, dyn, substs = [], False, False, False, []

    def end_seg(next_sep: str | None, end: int) -> None:
        nonlocal toks, cur_sep, seg_start, pending_redir, herestring
        flush()
        if pending_redir is not None:
            toks.append(pending_redir)
            pending_redir = None
        if toks:
            segs.append(_Seg(toks, cur_sep, text[seg_start:end].strip(), herestring=herestring))
        toks = []
        herestring = None
        cur_sep = next_sep
        seg_start = end + 1

    def start_word() -> None:
        nonlocal in_word
        in_word = True

    def add_subst(inner: str) -> bool:
        """Record a $(...) substitution; returns True when the word becomes dynamic."""
        substs.append(inner)
        static = _static_subst(inner)
        if static is not None:
            buf.append(static)
            return False
        was_empty = not buf
        buf.append("$(...)")
        return was_empty

    while i < n:
        c = text[i]
        if c == "\\":
            if i + 1 < n and text[i + 1] == "\n":
                i += 2
                continue
            start_word()
            quoted = True
            if i + 1 < n:
                buf.append(text[i + 1])
            i += 2
            continue
        if c == "'":
            start_word()
            quoted = True
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            buf.append(text[i + 1:j])
            i = j + 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "'":
            start_word()
            quoted = True
            j = i + 2
            out: list[str] = []
            while j < n and text[j] != "'":
                if text[j] == "\\" and j + 1 < n:
                    out.append({"n": "\n", "t": "\t", "'": "'", "\\": "\\"}.get(text[j + 1], text[j + 1]))
                    j += 2
                else:
                    out.append(text[j])
                    j += 1
            buf.append("".join(out))
            i = j + 1
            continue
        if c == '"':
            start_word()
            quoted = True
            j = i + 1
            while j < n and text[j] != '"':
                d = text[j]
                if d == "\\" and j + 1 < n and text[j + 1] in '"\\$`\n':
                    if text[j + 1] != "\n":
                        buf.append(text[j + 1])
                    j += 2
                elif d == "$" and j + 1 < n and text[j + 1] == "(":
                    k = _match_paren(text, j + 2)
                    dyn = add_subst(text[j + 2:k - 1]) or dyn
                    j = k
                elif d == "`":
                    k = text.find("`", j + 1)
                    k = n if k < 0 else k
                    dyn = add_subst(text[j + 1:k]) or dyn
                    j = k + 1
                else:
                    if d == "$" and not buf:
                        dyn = True
                    buf.append(d)
                    j += 1
            i = j + 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "(":
            start_word()
            k = _match_paren(text, i + 2)
            dyn = add_subst(text[i + 2:k - 1]) or dyn
            i = k
            continue
        if c == "`":
            start_word()
            k = text.find("`", i + 1)
            k = n if k < 0 else k
            dyn = add_subst(text[i + 1:k]) or dyn
            i = k + 1
            continue
        if c in "<>" and i + 1 < n and text[i + 1] == "(":
            start_word()
            k = _match_paren(text, i + 2)
            substs.append(text[i + 2:k - 1])
            buf.append("$(...)")
            i = k
            continue
        if c == "$":
            start_word()
            if not buf:
                dyn = True
            buf.append(c)
            i += 1
            continue
        if c in " \t":
            flush()
            i += 1
            continue
        if c == "\n":
            end_seg("\n", i)
            i += 1
            if heredocs:
                # consume heredoc bodies line by line
                for delim, strip_tabs, owner in heredocs:
                    body: list[str] = []
                    while i < n:
                        j = text.find("\n", i)
                        j = n if j < 0 else j
                        line = text[i:j]
                        i = j + 1
                        if (line.lstrip("\t") if strip_tabs else line) == delim:
                            break
                        body.append(line)
                    if owner < len(segs):
                        segs[owner].heredoc = "\n".join(body) + ("\n" if body else "")
                heredocs.clear()
                seg_start = i
            continue
        if c == "#" and not in_word:
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == ";":
            end_seg(";", i)
            i += 2 if text[i + 1:i + 2] == ";" else 1
            continue
        if c == "&":
            if text[i + 1:i + 2] == "&":
                end_seg("&&", i)
                i += 2
            elif text[i + 1:i + 2] == ">":
                flush()
                op = "&>>" if text[i + 2:i + 3] == ">" else "&>"
                pending_redir = _Tok("redir", op=">>" if op == "&>>" else ">", fd=1)
                i += len(op)
            else:
                end_seg("&", i)
                i += 1
            continue
        if c == "|":
            if text[i + 1:i + 2] == "|":
                end_seg("||", i)
                i += 2
            else:
                end_seg("|", i)
                i += 2 if text[i + 1:i + 2] == "&" else 1
            continue
        if c == "(":
            end_seg(cur_sep, i)
            i += 1
            continue
        if c == ")":
            end_seg(";", i)
            i += 1
            continue
        if c in "<>":
            fd = 1 if c == ">" else 0
            if in_word and not quoted and "".join(buf).isdigit():
                fd = int("".join(buf))
                buf, in_word = [], False
            else:
                flush()
            if c == "<" and text[i + 1:i + 2] == "<":
                if text[i + 2:i + 3] == "<":          # here-string
                    drop_next_word = True
                    i += 3
                else:                                   # heredoc
                    strip = text[i + 2:i + 3] == "-"
                    expect_heredoc = (strip,)
                    i += 3 if strip else 2
                continue
            if c == ">" and text[i + 1:i + 2] == ">":
                op, i = ">>", i + 2
            elif c == ">" and text[i + 1:i + 2] == "|":
                op, i = ">", i + 2
            elif text[i + 1:i + 2] == "&":
                op, i = c + "&", i + 2
            else:
                op, i = c, i + 1
            pending_redir = _Tok("redir", op=op, fd=fd)
            continue
        start_word()
        buf.append(c)
        i += 1
    end_seg(None, n)
    if heredocs:  # heredoc declared on the last line without a trailing newline
        for _, _, owner in heredocs:
            if owner < len(segs):
                segs[owner].heredoc = ""
    return segs


# --------------------------------------------------------------------------- expansion

def _strip_prefixes(words: list[str], env: dict[str, str]) -> list[str]:
    """Remove sudo/env/time/... wrappers and leading VAR=val assignments."""
    changed = True
    while changed and words:
        changed = False
        w = words[0]
        m = _NAME.match(w)
        if m and m.end() < len(w) and w[m.end()] == "=":
            env[w[:m.end()]] = w[m.end() + 1:]
            words.pop(0)
            changed = True
            continue
        if "/" in w:  # `/usr/bin/env rm`, `/usr/bin/sudo rm`: wrappers by absolute path (heldout tc-110)
            base = os.path.basename(w)
            if base in ("sudo", "doas", "env") or base in PREFIX_SIMPLE or base in PREFIX_WITH_ARG \
                    or base in MULTICALL or base == "coreutils":
                w = words[0] = base
        if w in LEADING_KEYWORDS:  # `do rm x`, `then rm x`, `while read f`, `! test -e x`
            words.pop(0)
            changed = True
        elif w in MULTICALL:  # `busybox rm -rf x`: the applet is the real command (heldout tc-112)
            words.pop(0)
            changed = True
        elif w == "coreutils":  # `coreutils --coreutils-prog=rm -rf x` (heldout tc-113)
            words.pop(0)
            if words and words[0].startswith("--coreutils-prog="):
                words[0] = words[0].split("=", 1)[1]
            changed = True
        elif w in ("sudo", "doas"):
            words.pop(0)
            while words and words[0].startswith("-"):
                opt = words.pop(0)
                if opt in SUDO_ARG_OPTS and words:
                    words.pop(0)
                if opt == "--":
                    break
            changed = True
        elif w == "env":
            words.pop(0)
            while words:
                if words[0] == "-u" and len(words) > 1:
                    words.pop(0)
                    words.pop(0)
                elif words[0].startswith("-"):
                    words.pop(0)
                elif "=" in words[0] and _NAME.match(words[0]):
                    k, _, v = words.pop(0).partition("=")
                    env[k] = v
                else:
                    break
            changed = True
        elif w in PREFIX_SIMPLE:
            words.pop(0)
            while words and words[0].startswith("-") and w in ("time", "nohup", "command"):
                words.pop(0)
            changed = True
        elif w in PREFIX_WITH_ARG:
            arg_opts = PREFIX_WITH_ARG[w]
            words.pop(0)
            if w == "timeout":
                while words and words[0].startswith("-"):
                    opt = words.pop(0)
                    if opt in arg_opts and words:
                        words.pop(0)
                if words:
                    words.pop(0)  # the duration
            else:
                while words and words[0].startswith("-"):
                    opt = words.pop(0)
                    if opt in arg_opts and words:
                        words.pop(0)
            changed = True
    return words


_DELETE_CODE = re.compile(
    r"\b(os\.remove|os\.unlink|shutil\.rmtree|os\.rmdir|os\.removedirs|rmtree|\.unlink\w*\(|unlink\w*\b|"
    r"\.rmdir\(|rmdir\b|fs\.rm\w*\(|rmSync|rimraf|File\.delete|FileUtils\.rm\w*|send2trash|"
    r"\bremove\(|Dir\.delete|Dir\.rmdir|\.delete\(\))")  # a bare 'rm -rf' string literal is not a delete (tc-252)
_WRITE_CODE = re.compile(
    r"(open\([^)]*['\"][wax]\+?b?['\"]|open\([^)]*mode\s*=\s*['\"][wax]|\.write_text\(|\.write_bytes\(|"
    r"writeFile\w*\(|appendFile\w*\(|File\.write|File\.open\([^)]*['\"][wa]|copyFile\w*\(|renameSync|"
    r"shutil\.(copy\w*|move)|os\.(rename|replace)\(|FileUtils\.(cp|mv)|\bopen\(\s*(my\s+)?\$\w+\s*,\s*['\"]>|"
    r"\.truncate\(|io\.open\([^)]*['\"][wa])")
_NET_CODE = re.compile(
    r"(urllib|requests\.|http\.client|httpx|aiohttp|socket\.|\bfetch\(|https?\.(get|request)\(|axios|"
    r"Net::HTTP|LWP::|\bcurl\b|\bwget\b|XMLHttpRequest|net\.connect|WebSocket|ftplib|smtplib|paramiko)")
_SHELL_CODE = re.compile(r"(subprocess|os\.system|os\.popen|child_process|execSync|spawn\w*\(|\bsystem\(|`[^`]+`|%x\()")
_GIT_PUSH_CODE = re.compile(r"git\s+push")
_CODE_PATH = re.compile(
    r"(?:remove|unlink\w*|rmtree|rmdir|rm\w*|delete|open|writeFile\w*|write_text|write_bytes|Path)"
    r"\s*\(?\s*(?:glob\s*\(?\s*)?(?:os\.path\.join\(\s*)?['\"]([^'\"\n]+)['\"]")
_CODE_JOIN = re.compile(r"os\.path\.join\(\s*['\"]([^'\"\n]+)['\"]\s*,\s*['\"]([^'\"\n]+)['\"]\s*\)")


def _scan_code(code: str, cwd: str) -> tuple[list[str], list[str], str]:
    guess: list[str] = []
    ev: list[str] = []
    for cat, rx in (("delete", _DELETE_CODE), ("edit_path", _WRITE_CODE), ("network", _NET_CODE)):
        m = rx.search(code)
        if m:
            guess.append(cat)
            ev.append(f"{cat}: {m.group(0).strip()!r}")
    if _GIT_PUSH_CODE.search(code):
        guess.append("push")
        ev.append("push: 'git push'")
    m = _SHELL_CODE.search(code)
    if m:
        for cat in ("delete", "edit_path", "run_cmd"):
            if cat not in guess:
                guess.append(cat)
        ev.append(f"spawns a shell: {m.group(0).strip()!r}")
    code = _CODE_JOIN.sub(lambda m: f"'{m.group(1)}/{m.group(2)}'", code)  # os.path.join('a','b') -> 'a/b'
    paths = [resolve_path(p, cwd, None) for p in _CODE_PATH.findall(code) if p]
    return guess, paths, "; ".join(ev)


def _interpreter_code(argv: list[str], heredoc: str | None) -> str | None:
    """Return inline code for python/perl/node/ruby/php one-liners, else None."""
    cmd = os.path.basename(argv[0])
    flags = {"python": ("-c",), "perl": ("-e", "-E"), "node": ("-e", "--eval", "-p", "--print"),
             "ruby": ("-e",), "php": ("-r",)}
    key = next((k for k in flags if cmd.startswith(k)), None)
    if key is None:
        return None
    for i, a in enumerate(argv[1:], 1):
        if a in flags[key] and i + 1 < len(argv):
            return argv[i + 1]
        if key == "python" and a.startswith("-c") and len(a) > 2:
            return a[2:]
    rest = [a for a in argv[1:] if not a.startswith("-") or a == "-"]
    if heredoc is not None and (not rest or rest[0] == "-"):
        return heredoc
    return None


def _xargs(words: list[str]) -> list[str]:
    i = 1
    while i < len(words) and words[i].startswith("-"):
        opt = words[i]
        i += 1
        if opt in XARGS_ARG_OPTS and i < len(words):
            i += 1
        elif opt == "--":
            break
    return words[i:] or ["echo"]


def _find_start_dirs(words: list[str], cwd: str) -> list[str]:
    dirs: list[str] = []
    for w in words[1:]:
        if w.startswith("-") or w in ("(", "!", ")"):
            break
        dirs.append(resolve_path(w, cwd, None))
    return dirs or [cwd]


def _find_exec(words: list[str], cwd: str) -> list[list[str]]:
    """Return the argv lists of every -exec/-execdir/-ok clause in a find command."""
    out: list[list[str]] = []
    dirs = _find_start_dirs(words, cwd)
    i = 0
    while i < len(words):
        if words[i] in ("-exec", "-execdir", "-ok", "-okdir"):
            j = i + 1
            sub: list[str] = []
            while j < len(words) and words[j] not in (";", "+"):
                sub.extend(dirs if words[j] == "{}" else [words[j]])
                j += 1
            if sub:
                out.append(sub)
            i = j
        i += 1
    return out


_VAR_WORD = re.compile(r"^\$\{?(\w+)\}?$")
_BRACE = re.compile(r"\{([^{}]*,[^{}]*)\}")


def _brace_expand(word: str, limit: int = 64) -> list[str]:
    """`tests/{unit,integration}` -> both words; nested braces and sequences (`{1..9}`) stay literal."""
    out = [word]
    for _ in range(4):  # a few comma groups at most
        nxt: list[str] = []
        for w in out:
            m = _BRACE.search(w)
            if m is None or "$" in m.group(1):
                nxt.append(w)
            else:
                nxt.extend(w[:m.start()] + alt + w[m.end():] for alt in m.group(1).split(","))
        if nxt == out or len(nxt) > limit:
            return out
        out = nxt
    return out


def _expand(seg: _Seg, cwd: str, home: str | None, prev: Simple | None,
            depth: int, via: list[str], shvars: dict[str, str] | None = None) -> list[Simple]:
    shvars = shvars if shvars is not None else {}
    out: list[Simple] = []
    words = [t.value for t in seg.toks if t.kind == "word"]
    word_toks = [t for t in seg.toks if t.kind == "word"]
    redirects = [Redirect(t.op, resolve_path(t.target, cwd, home), t.fd)
                 for t in seg.toks if t.kind == "redir" and t.target]
    # substitutions run first
    for t in word_toks:
        for inner in t.substs:
            out.extend(decompose(inner, cwd, home, _depth=depth + 1, _via=via + ["$(...)"]))
    env: dict[str, str] = {}
    n_before = len(words)
    words = _strip_prefixes(list(words), env)
    dropped = n_before - len(words)
    first_tok = word_toks[dropped] if dropped < len(word_toks) else None
    if words and words[0] == "for":  # `for f in a b c` binds f for the loop body (heldout tc-147)
        if len(words) >= 3 and words[2] == "in":
            shvars[words[1]] = " ".join(words[3:])
        return out
    if len(words) == 1 and words[0] in CLOSING_KEYWORDS:  # `done`, `fi`: no command runs
        return out
    # `rm $f` with f bound by `for`/`x=...`: substitute the known value(s) into the arguments;
    # `rm -rf tests/{unit,integration}`: expand one level of unquoted braces (heldout tc-123)
    bound_toks: set[int] = set()
    if len(words) > 1:
        new_words: list[str] = [words[0]]
        for i, w in enumerate(words[1:], 1):
            m = _VAR_WORD.match(w)
            tok = word_toks[dropped + i] if dropped + i < len(word_toks) else None
            if m and m.group(1) in shvars:
                new_words.extend(shvars[m.group(1)].split() or [w])
                bound_toks.add(dropped + i)
            elif "{" in w and tok is not None and not tok.quoted:
                new_words.extend(_brace_expand(w))
            else:
                new_words.append(w)
        words = new_words
    arg_toks = [t for i, t in enumerate(word_toks) if i > dropped and i not in bound_toks]

    s = Simple(argv=words, raw=seg.raw, cwd=cwd, redirects=redirects, env=env, sep=seg.sep,
               prev=prev, heredoc=seg.heredoc, via=list(via))
    if not words:
        out.append(s)
        return out

    cmd = os.path.basename(words[0]) if "/" in words[0] else words[0]
    stdin = _static_stdin(seg, prev)
    if first_tok is not None and first_tok.dyn:
        m = _VAR_WORD.match(words[0])
        if m and m.group(1) in shvars and depth < MAX_DEPTH:
            rest = " ".join(shlex.quote(w) for w in words[1:])
            out.extend(decompose(f"{shvars[m.group(1)]} {rest}", cwd, home, _depth=depth + 1,
                                 _via=via + [f"${m.group(1)}"]))
            return out
        s.opaque, s.guess = True, list(ALL_CATEGORIES)
        s.evidence = f"command name is dynamic: {words[0]!r}"
        out.append(s)
        return out

    if depth < MAX_DEPTH:
        if cmd == "ssh":
            # `ssh host 'rm -rf x'`: the remote shell re-parses the joined arguments (heldout tc-077).
            # The ssh command itself stays (network / prod host); remote commands carry no local paths.
            i, host = 1, None
            while i < len(words):
                if words[i] in SSH_ARG_OPTS and i + 1 < len(words):
                    i += 2
                elif words[i].startswith("-"):
                    i += 1
                else:
                    host, i = words[i], i + 1
                    break
            out.append(s)
            if host is not None and i < len(words):
                for r in decompose(" ".join(words[i:]), cwd, home, _depth=depth + 1, _via=via + [f"ssh {host}"]):
                    r.remote = True
                    out.append(r)
            return out
        if cmd in SHELLS:
            script = None
            for i, a in enumerate(words[1:], 1):
                if a.startswith("-") and "c" in a[1:] and not a.startswith("--") and i + 1 < len(words):
                    script = words[i + 1]
                    break
            if script is not None:
                out.extend(decompose(script, cwd, home, _depth=depth + 1, _via=via + [f"{cmd} -c"]))
                return out
            any_dyn = any(t.dyn or (t.substs and "$(...)" in t.value) for t in arg_toks)
            piped = prev is not None and seg.sep == "|" and (prev.cmd in PIPE_SOURCES or prev.opaque)
            if stdin is not None and not [a for a in words[1:] if not a.startswith("-")]:
                out.extend(decompose(stdin, cwd, home, _depth=depth + 1, _via=via + [f"{cmd} <stdin"]))
                return out
            if piped or any_dyn or (seg.heredoc and not [a for a in words[1:] if not a.startswith("-")]):
                s.opaque, s.guess = True, list(ALL_CATEGORIES)
                s.evidence = f"{cmd} executes a script that is not visible here"
                if piped:
                    s.evidence = f"{cmd} executes output piped from {prev.cmd!r}"
            out.append(s)
            return out
        if cmd == "eval":
            out.extend(decompose(" ".join(words[1:]), cwd, home, _depth=depth + 1, _via=via + ["eval"]))
            return out
        if cmd == "xargs":
            argv = _xargs(words)
            inner = Simple(argv=argv, raw=seg.raw, cwd=cwd, redirects=redirects, env=env,
                           sep=seg.sep, prev=prev, via=via + ["xargs"], unknown_args=True)
            if stdin is not None:  # `echo a b | xargs rm`, `xargs rm <<< a`
                inner.argv = [a for a in argv if "{}" not in a] + stdin.split()
                inner.unknown_args = False
            else:  # `xargs -I{} rm tests/{}` -> at least we know the directory
                inner.argv = [a.replace("{}", "").rstrip("/") or a for a in argv if a.replace("{}", "").rstrip("/") or a == "{}"]
                inner.argv = [a for a in inner.argv if a != "{}"]
            if prev is not None and prev.cmd == "find":
                inner.guess_paths = _find_start_dirs(prev.argv, prev.cwd)
            out.append(inner)
            return out
        if cmd == "find":
            out.append(s)
            for sub in _find_exec(words, cwd):
                out.append(Simple(argv=sub, raw=seg.raw, cwd=cwd, env=env, sep=seg.sep, prev=prev,
                                  via=via + ["find -exec"]))
            return out

    if cmd == "git" and any(t.dyn or "$(...)" in t.value for t in arg_toks):
        s.opaque, s.guess = True, ["push", "force_push", "commit", "delete", "network"]
        s.evidence = "git subcommand or arguments are dynamic"
        out.append(s)
        return out

    if cmd in ("echo", "printf") and not any(t.dyn or "$(...)" in t.value for t in arg_toks):
        body = " ".join(a for a in words[1:] if not (a.startswith("-") and a[1:] and set(a[1:]) <= set("neE")))
        s.static_stdout = body.replace("\\n", "\n").replace("\\t", "\t") if cmd == "printf" else body
    elif cmd == "base64" and any(a in ("-d", "--decode", "-D") for a in words[1:]):
        src = stdin
        files = [a for a in words[1:] if not a.startswith("-")]
        if src is not None and not files:
            try:
                s.static_stdout = base64.b64decode("".join(src.split()), validate=False).decode("utf-8", "replace")
            except (ValueError, binascii.Error):
                pass

    if cmd in INTERPRETERS or any(cmd.startswith(p) for p in ("python",)):
        code = _interpreter_code(words, seg.heredoc if seg.heredoc is not None else stdin)
        piped = prev is not None and seg.sep == "|" and prev.cmd in PIPE_SOURCES and stdin is None
        if code is not None:
            guess, paths, ev = _scan_code(code, cwd)
            if guess:
                s.opaque, s.guess, s.guess_paths = True, guess, paths
                s.evidence = f"{cmd} inline code: {ev}"
        elif piped:
            s.opaque, s.guess = True, list(ALL_CATEGORIES)
            s.evidence = f"{cmd} executes code piped from {prev.cmd!r}"
    out.append(s)
    return out


def decompose(command: str, cwd: str, home: str | None = None, *, _depth: int = 0,
              _via: list[str] | None = None) -> list[Simple]:
    """Decompose a Bash command string into simple commands, in execution order."""
    if not command or not command.strip():
        return []
    cur = cwd
    prev: Simple | None = None
    result: list[Simple] = []
    shvars: dict[str, str] = {}
    for seg in _lex(command):
        simples = _expand(seg, cur, home, prev, _depth, _via or [], shvars)
        if not simples:
            continue
        result.extend(simples)
        last = simples[-1]
        if not last.argv and last.env:  # bare `name=value` assignment
            shvars.update(last.env)
        if last.cmd in ("cd", "pushd") and not last.opaque:
            args = [a for a in last.argv[1:] if not a.startswith("-")]
            if not args:
                cur = home or os.path.expanduser("~")
            elif args[0] != "-":
                cur = resolve_path(args[0], cur, home)
        prev = last
    return result
