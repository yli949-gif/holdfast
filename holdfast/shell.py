"""Shell command → effects.

Deliberately conservative: when we cannot tell what a command touches we emit an
effect with target UNKNOWN and certain=False, and let the monitor's
``opaque_policy`` decide (ask by default). We never try to be a full bash
parser; the goal is to make the *cheap* bypasses (redirects, chaining,
`bash -c`, `xargs`, `find -delete`, interpreters) visible to rules.
"""
from __future__ import annotations

import re
import shlex

from .effects import Effect, EVERYTHING, UNKNOWN, normalize_path

WRAPPERS = {"sudo", "doas", "nohup", "time", "env", "command", "builtin", "exec",
            "nice", "ionice", "timeout", "stdbuf", "chronic", "caffeinate"}
# wrappers whose following argument(s) are options/values to skip
WRAPPER_SKIP_ARGS = {"timeout": 1, "nice": 0, "ionice": 0}
# wrapper option flags that consume the next word as their value (sudo -u root, nice -n 5, ...)
WRAPPER_VALUE_FLAGS = {
    "sudo": {"-u", "-g", "-h", "-p", "-r", "-t", "-U", "-C", "-D", "-T"},
    "doas": {"-u", "-C"},
    "nice": {"-n"},
    "ionice": {"-c", "-n", "-p"},
    "env": {"-u", "-C", "-S"},
    "timeout": {"-s", "-k"},
}

INTERPRETERS = {"python", "python3", "python2", "node", "perl", "ruby", "php", "deno", "bun", "uv"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
TEST_RUNNERS = re.compile(r"^(pytest|py\.test|python3? -m pytest|npm (run )?test|yarn test|pnpm test|go test|cargo test|make test|jest|vitest|mocha|rspec|phpunit|tox|nox)\b")

INTERP_DANGER = [
    (re.compile(r"\b(os\.remove|os\.unlink|os\.rmdir|shutil\.rmtree|os\.removedirs|unlinkSync|rmSync|rimraf|fs\.rm\b|File\.delete|FileUtils\.rm)"), "delete"),
    (re.compile(r"\bopen\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"][wa]"), "write"),
    (re.compile(r"\b(writeFileSync|writeFile|write_text|Path\([^)]*\)\.write)"), "write"),
    (re.compile(r"\b(subprocess|os\.system|child_process|exec\(|spawn\()"), "exec_opaque"),
    (re.compile(r"\b(requests\.|urllib|httpx|fetch\(|axios|http\.get|socket\.)"), "network"),
]


_SEPARATORS = {";", "&&", "||", "|", "&", "|&", ";;"}


def _tokenize(cmd: str) -> list[str]:
    # newline is a command separator, not whitespace: it must surface as a token so `ls\nrm x` is two commands
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|<>()\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        raw = list(lex)
    except ValueError:
        # unbalanced quotes etc. — fall back to whitespace split, mark opaque later
        return cmd.split()
    # shlex returns runs of punctuation as one token ("&&\n", "(\n"); split the newlines back out
    out: list[str] = []
    for t in raw:
        if "\n" in t and set(t) <= set(";&|<>()\n"):
            for part in re.split(r"(\n+)", t):
                if part:
                    out.append("\n" if part.startswith("\n") else part)
        else:
            out.append(t)
    return out


def _split_pipeline(tokens: list[str]) -> list[list[str]]:
    """Split token stream into simple commands on ; && || | & newline."""
    cmds: list[list[str]] = []
    cur: list[str] = []
    for t in tokens:
        if t in _SEPARATORS or t == "\n":
            if cur:
                cmds.append(cur)
            cur = []
        elif t in ("(", ")", "{", "}"):
            continue
        else:
            cur.append(t)
    if cur:
        cmds.append(cur)
    return cmds


def _strip_wrappers(words: list[str]) -> list[str]:
    i = 0
    while i < len(words):
        w = words[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):      # env assignment prefix
            i += 1
            continue
        if w in WRAPPERS:
            i += 1
            # skip option flags of the wrapper (e.g. sudo -u root, env -i), then its positional args (timeout 5)
            while i < len(words) and words[i].startswith("-"):
                flag = words[i]
                i += 1
                if flag in WRAPPER_VALUE_FLAGS.get(w, ()) and i < len(words):
                    i += 1
            i += WRAPPER_SKIP_ARGS.get(w, 0)
            continue
        break
    return words[i:]


def _redirect_effects(words: list[str], cwd: str, root: str | None) -> tuple[list[str], list[Effect]]:
    """Extract `> file`, `>> file`, `&> file` redirects; return remaining words + write effects."""
    out: list[Effect] = []
    rest: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w in (">", ">>", "&>", "&>>") or re.fullmatch(r"\d?>>?", w):
            if i + 1 < len(words):
                target = words[i + 1]
                if target not in ("/dev/null", "&1", "&2") and not target.startswith("&"):
                    out.append(Effect("write", normalize_path(target, cwd, root), detail=f"redirect {w} {target}"))
                i += 2
                continue
        if w == "<":
            i += 2
            continue
        rest.append(w)
        i += 1
    return rest, out


def _paths_after_opts(args: list[str]) -> list[str]:
    """Positional (non-option) arguments; everything after a bare `--` is positional."""
    if "--" in args:
        i = args.index("--")
        return [a for a in args[:i] if not a.startswith("-")] + list(args[i + 1:])
    return [a for a in args if not a.startswith("-")]


def _basename(cmd: str) -> str:
    return cmd.rsplit("/", 1)[-1]


def classify_simple(words: list[str], cwd: str, root: str | None, depth: int = 0) -> list[Effect]:
    if not words:
        return []
    words, effects = _redirect_effects(words, cwd, root)
    words = _strip_wrappers(words)
    if not words:
        return effects
    cmd = _basename(words[0])
    args = words[1:]
    pos = _paths_after_opts(args)
    joined = " ".join(words)
    N = lambda p: normalize_path(p, cwd, root)

    def everything_or(paths: list[str]) -> list[str]:
        if any(p in ("/", "*", ".", "./", "./*", "~", "~/") or p.startswith("/*") for p in paths):
            return [EVERYTHING]
        return paths

    # --- shells / eval: recurse into the string ---
    if cmd in SHELLS and depth < 3:
        if "-c" in args:
            inner = args[args.index("-c") + 1] if args.index("-c") + 1 < len(args) else ""
            return effects + classify_command(inner, cwd, root, depth + 1)
        if pos:  # bash script.sh
            return effects + [Effect("exec_opaque", N(pos[0]), kind="opaque", detail=joined, certain=False)]
        return effects + [Effect("run", joined, kind="command", detail=joined)]
    if cmd in ("eval", "source", ".") and depth < 3:
        inner = " ".join(args)
        if cmd == "eval":
            return effects + classify_command(inner, cwd, root, depth + 1)
        return effects + [Effect("exec_opaque", N(pos[0]) if pos else UNKNOWN, kind="opaque", detail=joined, certain=False)]

    # --- xargs: the real command follows ---
    if cmd == "xargs" and depth < 3:
        inner_words = [a for a in args if not a.startswith("-")]
        if inner_words:
            inner = classify_simple(inner_words + [UNKNOWN], cwd, root, depth + 1)
            for e in inner:
                e.certain = False
                if e.target not in (EVERYTHING,):
                    e.target = UNKNOWN
            return effects + inner
        return effects + [Effect("exec_opaque", UNKNOWN, kind="opaque", detail=joined, certain=False)]

    # --- in-place perl edits (must precede the generic interpreter branch, which would swallow `-e`) ---
    if cmd == "perl" and any(a.startswith("-p") and "i" in a or a.startswith("-i") for a in args):
        files = [p for p in pos if not p.startswith("s/")]
        return effects + [Effect("edit", N(f), detail=joined) for f in files] or [Effect("edit", UNKNOWN, detail=joined, certain=False)]

    # --- uv as a package manager / runner (must precede the generic interpreter branch) ---
    if cmd == "uv":
        if pos[:1] == ["add"] or (pos[:2] == ["pip", "install"]):
            pkgs = pos[1:] if pos[0] == "add" else pos[2:]
            return effects + [Effect("install", " ".join(pkgs) or UNKNOWN, kind="package", detail=joined), Effect("network", "uv", kind="url", detail=joined)]
        if args[:1] == ["run"] and depth < 3:
            inner = args[1:]
            # drop `uv run` options (--with pkg, --python 3.12, -q ...) up to the real command
            while inner and inner[0].startswith("-"):
                flag = inner.pop(0)
                if flag in ("--with", "--python", "-p", "--group", "--extra", "--env-file", "--directory", "--project") and inner:
                    inner.pop(0)
            if inner:
                return effects + classify_simple(inner, cwd, root, depth + 1)

    # --- interpreters ---
    if cmd in INTERPRETERS or cmd.startswith("python3."):
        inline = None
        for flag in ("-c", "-e", "--eval", "-r"):
            if flag in args and args.index(flag) + 1 < len(args):
                inline = args[args.index(flag) + 1]
        if cmd == "uv" and args[:1] == ["run"]:
            inline = None
        if inline is not None:
            found = []
            for rx, act in INTERP_DANGER:
                m = rx.search(inline)
                if m:
                    tgt = UNKNOWN
                    if m.groups() and m.group(1) and act == "write":
                        tgt = N(m.group(1))
                    else:
                        # try to recover a quoted path argument right after the dangerous call
                        tail = inline[m.end():m.end() + 120]
                        q = re.match(r"\s*\(\s*['\"]([^'\"]+)['\"]", tail)
                        if q:
                            tgt = N(q.group(1))
                    found.append(Effect(act, tgt, kind="path" if tgt != UNKNOWN else "opaque", detail=joined, certain=False))
            if found:
                return effects + found
            return effects + [Effect("exec_opaque", UNKNOWN, kind="opaque", detail=joined, certain=False)]
        if cmd in ("python", "python3") and "-m" in args:
            mod = args[args.index("-m") + 1] if args.index("-m") + 1 < len(args) else ""
            if mod in ("pytest", "unittest", "nose", "tox"):
                return effects + [Effect("test", joined, kind="command", detail=joined)]
            if mod == "pip":
                pkgs = [p for p in pos[1:] if p != "install"]
                return effects + [Effect("install", " ".join(pkgs) or UNKNOWN, kind="package", detail=joined),
                                  Effect("network", cmd, kind="url", detail=joined)]
            if mod == "http.server":
                return effects + [Effect("network", "serve", kind="url", detail=joined)]
        if pos:
            return effects + [Effect("exec_opaque", N(pos[0]), kind="opaque", detail=joined, certain=False)]
        return effects + [Effect("exec_opaque", UNKNOWN, kind="opaque", detail=joined, certain=False)]

    # --- deletion ---
    if cmd in ("rm", "unlink", "shred", "rmdir"):
        targets = everything_or(pos) or [UNKNOWN]
        return effects + [Effect("delete", t if t in (EVERYTHING, UNKNOWN) else N(t), detail=joined) for t in targets]
    if cmd == "find":
        if "-delete" in args or "-exec" in args and any(a in ("rm", "unlink", "shred") for a in args):
            roots = [a for a in pos if not a.startswith("-")] or ["."]
            roots = [r for r in roots if r not in ("rm", "unlink", "shred", "{}", "\\;", ";", "+", "-rf", "-r", "-f")]
            roots = everything_or(roots[:1]) or ["."]
            return effects + [Effect("delete", t if t == EVERYTHING else N(t), detail=joined, certain=False) for t in roots]
        return effects + [Effect("read", N(pos[0]) if pos else ".", detail=joined)]
    if cmd == "mv":
        if len(pos) >= 2:
            srcs, dst = pos[:-1], pos[-1]
            return effects + [Effect("move", N(s), detail=joined) for s in srcs] + [Effect("write", N(dst), detail=joined)]
        return effects + [Effect("move", UNKNOWN, detail=joined, certain=False)]
    if cmd in ("cp", "rsync", "install"):
        if len(pos) >= 2:
            effs = [Effect("write", N(pos[-1]), detail=joined)]
            if cmd == "rsync" and "--delete" in args:
                effs.append(Effect("delete", N(pos[-1]), detail=joined))
            return effects + effs
    if cmd in ("truncate", "dd"):
        if cmd == "dd":
            of = [a[3:] for a in args if a.startswith("of=")]
            return effects + [Effect("write", N(of[0]) if of else UNKNOWN, detail=joined)]
        return effects + [Effect("write", N(p), detail=joined) for p in pos] or [Effect("write", UNKNOWN, detail=joined)]
    if cmd == "tee":
        return effects + [Effect("write", N(p), detail=joined) for p in pos]
    if cmd == "sed":
        if any(a == "-i" or a.startswith("-i") or a == "--in-place" for a in args):
            # values of -e/-f/--expression/--file are scripts, not files
            script_vals = {args[i + 1] for i, a in enumerate(args) if a in ("-e", "-f", "--expression", "--file") and i + 1 < len(args)}
            files = [p for p in pos if p not in script_vals]
            if not script_vals and files:
                files = files[1:]   # first positional is the script
            return effects + [Effect("edit", N(f), detail=joined) for f in files] or [Effect("edit", UNKNOWN, detail=joined, certain=False)]
        return effects + [Effect("read", N(p), detail=joined) for p in pos[1:]]
    if cmd in ("chmod", "chown", "chgrp", "touch", "mkdir", "ln"):
        act = "write" if cmd in ("touch", "mkdir", "ln") else "edit"
        return effects + [Effect(act, N(p), detail=joined) for p in pos] or [Effect(act, UNKNOWN, detail=joined)]
    if cmd in ("patch",):
        return effects + [Effect("edit", UNKNOWN, detail=joined, certain=False)]

    # --- git ---
    if cmd == "git":
        gargs = [a for a in args if not (a.startswith("-C") or a.startswith("--git-dir"))]
        sub = next((a for a in gargs if not a.startswith("-")), "")
        rest = gargs[gargs.index(sub) + 1:] if sub in gargs else []
        rpos = _paths_after_opts(rest)
        if sub == "push":
            forced = any(a in ("-f", "--force", "--force-with-lease", "--force-if-includes") or a.startswith("--force") for a in rest) \
                or any(a.startswith("+") for a in rpos) or any(a == "--delete" or a == "-d" for a in rest)
            remote = rpos[0] if rpos else "origin"
            branch = rpos[1].lstrip("+") if len(rpos) > 1 else "HEAD"
            if ":" in branch:
                branch = branch.split(":")[-1]
            tgt = f"{remote}/{branch}"
            effs = [Effect("force_push" if forced else "push", tgt, kind="branch", detail=joined)]
            if any(a in ("--delete", "-d") for a in rest):
                effs.append(Effect("delete", tgt, kind="branch", detail=joined))
            return effects + effs
        if sub == "reset" and any(a in ("--hard", "--merge") for a in rest):
            return effects + [Effect("discard", EVERYTHING, detail=joined)]
        if sub == "checkout":
            if "--" in rest or (rpos and (rpos[0] in (".", "*") or "/" in rpos[0] or "." in rpos[0])):
                paths = rest[rest.index("--") + 1:] if "--" in rest else rpos
                paths = everything_or(paths)
                return effects + [Effect("discard", p if p == EVERYTHING else N(p), detail=joined, certain=False) for p in paths] or [Effect("discard", UNKNOWN, detail=joined, certain=False)]
            return effects + [Effect("run", joined, kind="command", detail=joined)]
        if sub == "restore":
            paths = everything_or(rpos) or [UNKNOWN]
            return effects + [Effect("discard", p if p in (EVERYTHING, UNKNOWN) else N(p), detail=joined) for p in paths]
        if sub == "clean":
            if any("f" in a for a in rest if a.startswith("-") and not a.startswith("--")) or "--force" in rest:
                paths = everything_or(rpos) or ["."]
                return effects + [Effect("delete", p if p == EVERYTHING else N(p), detail=joined) for p in paths]
            return effects + [Effect("read", ".", detail=joined)]
        if sub == "branch" and any(a in ("-D", "-d", "--delete") for a in rest):
            return effects + [Effect("delete", f"branch/{p}", kind="branch", detail=joined) for p in rpos]
        if sub == "stash" and rpos[:1] in (["drop"], ["clear"]):
            return effects + [Effect("discard", "stash", kind="branch", detail=joined)]
        if sub == "rebase" or (sub == "commit" and "--amend" in rest) or sub == "filter-branch" or sub == "filter-repo":
            return effects + [Effect("history_rewrite", "HEAD", kind="branch", detail=joined)]
        if sub == "commit":
            return effects + [Effect("commit", "HEAD", kind="branch", detail=joined)]
        if sub in ("rm",):
            return effects + [Effect("delete", N(p), detail=joined) for p in rpos]
        if sub in ("mv",):
            return effects + [Effect("move", N(p), detail=joined) for p in rpos[:-1]]
        if sub in ("add", "status", "diff", "log", "show", "fetch", "pull", "merge", "tag", "remote", "init", "switch", "cherry-pick", "revert", "config", "blame", "ls-files", "rev-parse", "describe", "worktree", "submodule"):
            if sub == "pull":
                return effects + [Effect("network", "git pull", kind="url", detail=joined), Effect("run", joined, kind="command", detail=joined)]
            if sub == "tag" and any(a in ("-d", "--delete") for a in rest):
                return effects + [Effect("delete", f"tag/{p}", kind="branch", detail=joined) for p in rpos]
            return effects + [Effect("run", joined, kind="command", detail=joined)]
        return effects + [Effect("run", joined, kind="command", detail=joined)]

    # --- network / install / deploy ---
    if cmd in ("curl", "wget", "http", "https", "nc", "ncat", "telnet", "ssh", "scp", "sftp", "ftp"):
        outputs: list[str] = []
        if cmd in ("curl", "wget"):
            # curl -O means "use the remote file name" and takes no value; wget -O takes the output path
            for flag in (("-o", "--output") if cmd == "curl" else ("-O", "--output-document")):
                if flag in args and args.index(flag) + 1 < len(args):
                    outputs.append(args[args.index(flag) + 1])
        # the URL is the first `scheme://` argument; otherwise the first host-looking argument that is not an output file
        url = next((p for p in pos if "://" in p), None) \
            or next((p for p in pos if p not in outputs and ("." in p or "@" in p or ":" in p)), UNKNOWN)
        effs = [Effect("network", url, kind="url", detail=joined)]
        effs += [Effect("write", N(o), detail=joined) for o in outputs]
        return effects + effs
    if cmd in ("pip", "pip3", "npm", "yarn", "pnpm", "cargo", "gem", "apt", "apt-get", "brew", "poetry", "conda", "go") and pos:
        verb = pos[0]
        if verb in ("install", "add", "i", "get") or (cmd == "go" and verb == "get"):
            return effects + [Effect("install", " ".join(pos[1:]) or UNKNOWN, kind="package", detail=joined),
                              Effect("network", cmd, kind="url", detail=joined)]
        if verb in ("test", "run") and (verb == "test" or (len(pos) > 1 and pos[1] == "test")):
            return effects + [Effect("test", joined, kind="command", detail=joined)]
        if verb in ("publish", "deploy"):
            return effects + [Effect("deploy", joined, kind="command", detail=joined)]
        if verb in ("uninstall", "remove"):
            return effects + [Effect("delete", "package/" + " ".join(pos[1:]), kind="package", detail=joined)]
    if cmd in ("terraform", "tofu", "kubectl", "helm", "aws", "gcloud", "az", "docker", "docker-compose", "fly", "vercel", "heroku", "pulumi", "ansible-playbook", "psql", "mysql", "dropdb", "redis-cli", "mongo", "mongosh"):
        destructive = any(a in ("destroy", "delete", "rm", "down", "drop", "prune", "uninstall", "FLUSHALL", "flushall") for a in args) \
            or cmd == "dropdb" or re.search(r"\b(drop|truncate|delete\s+from)\b", joined, re.I) is not None
        act = "deploy_destroy" if destructive else ("deploy" if any(a in ("apply", "up", "run", "push", "deploy", "exec", "create", "scale") for a in args) else "run")
        return effects + [Effect(act, joined, kind="command", detail=joined, certain=not destructive or True)]

    # --- tests ---
    if TEST_RUNNERS.match(joined) or cmd in ("pytest", "jest", "vitest", "mocha", "rspec", "tox", "nox"):
        return effects + [Effect("test", joined, kind="command", detail=joined)]
    if cmd == "make" and pos[:1] == ["test"]:
        return effects + [Effect("test", joined, kind="command", detail=joined)]

    # --- benign reads ---
    if cmd in ("cat", "ls", "head", "tail", "less", "more", "wc", "grep", "rg", "ag", "echo", "printf", "pwd", "which", "type",
               "file", "stat", "du", "df", "tree", "diff", "cmp", "sort", "uniq", "cut", "awk", "tr", "jq", "yq", "date", "env",
               "printenv", "true", "false", "test", "[", "sleep", "basename", "dirname", "realpath", "readlink", "md5sum", "sha256sum", "cd", "export", "set", "unset", "alias", "history"):
        return effects + [Effect("read", N(pos[0]) if pos and cmd not in ("echo", "printf", "awk", "grep", "rg", "cd", "export", "set", "unset") else ".", detail=joined)]

    # --- unknown executable: could be anything (a script in the repo, a custom tool) ---
    if "/" in words[0] or words[0].startswith("./"):
        return effects + [Effect("exec_opaque", N(words[0]), kind="opaque", detail=joined, certain=False)]
    return effects + [Effect("run", joined, kind="command", detail=joined)]


def classify_command(cmd: str, cwd: str, root: str | None = None, depth: int = 0) -> list[Effect]:
    if not cmd.strip():
        return []
    if depth > 3:
        return [Effect("exec_opaque", UNKNOWN, kind="opaque", detail=cmd, certain=False)]
    tokens = _tokenize(cmd)
    # command substitution / process substitution: contents are opaque
    opaque_sub = bool(re.search(r"\$\(|`|<\(|>\(", cmd))
    out: list[Effect] = []
    for words in _split_pipeline(tokens):
        out.extend(classify_simple(words, cwd, root, depth))
    if opaque_sub:
        # try to classify the inner text too, so `rm $(ls data/)` still shows delete
        for inner in re.findall(r"[$<>]\(([^()]*)\)|`([^`]*)`", cmd):
            s = inner[0] or inner[1]
            if s.strip():
                for e in classify_command(s, cwd, root, depth + 1):
                    e.certain = False
                    out.append(e)
    return out


def is_test_success(command: str, stdout: str, stderr: str) -> bool | None:
    """Heuristic: did a test command succeed? Returns None if the command is not a test runner."""
    joined = command.strip()
    effs = classify_command(joined, "/", "/")
    if not any(e.action == "test" for e in effs):
        return None
    text = (stdout or "") + "\n" + (stderr or "")
    if re.search(r"\b\d+ (?:failed|errors?)\b", text) or re.search(r"\bFAILED\b|\bERROR\b|Error:|failing|✗|✕|Tests:\s+\d+ failed|FAIL\b", text):
        return False
    if re.search(r"\b\d+ passed\b|\bpassed\b|\bPASS\b|Tests:\s+\d+ passed|✓|ok\b|OK\b", text):
        return True
    if "no tests ran" in text or "collected 0 items" in text:
        return False
    return False
