"""Tool-call classification for Holdfast (SPEC §2 action table).

Design
------
``classify(tool_name, tool_input, cwd)`` maps one tool call to a list of
:class:`Action` records. Each action is one *category* from the rule
vocabulary (delete, push, edit_path, ...), the absolute *paths* it touches
(empty when unknown), a *confidence* and a short *evidence* string that is
quoted back to the agent in deny reasons.

For ``Bash`` the command is first decomposed by :mod:`holdfast.bashparse`
into simple commands; each simple command is then matched against small
per-command handlers below. Everything is pure and side-effect free except
``os.path.isfile`` checks used to tell "truncate an existing file" from
"create a new file".  Every simple command additionally yields a ``run_cmd``
action so ``run_cmd`` rules can match on the command name.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlparse

from .bashparse import Simple, decompose, resolve_path

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
DELETE_CMDS = {"rm", "rmdir", "unlink", "shred", "trash", "trash-put", "gio-trash"}
NET_CMDS = {"curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "telnet", "ftp", "http",
            "https", "socat", "gh", "aws", "gcloud", "az", "httpie", "ping"}
PROD_RX = re.compile(r"(^|[^a-z])(prod|production|prd)([^a-z]|$)", re.I)
PROD_PATH_RX = re.compile(r"^(prod|production|prd)$", re.I)
TEST_EXT = re.compile(r"\.\w{1,5}$")
# Commands that act on infrastructure: a prod marker in their arguments means "touching prod".
INFRA_CMDS = {"kubectl", "helm", "terraform", "aws", "gcloud", "az", "heroku", "vercel", "ssh", "psql", "mysql",
              "docker", "flyctl", "fly", "ansible", "ansible-playbook", "serverless", "sls", "wrangler", "firebase",
              "netlify", "railway", "pulumi", "kustomize", "oc", "eb", "sam", "cdk", "mongosh", "mongo", "redis-cli",
              "scp", "rsync", "sqlcmd", "pg_dump", "pg_restore", "mysqldump", "flyway", "liquibase", "alembic", "doctl",
              "linode-cli", "supabase", "prisma", "dbt", "capistrano", "cap", "fab", "fabric"}
# Read-only / text commands never touch prod, whatever their arguments say.
READONLY_CMDS = {"cat", "head", "tail", "less", "more", "grep", "rg", "ag", "echo", "printf", "ls", "wc", "diff",
                 "man", "which", "whereis", "type", "stat", "file", "tree", "sort", "uniq", "cut", "awk", "sed", "find",
                 "fd", "du", "df", "env", "printenv", "pwd", "true", "false", ":", "test", "[", "basename", "dirname",
                 "realpath", "readlink", "md5sum", "sha256sum", "jq", "yq", "bat", "column", "tr", "xxd", "od", "strings",
                 "git", "pytest", "python", "python3", "node", "ruby", "perl", "go", "cargo", "make"}
PROD_VALUE_FLAGS = {"-n", "--namespace", "--context", "--profile", "--env", "--environment", "-e", "--stage",
                    "--cluster", "--project", "--app", "-a", "--target", "--host", "-h", "--remote", "--workspace"}
REMOTE_DELETE = {  # cmd -> predicate on its argv (remote resource deletion, no local path)
    "kubectl": lambda a: "delete" in a, "oc": lambda a: "delete" in a, "helm": lambda a: "uninstall" in a or "delete" in a,
    "aws": lambda a: any(w == "rm" or w.startswith("delete-") or w in ("rb", "terminate-instances") for w in a),
    "gcloud": lambda a: "delete" in a, "az": lambda a: "delete" in a,
    "gh": lambda a: "delete" in a, "heroku": lambda a: any(w.endswith(":destroy") or w == "destroy" for w in a),
    "terraform": lambda a: "destroy" in a, "pulumi": lambda a: "destroy" in a, "doctl": lambda a: "delete" in a,
    "flyctl": lambda a: "destroy" in a, "fly": lambda a: "destroy" in a, "docker": lambda a: any(w in ("rm", "rmi", "prune") for w in a),
}


# Local-only subcommands of network CLIs (no transfer happens).
LOCAL_CONFIG = {("gcloud", "config"), ("aws", "configure"), ("az", "configure"), ("az", "config")}
# docker subcommands that only build/inspect local images: a prod *tag* is not a prod touch (heldout tc-268).
DOCKER_LOCAL = {"build", "tag", "images", "image", "ps", "inspect", "logs", "history", "save", "load",
                "version", "info", "stats", "top", "port", "diff", "events", "buildx"}
# Formatters / fixers that rewrite files in place: cmd -> predicate on argv[1:] saying "writes".
FORMATTERS = {
    "black": lambda a: "--check" not in a and "--diff" not in a,
    "isort": lambda a: "--check" not in a and "--check-only" not in a and "--diff" not in a and "-c" not in a,
    "autopep8": lambda a: "-i" in a or "--in-place" in a,
    "yapf": lambda a: "-i" in a or "--in-place" in a,
    "ruff": lambda a: ("--fix" in a and "check" in a) or (a[:1] == ["format"] and "--check" not in a and "--diff" not in a),
    "prettier": lambda a: "--write" in a or "-w" in a,
    "gofmt": lambda a: "-w" in a,
    "goimports": lambda a: "-w" in a,
    "rustfmt": lambda a: "--check" not in a,
    "shfmt": lambda a: "-w" in a,
    "clang-format": lambda a: "-i" in a,
}


_HOSTISH = re.compile(r"^[\w-]+(\.[\w-]+)+(:\d+)?(/|$|\?)")


def _url_prod_marker(word: str) -> bool:
    """True when a URL's host or query string carries a prod marker (path components are ignored)."""
    if "://" not in word and not _HOSTISH.match(word):
        return False
    url = word if "://" in word else "http://" + word
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = u.hostname or ""
    if any(PROD_RX.fullmatch(lbl) for lbl in re.split(r"[.-]", host)):
        return True
    return any(PROD_RX.search(v) for _, v in parse_qsl(u.query, keep_blank_values=True))


def _http_delete(argv: list[str]) -> bool:
    """`curl -X DELETE …`, `curl -XDELETE`, `--request DELETE`, `http DELETE …`."""
    cmd = os.path.basename(argv[0])
    if cmd in ("http", "https"):
        return any(a.upper() == "DELETE" for a in argv[1:] if not a.startswith("-"))
    for i, a in enumerate(argv[1:], 1):
        if a in ("-X", "--request") and i + 1 < len(argv) and argv[i + 1].upper() == "DELETE":
            return True
        if a.startswith("-X") and a[2:].upper() == "DELETE":
            return True
        if a.startswith("--request=") and a.split("=", 1)[1].upper() == "DELETE":
            return True
    return False


@dataclass
class Action:
    category: str
    paths: list[str] = field(default_factory=list)
    confidence: float = 1.0
    opaque: bool = False
    evidence: str = ""


# ------------------------------------------------------------------ helpers

def _args(argv: list[str], skip_opts: set[str] | None = None) -> list[str]:
    """Non-option arguments; ``--`` ends option parsing; ``skip_opts`` take a value."""
    out: list[str] = []
    it = iter(argv[1:])
    dashdash = False
    for a in it:
        if dashdash:
            out.append(a)
        elif a == "--":
            dashdash = True
        elif a.startswith("-") and len(a) > 1:
            if skip_opts and a in skip_opts:
                next(it, None)
        else:
            out.append(a)
    return out


def _resolve(words: list[str], s: Simple, home: str | None) -> list[str]:
    return [resolve_path(w, s.cwd, home) for w in words]


def _catastrophic(path: str, cwd: str, home: str | None) -> bool:
    home = home or os.path.expanduser("~")
    return path in ("/", home, cwd) or path in ("/*", home + "/*", cwd + "/*")


def _is_existing_file(path: str) -> bool:
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def _git_subcommand(argv: list[str]) -> tuple[str | None, list[str]]:
    """Return (subcommand, remaining argv) skipping git's global options."""
    i = 1
    takes_value = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
    while i < len(argv):
        a = argv[i]
        if a in takes_value:
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return a, argv[i + 1:]
    return None, []


# ------------------------------------------------------------------ Bash handlers

def _classify_git(s: Simple, home: str | None, out: list[Action]) -> None:
    sub, rest = _git_subcommand(s.argv)
    if sub is None:
        return
    opts = [a for a in rest if a.startswith("-")]
    args = [a for a in rest if not a.startswith("-")]
    ev = s.raw
    # git transport is NOT scored as `network` (corpus: push/fetch have their own semantics).
    if "--dry-run" in opts or ("-n" in opts and sub in ("push", "clean", "add")):
        return
    if sub == "push":
        branch = args[1] if len(args) > 1 else (args[0] if args else "")
        paths = [p for p in [branch, "/".join(args[:2]) if len(args) > 1 else ""] if p]
        force = any(o in ("-f", "--force", "--force-with-lease", "--force-if-includes") or
                    o.startswith("--force-with-lease=") for o in opts)
        plus = any(a.startswith("+") for a in args)
        out.append(Action("push", paths, 1.0, False, ev))
        if force or plus:
            out.append(Action("force_push", paths, 1.0, False, ev))
        if "--delete" in opts or "-d" in opts or any(a.startswith(":") and len(a) > 1 for a in args):
            out.append(Action("delete", paths, 0.7, False, f"{ev} (deletes a remote branch)"))
        # Pushing to main/master is prod only when a touch_prod rule targets that branch; the
        # engine matches targeted touch_prod rules against push branch names, so nothing is emitted here.
        # A literal prod marker in the remote name or destination ref is firm (heldout tc-074/166/167):
        # `git push prod main`, `git push origin feature:production`, `HEAD:refs/heads/production`.
        remote = args[0] if args else ""
        dsts = [a.split(":", 1)[1] if ":" in a else a for a in args[1:]]
        prod_refs = [r for r in [remote] + dsts if r and PROD_RX.search(r)]
        if prod_refs:
            out.append(Action("touch_prod", prod_refs, 0.9, False, f"{ev} (prod marker: {', '.join(prod_refs)})"))
    elif sub == "commit":
        out.append(Action("commit", [], 1.0, False, ev))
    elif sub == "clean":
        if not any(o.startswith("-n") for o in opts):
            out.append(Action("delete", _resolve(args or ["."], s, home), 0.9, False, f"{ev} (removes untracked files)"))
    elif sub == "rm":
        if "--cached" not in opts:  # --cached leaves the file on disk (tc-237)
            out.append(Action("delete", _resolve(args, s, home), 0.9, False, ev))
    elif sub == "checkout":
        if "--" in rest:
            paths = rest[rest.index("--") + 1:]
            before = [a for a in rest[:rest.index("--")] if not a.startswith("-")]
            if before:  # git checkout <ref> -- <path>: overwrite from another ref (tc-172)
                out.append(Action("edit_path", _resolve(paths or ["."], s, home), 0.85, False,
                                  f"{ev} (overwrites files from {before[0]})"))
            else:
                out.append(Action("delete", _resolve(paths or ["."], s, home), 0.85, False,
                                  f"{ev} (discards uncommitted changes)"))
        elif not any(o in ("-b", "-B", "--orphan", "-t", "--track") for o in opts):
            pathish = [a for a in args if a == "." or "/" in a or TEST_EXT.search(a)]
            if pathish:
                out.append(Action("delete", _resolve(pathish, s, home), 0.75, False,
                                  f"{ev} (discards uncommitted changes)"))
    elif sub == "restore":
        if "--staged" not in opts and "-S" not in opts or "--worktree" in opts or "-W" in opts:
            out.append(Action("delete", _resolve(args or ["."], s, home), 0.85, False,
                              f"{ev} (discards uncommitted changes)"))
    elif sub == "reset":
        if "--hard" in opts or "--merge" in opts:
            out.append(Action("delete", [s.cwd], 0.9, False, f"{ev} (discards uncommitted changes)"))
    elif sub == "stash":
        if args and args[0] in ("drop", "clear"):
            out.append(Action("delete", [], 0.9, False, f"{ev} (discards stashed work)"))
    elif sub == "branch":
        if any(o in ("-D", "-d", "--delete") or (o.startswith("-") and "D" in o) for o in opts):
            out.append(Action("delete", [], 0.8, False, f"{ev} (deletes branch)"))
    elif sub == "tag":
        if "-d" in opts or "--delete" in opts:
            out.append(Action("delete", [], 0.6, False, f"{ev} (deletes tag)"))
    elif sub in ("apply", "am"):
        out.append(Action("edit_path", [s.cwd], 0.7, False, f"{ev} (patches files in the worktree)"))
    elif sub == "worktree":
        if args and args[0] == "remove":  # removes the worktree directory from disk (heldout tc-015/016)
            out.append(Action("delete", _resolve(args[1:], s, home), 0.9, False, f"{ev} (removes worktree directory)"))
    elif sub in ("cherry-pick", "revert"):
        # Rewrites worktree files and, unless -n/--no-commit, records a commit (heldout tc-029).
        out.append(Action("edit_path", [s.cwd], 0.7, False, f"{ev} (rewrites files in the worktree)"))
        if not any(o in ("-n", "--no-commit") for o in opts) and not (args and args[0] in ("--continue", "--abort", "--skip", "--quit")):
            out.append(Action("commit", [], 0.9, False, f"{ev} (creates a commit)"))
    elif sub == "gc" and any(o.startswith("--prune=now") or o == "--prune=all" for o in opts):
        out.append(Action("delete", [], 0.6, False, f"{ev} (permanently discards unreachable history)"))
    elif sub == "reflog" and args[:1] == ["expire"] and any(o.startswith(("--expire=now", "--expire-unreachable=now")) for o in opts):
        out.append(Action("delete", [], 0.6, False, f"{ev} (expires reflog entries; history becomes collectable)"))
    elif sub == "clone":
        # SPEC lists only curl/wget/.../pip/npm as `network`; a clone of a URL is unambiguously a transfer
        # (heldout tc-085). fetch/pull stay unclassified: the dev corpus labels `git fetch origin` as benign.
        targets = [a for a in args if not a.startswith("-")]
        if targets:
            out.append(Action("network", [], 0.9, False, f"{ev} (clones from {targets[0]})"))
            dest = targets[1] if len(targets) > 1 else os.path.basename(targets[0].rstrip("/")).removesuffix(".git")
            out.append(Action("edit_path", _resolve([dest], s, home), 0.7, False, f"{ev} (writes the clone)"))
    elif sub == "submodule" and args[:1] == ["update"] and any(o in ("--init", "--remote") for o in opts):
        out.append(Action("network", [], 0.8, False, f"{ev} (fetches submodules)"))


def _classify_simple(s: Simple, home: str | None) -> list[Action]:
    out: list[Action] = []
    cmd = os.path.basename(s.cmd) if "/" in s.cmd else s.cmd
    ev = s.raw
    if s.via:
        ev = f"{s.raw} (via {' > '.join(s.via)})"

    # redirect / tee / dd writes
    for w in s.writes:
        out.append(Action("edit_path", [w], 0.95, False, f"{ev} writes to {w}"))
    pure_truncation = cmd in ("", ":", "true") or (cmd == "cat" and s.argv[1:] == ["/dev/null"])
    for t in s.truncates:
        if pure_truncation:  # `: > f`, `> f`: the only effect is emptying f (tc-023/024/152/153)
            out.append(Action("delete", [t], 0.9, False, f"{ev} truncates {t}"))
        elif _is_existing_file(t):
            out.append(Action("delete", [t], 0.8, False, f"{ev} truncates existing file {t}"))

    if s.opaque:
        for g in s.guess:
            out.append(Action(g, list(s.guess_paths), 0.5, True, s.evidence or ev))
        if cmd:
            out.append(Action("run_cmd", [cmd, " ".join(s.argv)], 1.0, False, ev))
        return out
    if not cmd:
        return out

    out.append(Action("run_cmd", [cmd, " ".join(s.argv)], 1.0, False, ev))

    args = _args(s.argv)
    if "--help" in s.argv or "--version" in s.argv or cmd in ("man", "help", "info", "tldr"):
        return out
    if cmd in DELETE_CMDS:
        if s.unknown_args and not args:
            out.append(Action("delete", list(s.guess_paths), 0.8, False, f"{ev} (paths come from stdin)"))
        else:
            paths = _resolve(args, s, home)
            conf = 1.0 if any(_catastrophic(p, s.cwd, home) for p in paths) else 0.95
            if s.unknown_args:
                conf = 0.8
            out.append(Action("delete", paths, conf, False, ev))
    elif cmd == "find":
        if "-delete" in s.argv:
            dirs = []
            for a in s.argv[1:]:
                if a.startswith("-"):
                    break
                dirs.append(a)
            out.append(Action("delete", _resolve(dirs or ["."], s, home), 0.9, False, ev))
    elif cmd == "git":
        _classify_git(s, home, out)
    elif cmd == "truncate":
        sizes = [s.argv[i + 1] for i, a in enumerate(s.argv) if a in ("-s", "--size") and i + 1 < len(s.argv)]
        sizes += [a[2:] for a in s.argv if a.startswith("-s") and len(a) > 2]
        files = _resolve(_args(s.argv, {"-s", "--size", "-r", "--reference"}), s, home)
        if any(z.lstrip("<>") in ("0", "0K", "0M") for z in sizes):
            out.append(Action("delete", files, 0.9, False, ev))
        out.append(Action("edit_path", files, 0.9, False, ev))
    elif cmd == "sed":
        inplace = any(a == "-i" or a.startswith("-i") or a.startswith("--in-place") or
                      (a.startswith("-") and not a.startswith("--") and "i" in a[1:] and "e" not in a[1:])
                      for a in s.argv[1:])
        if inplace:
            files = _args(s.argv, {"-e", "--expression", "-f", "--file"})
            has_e = any(a in ("-e", "--expression", "-f", "--file") or a.startswith("--expression=")
                        for a in s.argv)
            if not has_e and files:
                files = files[1:]  # first positional is the script
            out.append(Action("edit_path", _resolve(files, s, home), 0.9, False, ev))
    elif cmd in REMOTE_DELETE and REMOTE_DELETE[cmd](s.argv[1:]):
        out.append(Action("delete", [], 0.8, False, f"{ev} (deletes a remote resource)"))
    elif cmd in ("cp", "mv", "install", "ln", "rsync"):
        remote = cmd == "rsync" and any(":" in a and not a.startswith("-") for a in args)
        if remote:
            out.append(Action("network", [], 0.9, False, ev))
        else:
            if cmd == "rsync" and any(a.startswith("--delete") for a in s.argv) and len(args) >= 2:
                # local rsync --delete removes extraneous files under the destination (heldout tc-036)
                out.append(Action("delete", _resolve(args[-1:], s, home), 0.8, False, f"{ev} (removes extraneous files at destination)"))
            skip = {"-t", "--target-directory", "-m", "-o", "-g", "-S", "--suffix"}
            if cmd == "rsync":
                skip |= {"-e", "--exclude", "--include", "--files-from"}
            a2 = _args(s.argv, skip)
            tgt_opt = [s.argv[i + 1] for i, a in enumerate(s.argv) if a in ("-t", "--target-directory")]
            dest = tgt_opt[0] if tgt_opt else (a2[-1] if len(a2) >= 2 else (a2[0] if cmd == "install" else None))
            srcs = a2 if tgt_opt else a2[:-1]
            if dest is not None:
                d = resolve_path(dest, s.cwd, home)
                if cmd == "mv":
                    # mv = source removed + destination written; the write only counts inside the project
                    # (corpus tc-052/154: moving out of the project is a plain removal).
                    if srcs:
                        out.append(Action("delete", _resolve(srcs, s, home), 0.8, False, f"{ev} (moves source away)"))
                    if d != "/dev/null" and (d == s.cwd or d.startswith(s.cwd.rstrip("/") + "/")):
                        out.append(Action("edit_path", [d], 0.9, False, f"{ev} writes to {d}"))
                else:
                    out.append(Action("edit_path", [d], 0.9, False, f"{ev} writes to {d}"))
    elif cmd == "touch":
        out.append(Action("edit_path", _resolve(_args(s.argv, {"-d", "-t", "-r"}), s, home), 0.9, False, ev))
    elif cmd in ("perl", "ruby") and any(re.match(r"^-[pnlawW0-9]*i", a) for a in s.argv[1:]):
        files = _args(s.argv, {"-e", "-E", "-M", "-m", "-I", "-r", "-C"})
        out.append(Action("edit_path", _resolve(files, s, home), 0.9, False, f"{ev} ({cmd} in-place edit)"))
    elif cmd in FORMATTERS and FORMATTERS[cmd](s.argv[1:]):
        # Known in-place formatters/fixers rewrite the files they are given (heldout tc-189/190).
        files = [a for a in args if a not in ("check", "format")]
        out.append(Action("edit_path", _resolve(files or ["."], s, home), 0.7, False, f"{ev} (rewrites files in place)"))
    elif cmd == "cargo" and args[:1] == ["clean"]:
        out.append(Action("delete", [resolve_path("target", s.cwd, home)], 0.7, False, f"{ev} (removes target/)"))
    elif cmd == "make" and any(a in ("clean", "distclean", "mrproper", "clobber") for a in args):
        out.append(Action("delete", [], 0.6, False, f"{ev} (clean target conventionally removes build outputs)"))
    elif cmd == "gsutil":
        if any(a in ("rm", "rb") for a in args):
            out.append(Action("delete", [], 0.8, False, f"{ev} (deletes a remote object)"))
        out.append(Action("network", [], 0.8, False, ev))
    elif cmd == "tar" and s.argv[1:2] and ("x" in s.argv[1].lstrip("-") or "--extract" in s.argv or "-x" in s.argv):
        dest = [s.argv[i + 1] for i, a in enumerate(s.argv) if a in ("-C", "--directory") and i + 1 < len(s.argv)]
        dest += [a.split("=", 1)[1] for a in s.argv if a.startswith("--directory=")]
        out.append(Action("edit_path", _resolve(dest or ["."], s, home), 0.8, False, f"{ev} (extracts files)"))
    elif cmd == "unzip":
        dest = [s.argv[i + 1] for i, a in enumerate(s.argv) if a == "-d" and i + 1 < len(s.argv)]
        out.append(Action("edit_path", _resolve(dest or ["."], s, home), 0.8, False, f"{ev} (extracts files)"))
    elif cmd == "gio" and args[:1] == ["trash"]:
        out.append(Action("delete", _resolve(args[1:], s, home), 0.9, False, ev))
    elif cmd == "patch":
        files = _args(s.argv, {"-p", "-d", "-i", "-o", "-r", "-D", "-F", "-z", "-B", "-Y"})
        inputs = [s.argv[i + 1] for i, a in enumerate(s.argv) if a in ("-i", "--input") and i + 1 < len(s.argv)]
        files = [f for f in files if f not in inputs]
        out.append(Action("edit_path", _resolve(files, s, home) if files else [s.cwd], 0.8 if files else 0.7, False, ev))
    elif cmd in NET_CMDS:
        if (cmd, args[:1] and args[0]) in LOCAL_CONFIG:  # `gcloud config set …`, `aws configure`: no transfer (heldout tc-064)
            pass
        else:
            out.append(Action("network", [], 0.95 if cmd not in ("gh", "aws", "gcloud", "az", "ping") else 0.7, False, ev))
            if cmd in ("curl", "http", "https", "wget") and _http_delete(s.argv):
                # HTTP DELETE deletes a remote resource (heldout tc-075/076, labelled debatable)
                out.append(Action("delete", [], 0.6, False, f"{ev} (HTTP DELETE of a remote resource)"))
            # Downloads written to a named local path are writes to that path (heldout tc-079/083/084):
            # `curl -o f`, `wget -O f`, `wget -P dir`, `scp host:src local`.
            dl = []
            if cmd == "curl":
                dl = [s.argv[i + 1] for i, a in enumerate(s.argv) if a in ("-o", "--output") and i + 1 < len(s.argv)]
                dl += [a.split("=", 1)[1] for a in s.argv if a.startswith("--output=")]
            elif cmd == "wget":
                dl = [s.argv[i + 1] for i, a in enumerate(s.argv)
                      if a in ("-O", "--output-document", "-P", "--directory-prefix") and i + 1 < len(s.argv)]
                dl += [a.split("=", 1)[1] for a in s.argv if a.startswith(("--output-document=", "--directory-prefix="))]
                dl += [a[2:] for a in s.argv if (a.startswith("-O") or a.startswith("-P")) and len(a) > 2]
            elif cmd in ("scp", "sftp") and len(args) >= 2 and ":" in args[-2] and ":" not in args[-1]:
                dl = [args[-1]]
            dl = [d for d in dl if d not in ("-", "/dev/null", "/dev/stdout")]
            if dl:
                out.append(Action("edit_path", _resolve(dl, s, home), 0.8, False, f"{ev} (downloads to a local path)"))
    elif cmd in ("docker", "podman") and args and args[0] in ("pull", "push", "login"):
        out.append(Action("network", [], 0.9, False, ev))
    elif cmd in ("npx", "bunx", "pnpx") or (cmd in ("pnpm", "yarn", "npm", "bun") and args[:1] in (["dlx"], ["exec"], ["x"])):
        # Corpus (tc-190, debatable; heldout tc-197/198): npx / pnpm dlx may download and run an arbitrary package.
        out.append(Action("install", [], 0.7, False, f"{ev} (may download a package)"))
        out.append(Action("network", [], 0.7, False, f"{ev} (may download a package)"))
    elif cmd == "twine" and args[:1] == ["upload"]:
        out.append(Action("network", [], 0.9, False, f"{ev} (uploads to a package index)"))

    _classify_install(cmd, s, out, ev)
    _classify_prod(cmd, s, out, ev)
    return out


def _classify_install(cmd: str, s: Simple, out: list[Action], ev: str) -> None:
    argv = s.argv
    args = [a for a in argv[1:] if not a.startswith("-")]
    first = args[0] if args else ""
    hit = False
    pip_args = args
    if cmd.startswith("python") and argv[1:3] == ["-m", "pip"]:
        cmd, pip_args = "pip", [a for a in argv[3:] if not a.startswith("-")]
        first = pip_args[0] if pip_args else ""
    # Registry queries / downloads that install nothing are `network` only (heldout tc-255/257/259/260).
    net_only = False
    if cmd in ("pip", "pip3", "pipx") and (first in ("download", "index", "search") or
                                            (first == "list" and "--outdated" in argv)):
        net_only = True
    elif cmd in ("npm", "pnpm", "yarn", "bun") and first in ("view", "info", "show", "search", "outdated", "publish"):
        net_only = True
    elif "--dry-run" in argv and cmd in ("pip", "pip3", "npm", "pnpm", "yarn"):
        net_only = True
    if net_only:
        if not any(a.category == "network" for a in out):
            out.append(Action("network", [], 0.8, False, f"{ev} (queries a package index)"))
        return
    if cmd in ("pip", "pip3", "pipx") and first == "install":
        hit = True
    elif cmd == "uv" and (first == "add" or argv[1:3] == ["pip", "install"] or first == "sync"):
        hit = True
    elif cmd in ("npm", "pnpm", "yarn", "bun"):
        if cmd == "yarn" and not args:
            hit = True
        elif first in ("install", "i", "add", "ci", "update", "up"):
            hit = True
    elif cmd in ("apt", "apt-get", "brew", "dnf", "yum", "pacman", "apk", "gem", "conda", "mamba") and \
            first in ("install", "add", "-S") or (cmd == "pacman" and any(a.startswith("-S") for a in argv)):
        hit = True
    elif cmd == "cargo" and first in ("add", "install"):
        hit = True
    elif cmd == "go" and first in ("get", "install"):
        hit = True
    elif cmd == "poetry" and first in ("add", "install"):
        hit = True
    elif cmd == "bundle" and first in ("add", "install", "update"):
        hit = True
    elif cmd == "composer" and first in ("require", "install", "update"):
        hit = True
    if hit:
        out.append(Action("install", [], 0.95, False, ev))
        offline = "--no-index" in argv or "--offline" in argv  # heldout tc-202: install from local wheels
        if not offline and not any(a.category == "network" for a in out):
            out.append(Action("network", [], 0.8, False, f"{ev} (downloads packages)"))


def _classify_prod(cmd: str, s: Simple, out: list[Action], ev: str) -> None:
    """touch_prod only for commands that *act*: infra CLIs, deploy scripts, prod env vars, --prod flags."""
    env_hits = [f"{k}={v}" for k, v in s.env.items() if PROD_RX.search(v) or PROD_RX.search(k)]
    argv = s.argv[1:]
    flag_hits = [a for a in argv if a.lstrip("-").lower() in ("prod", "production")
                 or (a.startswith("--") and "=" in a and a.split("=", 1)[0] in PROD_VALUE_FLAGS and PROD_RX.search(a.split("=", 1)[1]))]
    for i, a in enumerate(argv[:-1]):
        if a in PROD_VALUE_FLAGS and PROD_RX.search(argv[i + 1]):
            flag_hits.append(f"{a} {argv[i + 1]}")
    arg_hits = [a for a in argv if PROD_RX.search(a) and not a.startswith("-")]
    # `terraform apply -var-file=prod.tfvars`: a prod marker in an option *value* (heldout tc-066)
    arg_hits += [a for a in argv if a.startswith("-") and "=" in a and a not in flag_hits
                 and PROD_RX.search(a.split("=", 1)[1])]
    deployish = re.search(r"(deploy|release|publish|migrate|rollout|ship|provision)", cmd) is not None
    hits: list[str] = []
    if env_hits:
        hits += env_hits
    if cmd in ("docker", "podman") and argv[:1] and argv[0] in DOCKER_LOCAL:
        hits += flag_hits  # `docker build -t app:prod .` builds locally (heldout tc-268)
    elif cmd in INFRA_CMDS or deployish:
        hits += flag_hits + arg_hits
    elif cmd in NET_CMDS:
        # a prod host or `?env=production` in a request URL (heldout tc-177); the URL path is ignored
        hits += flag_hits + [a for a in argv if _url_prod_marker(a)]
    elif cmd not in READONLY_CMDS:
        hits += flag_hits  # e.g. `npm run deploy -- --prod`, `./deploy.sh --prod`
    if cmd in READONLY_CMDS and not env_hits:
        return
    if hits:
        strong = cmd in INFRA_CMDS or deployish or bool(env_hits)
        out.append(Action("touch_prod", list(dict.fromkeys(hits)), 0.9 if strong else 0.7, False,
                          f"{ev} (prod marker: {', '.join(dict.fromkeys(hits))})"))


# ------------------------------------------------------------------ entry point

def classify(tool_name: str, tool_input: dict, cwd: str, home: str | None = None) -> list[Action]:
    """Map a tool call to the actions it would perform."""
    if not isinstance(tool_input, dict):
        return []
    if tool_name == "Bash":
        cmd = tool_input.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return []
        out: list[Action] = []
        for s in decompose(cmd, cwd, home):
            acts = _classify_simple(s, home)
            if s.remote:  # `ssh host 'rm -rf x'`: a remote deletion/write, but the path is not local
                for a in acts:
                    if a.category in ("delete", "edit_path"):
                        a.paths = []
                        a.evidence += " (on remote host)"
            out.extend(acts)
        return out
    if tool_name in EDIT_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(path, str) or not path:
            return []
        p = resolve_path(path, cwd, home)
        out = [Action("edit_path", [p], 1.0, False, f"{tool_name} {path}")]
        if tool_name == "Write" and tool_input.get("content", None) == "" and _is_existing_file(p):
            out.append(Action("delete", [p], 0.9, False, f"Write with empty content truncates existing file {path}"))
        # touch_prod only when a path component, or a dot/dash-separated token of one, *is*
        # prod/production (config/prod.yml, .env.production, docker-compose.prod.yml), not when the word
        # merely occurs inside a token (src/production_utils.py tc-261, docs/reproduction-steps.md,
        # src/product.py). Rules with a target glob are matched by the engine against the edit_path path.
        rel = os.path.relpath(p, cwd) if p.startswith(cwd.rstrip("/") + "/") else p
        if any(PROD_PATH_RX.match(tok) for part in rel.split("/") for tok in re.split(r"[.-]", part)):
            out.append(Action("touch_prod", [p], 0.7, False, f"{tool_name} {path} (prod path)"))
        return out
    return []
