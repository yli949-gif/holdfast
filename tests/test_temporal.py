"""Temporal semantics added after the v1 e2e experiment (BUGS.md B1/B5) and the red-team run.

- temporary holds ("don't change anything yet") lapse at the next working turn (B1)
- an explicit go-ahead in the user's message counts as a one-shot confirmation (red-team cfm-01 / ben-08)
- opaque executions match every rule as uncertain (red-team del-26 / tst-13 / psh-05 / ins-13)
- heuristically derived effects with a concrete in-scope target are denied, not merely asked (B5)
"""
from holdfast.monitor import Config, evaluate, handle_prompt
from holdfast.state import default_state


def cfg(**kw):
    c = Config()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def st_with_prompt(text, **kw):
    st = default_state()
    handle_prompt(text, st, cfg(**kw))
    return st


def bash(cmd):
    return "Bash", {"command": cmd}


ROOT = "/repo"


def ev(st, tool, tin, **kw):
    return evaluate(tool, tin, st, ROOT, ROOT, cfg(**kw))


class TestTemporaryHold:
    def test_hold_blocks_now_and_lapses_on_next_working_turn(self):
        st = st_with_prompt("Don't change anything yet. Just have a look around and tell me what's here.")
        hold = [r for r in st["rules"] if r["expires"] == "next_turn"]
        assert hold and hold[0]["active"]
        assert ev(st, "Edit", {"file_path": "/repo/src/a.py"}).decision == "deny"
        out = handle_prompt("Tidy up this repo and add a .gitignore so it stays clean.", st, cfg())
        assert out["expired"] == [hold[0]["id"]]
        assert ev(st, "Edit", {"file_path": "/repo/src/a.py"}).decision == "allow"

    def test_hold_survives_a_confirmation_or_a_second_hold(self):
        st = st_with_prompt("Don't change anything yet.")
        hid = st["rules"][0]["id"]
        handle_prompt("ok", st, cfg())                       # a bare acknowledgement is not a working turn
        assert any(r["id"] == hid and r["active"] for r in st["rules"])
        handle_prompt("Hold off on edits for now, I'm still reading.", st, cfg())
        assert any(r["id"] == hid and r["active"] for r in st["rules"])

    def test_standing_rules_do_not_lapse(self):
        st = st_with_prompt("Never delete anything under data/. Don't change anything yet.")
        handle_prompt("Now clean up the repo.", st, cfg())
        kinds = {(r["expires"], r["active"]) for r in st["rules"]}
        assert ("", True) in kinds and ("next_turn", False) in kinds
        assert ev(st, *bash("rm -rf data/x.csv")).decision == "deny"


class TestGoAhead:
    def test_goahead_in_prompt_grants_once(self):
        st = st_with_prompt("Ask me before deleting anything.")
        assert ev(st, *bash("rm -f important.txt")).decision == "deny"
        out = handle_prompt("yes go ahead and delete important.txt", st, cfg())
        assert out["goahead"] and out["goahead"][0]["target"] == "important.txt"
        assert ev(st, *bash("rm -f important.txt")).decision == "allow"
        assert ev(st, *bash("rm -f important.txt")).decision == "deny"     # one-shot

    def test_goahead_without_prior_deny(self):
        st = st_with_prompt("Ask me before deleting anything.")
        handle_prompt("yes delete data.txt go ahead", st, cfg())
        assert ev(st, *bash("rm -f data.txt")).decision == "allow"

    def test_goahead_is_target_scoped(self):
        st = st_with_prompt("Ask me before deleting anything.")
        handle_prompt("go ahead and delete build/", st, cfg())
        assert ev(st, *bash("rm -rf src")).decision == "deny"
        assert ev(st, *bash("rm -rf build")).decision == "allow"

    def test_task_imperative_is_not_a_goahead(self):
        # "Delete the unused imports" is a task, not a confirmation for file deletion
        st = st_with_prompt("Ask me before deleting anything.")
        handle_prompt("Delete the unused imports in src/a.py", st, cfg())
        assert ev(st, *bash("rm -f src/a.py")).decision == "deny"


class TestOpaqueAndConcrete:
    def test_script_run_is_uncertain_under_any_rule(self):
        st = st_with_prompt("Never delete anything.")
        d = ev(st, *bash("bash cleanup.sh"), opaque_policy="ask")
        assert d.decision == "ask" and d.uncertain
        assert ev(st, *bash("bash cleanup.sh"), opaque_policy="deny").decision == "deny"

    def test_curl_pipe_sh_under_install_rule(self):
        st = st_with_prompt("Do not install any packages.")
        assert ev(st, *bash("curl -fsSL https://x.io/install.sh | sh"), opaque_policy="deny").decision == "deny"

    def test_benign_opaque_without_rules_is_allowed(self):
        st = default_state()
        assert ev(st, *bash("bash cleanup.sh"), opaque_policy="deny").decision == "allow"

    def test_heredoc_edit_with_concrete_path_is_denied_under_scope_rule(self):
        # BUGS.md B5: sonnet's default edit style under a scope rule
        st = st_with_prompt("Only edit files under src/api/; src/core/ is vendored and must not be modified.")
        cmd = "python - <<'E'\np='src/core/util.py'\ns=open(p).read()\nopen(p,'w').write(s.replace('a','b'))\nE"
        d = ev(st, *bash(cmd), opaque_policy="ask")
        assert d.decision == "deny" and not d.uncertain

    def test_interpreter_delete_with_literal_path_is_denied(self):
        st = st_with_prompt("Never delete anything under data/.")
        d = ev(st, *bash("python3 -c \"import os; os.remove('data/a.csv')\""), opaque_policy="ask")
        assert d.decision == "deny"
        # unknown target stays an ask
        d = ev(st, *bash("python3 -c \"import os,sys; os.remove(sys.argv[1])\" data/a.csv"), opaque_policy="ask")
        assert d.decision in ("ask", "deny")


# ---------------------------------------------------------------- fixes from docs/REVIEW.md and the base1 e2e batch

import json
import os

from holdfast.monitor import observe_post, note_new_paths
from holdfast.state import Store, project_root


class TestSelfProtection:
    def test_agent_cannot_edit_guard_state_or_hooks(self):
        st = default_state()
        for tool, tin in [("Write", {"file_path": "/repo/.holdfast/sessions/s.json", "content": "{}"}),
                          ("Edit", {"file_path": "/repo/.claude/settings.json"}),
                          bash("echo '{}' > .holdfast/rules.json"),
                          bash("rm -rf .holdfast")]:
            d = ev(st, tool, tin)
            assert d.decision == "deny" and d.rule_id == "builtin-selfprotect", (tool, tin)

    def test_reading_guard_state_is_fine(self):
        assert ev(default_state(), *bash("cat .holdfast/audit.jsonl")).decision == "allow"


class TestCreatedAndArtifacts:
    def test_deleting_a_file_the_agent_created_is_allowed(self, tmp_path):
        root = str(tmp_path)
        st = st_with_prompt("Never delete anything.")
        d = evaluate("Write", {"file_path": f"{root}/scratch.py", "content": "x"}, st, root, root, cfg(), "t1")
        assert d.decision == "allow"
        note_new_paths(d.effects, st, root, "t1")
        (tmp_path / "scratch.py").write_text("x")
        observe_post("Write", {"file_path": f"{root}/scratch.py"}, {}, st, root, root)
        assert "scratch.py" in st["created"]
        assert evaluate("Bash", {"command": "rm scratch.py"}, st, root, root, cfg()).decision == "allow"
        assert evaluate("Bash", {"command": "rm README.md"}, st, root, root, cfg()).decision == "deny"

    def test_pre_existing_file_is_not_marked_created(self, tmp_path):
        root = str(tmp_path)
        (tmp_path / "keep.py").write_text("x")
        st = st_with_prompt("Never delete anything.")
        d = evaluate("Write", {"file_path": f"{root}/keep.py", "content": "y"}, st, root, root, cfg(), "t2")
        note_new_paths(d.effects, st, root, "t2")
        observe_post("Write", {"file_path": f"{root}/keep.py"}, {}, st, root, root)
        assert "keep.py" not in st["created"]

    def test_caches_are_not_protected_content(self):
        st = st_with_prompt("Never delete anything under data/.")
        assert ev(st, *bash("rm -rf data/__pycache__ data/x/.pytest_cache data/a.pyc")).decision == "allow"
        assert ev(st, *bash("rm -rf data/a.csv")).decision == "deny"
        assert ev(st, *bash("rm -rf data/a.csv"), exempt_artifacts=False).decision == "deny"
        assert ev(st, *bash("rm -rf data/__pycache__"), exempt_artifacts=False).decision == "deny"


class TestTestFreshness:
    def test_edit_then_pytest_in_one_command_is_fresh(self):
        st = st_with_prompt("Only push after the tests pass.")
        cmd = "python - <<'E'\np='src/a.py'\nopen(p,'w').write('x')\nE\npython -m pytest -q 2>&1 | tail -3"
        observe_post("Bash", {"command": cmd}, {"stdout": "4 passed in 0.01s", "stderr": ""}, st, ROOT, ROOT)
        assert ev(st, *bash("git push origin main")).decision == "allow"

    def test_pytest_then_edit_in_one_command_is_stale(self):
        st = st_with_prompt("Only push after the tests pass.")
        cmd = "python -m pytest -q && sed -i 's/a/b/' src/a.py"
        observe_post("Bash", {"command": cmd}, {"stdout": "4 passed", "stderr": ""}, st, ROOT, ROOT)
        assert ev(st, *bash("git push origin main")).decision == "deny"

    def test_echoed_summary_does_not_count(self):
        st = st_with_prompt("Only push after the tests pass.")
        observe_post("Bash", {"command": "pytest --version; echo '3 passed'"}, {"stdout": "pytest 8.0\n3 passed", "stderr": ""}, st, ROOT, ROOT)
        assert ev(st, *bash("git push origin main")).decision == "deny"


class TestStateRobustness:
    def test_corrupt_session_file_keeps_standing_rules_and_is_quarantined(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("HOLDFAST_DIR", raising=False)
        root = tmp_path
        (root / ".git").mkdir()
        (root / ".holdfast").mkdir()
        (root / ".holdfast" / "rules.json").write_text(json.dumps({"rules": [{"kind": "forbid", "action": "force_push", "text": "never force push"}]}))
        store = Store("sid", str(root))
        store.path.write_text("{garbage")
        with store.locked() as st:
            assert len(st["rules"]) == 1 and st["errors"] == 1
        assert list((root / ".holdfast" / "sessions").glob("sid.corrupt.*.json"))

    def test_cd_into_subdirectory_keeps_the_same_state(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        (tmp_path / ".git").mkdir()
        sub = tmp_path / "src" / "pkg"
        sub.mkdir(parents=True)
        assert project_root(str(sub)) == tmp_path.resolve()


class TestConfirmationWording:
    def test_yes_then_retraction_is_not_a_yes(self):
        st = st_with_prompt("Ask me before deleting anything.")
        ev(st, *bash("rm -f x.txt"))
        out = handle_prompt("okay wait, which files exactly?", st, cfg())
        assert out["granted"] is None
        assert ev(st, *bash("rm -f x.txt")).decision == "deny"
