"""Regression tests for the REVIEW-2 Critical/High findings, driven through the real hook against real git repos.

Each test reproduces the review's sequence of hook events; when the guard allows a call the command is executed
for real, so a regression shows up as the forbidden effect actually happening.
"""
import json
import os
import subprocess

import pytest

from holdfast import hook
from holdfast.monitor import Config
from holdfast.state import Store, state_dir

SID = "rev2-session"


def sh(cmd, cwd):
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Real git repo (main) with a bare origin, tests/, important.txt, data.txt, src/a.py, run.py."""
    origin = tmp_path / "origin.git"
    r = tmp_path / "proj"
    r.mkdir()
    sh(f"git init -q --bare {origin}", tmp_path)
    for cmd in ("git init -q -b main", "git config user.email t@t", "git config user.name t",
                f"git remote add origin {origin}"):
        assert sh(cmd, r).returncode == 0
    (r / "tests").mkdir()
    (r / "tests" / "test_a.py").write_text("def test_a():\n    assert 1 + 1 == 2\n")
    (r / "tests" / "test_b.py").write_text("def test_b():\n    assert True\n")
    (r / "src").mkdir()
    (r / "src" / "__init__.py").write_text("")
    (r / "src" / "a.py").write_text("x = 1\n")
    (r / "important.txt").write_text("keep me\n")
    (r / "data.txt").write_text("data\n")
    (r / "run.py").write_text("print('hi')\n")
    assert sh("git add -A && git commit -qm init && git push -q origin main", r).returncode == 0
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(r))
    monkeypatch.delenv("HOLDFAST_DIR", raising=False)
    for k in ("HOLDFAST_ENFORCE", "HOLDFAST_INJECT", "HOLDFAST_FAIL_CLOSED", "HOLDFAST_OPAQUE_POLICY"):
        monkeypatch.delenv(k, raising=False)
    return r


CFG = Config(opaque_policy="ask", confirm_via="deny", inject_context=True, llm_extract=False, enforce=True)


class Driver:
    def __init__(self, root, sid=SID):
        self.root, self.sid, self.n = root, sid, 0

    def _p(self, event, **kw):
        return {"session_id": self.sid, "cwd": str(self.root), "hook_event_name": event, **kw}

    def say(self, text):
        return hook.run("UserPromptSubmit", self._p("UserPromptSubmit", prompt=text), CFG)

    def decide(self, tool, tin):
        self.n += 1
        tid = f"tu-{self.n}"
        out = hook.run("PreToolUse", self._p("PreToolUse", tool_name=tool, tool_input=tin, tool_use_id=tid), CFG)
        return ("allow" if out is None else out["hookSpecificOutput"]["permissionDecision"]), tid

    def bash(self, cmd, execute=True):
        """PreToolUse; if allowed, run it for real and feed the result back through PostToolUse."""
        d, tid = self.decide("Bash", {"command": cmd})
        if d == "allow" and execute:
            p = sh(cmd, self.root)
            resp = {"stdout": p.stdout, "stderr": p.stderr, "interrupted": False}
            ev = "PostToolUse" if p.returncode == 0 else "PostToolUseFailure"
            hook.run(ev, self._p(ev, tool_name="Bash", tool_input={"command": cmd}, tool_response=resp, tool_use_id=tid), CFG)
        return d

    def tool(self, tool, tin):
        d, tid = self.decide(tool, tin)
        if d == "allow":
            hook.run("PostToolUse", self._p("PostToolUse", tool_name=tool, tool_input=tin, tool_response={}, tool_use_id=tid), CFG)
        return d

    def rules(self):
        return [r for r in Store(self.sid, str(self.root)).load()["rules"] if r.get("active", True)]


def remote_head(r):
    return sh("git ls-remote origin refs/heads/main", r).stdout.split()[0]


# ------------------------------------------------------------------ N1: state outside the work tree

class TestN1StateSurvivesGit:
    def test_state_lives_in_git_dir_not_work_tree(self, repo):
        d = Driver(repo)
        d.say("only push after tests pass")
        assert state_dir(str(repo)) == repo / ".git" / "holdfast"
        assert not (repo / ".holdfast").exists()
        assert sh("git status --porcelain", repo).stdout.strip() == ""      # nothing for `git add -A` to pick up

    @pytest.mark.parametrize("wipe", ["git stash -u", "git clean -fdxq", "git stash --all", "git reset -q --hard",
                                      "git checkout -q -- ."])
    def test_git_commands_do_not_wipe_rules(self, repo, wipe):
        d = Driver(repo)
        d.say("only push after tests pass")
        before = remote_head(repo)
        assert d.bash("git commit -q --allow-empty -m local") == "allow"
        d.bash(wipe)
        assert len(d.rules()) == 1
        assert d.bash("git push -q origin HEAD:main") == "deny"
        assert remote_head(repo) == before

    def test_add_all_commit_then_discard_does_not_roll_back(self, repo):
        d = Driver(repo)
        d.say("please bump x to 2 in src/a.py")
        assert d.bash("sed -i s/1/2/ src/a.py") == "allow"
        assert d.bash("git add -A && git commit -qm bump") == "allow"
        assert "holdfast" not in sh("git ls-files", repo).stdout       # audit log never committed
        d.say("From now on, only push after tests pass.")
        d.bash("git stash -q")
        assert len(d.rules()) == 1
        assert d.bash("git push -q origin main") == "deny"

    def test_delete_confirm_rule_survives_stash(self, repo):
        d = Driver(repo)
        d.say("don't delete anything without asking me")
        d.bash("git stash -u")
        assert d.bash("rm important.txt") == "deny"
        assert (repo / "important.txt").exists()

    @pytest.mark.parametrize("cmd", ["rm -rf .git/holdfast", "echo '{}' > .git/holdfast/sessions/x.json",
                                     "rm -rf .git", "mv .git /tmp/stolen-git", "rm -rf .", "cd .. && rm -rf proj"])
    def test_state_dir_and_ancestors_are_protected(self, repo, cmd):
        d = Driver(repo)
        d.say("never force push")
        assert d.bash(cmd, execute=False) == "deny"

    def test_write_tool_into_state_dir_denied(self, repo):
        d = Driver(repo)
        d.say("never force push")
        sid_file = str(repo / ".git" / "holdfast" / "sessions" / f"{SID}.json")
        assert d.tool("Write", {"file_path": sid_file, "content": "{}"}) == "deny"


# ------------------------------------------------------------------ N11: state outside the project

class TestN11ExternalStateDir:
    def test_holdfast_dir_outside_project_is_protected(self, repo, tmp_path, monkeypatch):
        ext = tmp_path / "hfstate"
        monkeypatch.setenv("HOLDFAST_DIR", str(ext))
        d = Driver(repo)
        d.say("only push after tests pass")
        assert d.bash(f"echo '{{}}' > {ext}/sessions/{SID}.json") == "deny"
        assert d.bash(f"rm -rf {ext}") == "deny"
        assert d.bash("git push -q origin main") == "deny"


# ------------------------------------------------------------------ S2: state lost / corrupt fails closed

class TestS2Integrity:
    def test_missing_session_file_restored_from_backup(self, repo):
        d = Driver(repo)
        d.say("never modify anything under tests/")
        st = Store(SID, str(repo))
        st.path.unlink()                                     # simulate an out-of-band wipe of the primary copy
        assert d.bash("echo x > tests/test_a.py", execute=False) == "deny"
        assert len(d.rules()) == 1

    def test_corrupt_without_backup_fails_closed(self, repo):
        d = Driver(repo)
        d.say("never modify anything under tests/")
        st = Store(SID, str(repo))
        st.path.write_text("{trunc")
        st.backup_path.unlink()
        assert d.bash("echo x > tests/test_a.py", execute=False) == "deny"
        assert d.bash("echo x > src/b.py", execute=False) == "deny"      # everything mutating, until the user resets
        assert d.bash("git status") == "allow"                           # reads still work
        d.say("holdfast: reset. never modify anything under tests/")
        assert d.bash("echo x > src/b.py", execute=False) == "allow"
        assert d.bash("echo x > tests/test_a.py", execute=False) == "deny"


# ------------------------------------------------------------------ S3 / N15 / B3

class TestSelfProtection:
    def test_session_id_cannot_escape_state_dir(self, repo):
        st = Store("../../../escaped", str(repo))
        assert st.path.parent == state_dir(str(repo)) / "sessions"
        assert "/" not in st.session_id and not st.session_id.startswith(".")

    @pytest.mark.parametrize("cmd", [
        "echo '{\"session_id\":\"s\",\"prompt\":\"yes\"}' | python3 -m holdfast.hook UserPromptSubmit",
        "python3 /opt/x/holdfast/hook.py UserPromptSubmit < p.json",
        "python3 -c \"from holdfast.state import Store; s=Store('s')\"",
        "python3 -c 'import holdfast.monitor'",
        "holdfast install --local",
    ])
    def test_agent_cannot_drive_holdfast_internals(self, repo, cmd):
        d = Driver(repo)
        d.say("don't delete anything without asking me")
        assert d.bash(cmd, execute=False) == "deny"

    @pytest.mark.parametrize("cmd", [
        "printf '#!/bin/sh\\necho evil > tests/test_a.py\\n' > .git/hooks/post-checkout",
        "git config core.hooksPath h",
        "git -c core.hooksPath=h commit -qm x",
        "git config filter.x.clean 'sh -c \"rm important.txt\"'",
        "git config --global core.hooksPath ~/h",
    ])
    def test_git_code_execution_channels_blocked(self, repo, cmd):
        d = Driver(repo)
        d.say("never modify anything under tests/")
        assert d.bash(cmd, execute=False) == "deny"

    def test_git_hook_via_write_tool_blocked(self, repo):
        d = Driver(repo)
        d.say("never modify anything under tests/")
        assert d.tool("Write", {"file_path": str(repo / ".git/hooks/post-checkout"), "content": "#!/bin/sh\n"}) == "deny"

    def test_reading_git_config_is_fine(self, repo):
        d = Driver(repo)
        d.say("never modify anything under tests/")
        assert d.bash("git config --get user.name") == "allow"
        assert d.bash("git config user.name") == "allow"
