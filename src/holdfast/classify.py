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

from .bashparse import Simple, decompose, resolve_path

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
DELETE_CMDS = {"rm", "rmdir", "unlink", "shred", "trash", "trash-put", "gio-trash"}
NET_CMDS = {"curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "telnet", "ftp", "http",
            "https", "socat", "gh", "aws", "gcloud", "az", "httpie", "ping"}
PROD_RX = re.compile(r"(^|[^a-z])(prod|production|prd)([^a-z]|$)", re.I)
PROD_PATH_RX = re.compile(r"^(prod|production|prd)([.-]|$)", re.I)
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
    elif cmd == "perl" and any(re.match(r"^-[pnlaw0-9]*i", a) for a in s.argv[1:]):
        files = _args(s.argv, {"-e", "-E", "-M", "-m", "-I"})
        out.append(Action("edit_path", _resolve(files, s, home), 0.9, False, f"{ev} (perl in-place edit)"))
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
        out.append(Action("network", [], 0.95 if cmd not in ("gh", "aws", "gcloud", "az", "ping") else 0.7, False, ev))
    elif cmd in ("docker", "podman") and args and args[0] in ("pull", "push", "login"):
        out.append(Action("network", [], 0.9, False, ev))
    elif cmd in ("npx", "bunx", "pnpx"):
        # Corpus (tc-190, debatable): npx may download and run an arbitrary package.
        out.append(Action("install", [], 0.7, False, f"{ev} (may download a package)"))
        out.append(Action("network", [], 0.7, False, f"{ev} (may download a package)"))

    _classify_install(cmd, s, out, ev)
    _classify_prod(cmd, s, out, ev)
    return out


def _classify_install(cmd: str, s: Simple, out: list[Action], ev: str) -> None:
    argv = s.argv
    args = [a for a in argv[1:] if not a.startswith("-")]
    first = args[0] if args else ""
    hit = False
    if cmd in ("pip", "pip3", "pipx") and first in ("install", "download"):
        hit = True
    elif cmd.startswith("python") and argv[1:3] == ["-m", "pip"] and "install" in argv:
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
    if hit:
        out.append(Action("install", [], 0.95, False, ev))
        if not any(a.category == "network" for a in out):
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
    deployish = re.search(r"(deploy|release|publish|migrate|rollout|ship|provision)", cmd) is not None
    hits: list[str] = []
    if env_hits:
        hits += env_hits
    if cmd in INFRA_CMDS or deployish:
        hits += flag_hits + arg_hits
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
            out.extend(_classify_simple(s, home))
        return out
    if tool_name in EDIT_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(path, str) or not path:
            return []
        p = resolve_path(path, cwd, home)
        out = [Action("edit_path", [p], 1.0, False, f"{tool_name} {path}")]
        if tool_name == "Write" and tool_input.get("content", None) == "" and _is_existing_file(p):
            out.append(Action("delete", [p], 0.9, False, f"Write with empty content truncates existing file {path}"))
        # touch_prod only when a path component or filename *is* prod/production (config/prod.yml), not
        # when the word merely occurs (src/production_utils.py, tc-261). Rules with a target glob are
        # matched by the engine against the edit_path path instead.
        rel = os.path.relpath(p, cwd) if p.startswith(cwd.rstrip("/") + "/") else p
        if any(PROD_PATH_RX.match(part) for part in rel.split("/")):
            out.append(Action("touch_prod", [p], 0.7, False, f"{tool_name} {path} (prod path)"))
        return out
    return []
