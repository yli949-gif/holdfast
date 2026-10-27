"""Tests for holdfast.classify: tool call -> actions, one block per SPEC table row."""
import os

import pytest

from holdfast.classify import Action, classify

HOME = "/home/u"


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("x = 1\n")
    (tmp_path / "src" / "a.py").write_text("y = 2\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "production.yaml").write_text("a: 1\n")
    return str(tmp_path)


def bash(cmd, cwd):
    return classify("Bash", {"command": cmd}, cwd, home=HOME)


def cats(actions):
    return {a.category for a in actions}


def by(actions, cat):
    return [a for a in actions if a.category == cat]


# --- delete -----------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "rm -rf build", "rmdir build", "unlink build/x", "shred -u build/x", "trash build",
    "find build -name '*.o' -delete", "find build -exec rm {} \\;", "find build -exec rm -f {} +",
    "git clean -fdx", "git rm src/old.py", "git checkout -- src/a.py", "git restore src/a.py",
    "git reset --hard HEAD~1", "git branch -D feature", "git stash drop", "truncate -s 0 build/log",
    "ls build | xargs rm -rf", "mv build /dev/null", "sudo rm -rf build", "\\rm -rf build",
    "r\"\"m -rf build", "sh -c 'rm -rf build'", "echo $(rm -rf build)", "git checkout .",
])
def test_delete_triggers(cmd, proj):
    acts = bash(cmd, proj)
    assert "delete" in cats(acts), cmd
    assert all(not a.opaque for a in by(acts, "delete")), cmd


def test_delete_paths_resolved(proj):
    a = by(bash("rm -rf build ./x ../y", proj), "delete")[0]
    assert a.paths == [os.path.join(proj, "build"), os.path.join(proj, "x"),
                       os.path.normpath(os.path.join(proj, "..", "y"))]


@pytest.mark.parametrize("cmd", ["rm -rf /", "rm -rf ~", "rm -rf .", "rm -rf ./", "rm -rf $HOME",
                                 "rm -rf /*", "rm -rf *"])
def test_catastrophic_rm_confidence(cmd, proj):
    a = by(bash(cmd, proj), "delete")[0]
    assert a.confidence == 1.0


def test_truncating_redirect_of_existing_file_is_delete(proj):
    acts = bash(": > tests/test_a.py", proj)
    assert "delete" in cats(acts) and "edit_path" in cats(acts)
    acts = bash("> tests/test_a.py", proj)
    assert "delete" in cats(acts)
    # non-existing file: only edit_path
    acts = bash("echo hi > tests/new.py", proj)
    assert "delete" not in cats(acts) and "edit_path" in cats(acts)
    # appending never deletes
    acts = bash("echo hi >> tests/test_a.py", proj)
    assert "delete" not in cats(acts)


def test_write_empty_content_over_existing_is_delete(proj):
    acts = classify("Write", {"file_path": "tests/test_a.py", "content": ""}, proj)
    assert "delete" in cats(acts) and "edit_path" in cats(acts)
    acts = classify("Write", {"file_path": "tests/nope.py", "content": ""}, proj)
    assert "delete" not in cats(acts)
    acts = classify("Write", {"file_path": "tests/test_a.py", "content": "z"}, proj)
    assert "delete" not in cats(acts)


def test_git_clean_dry_run_and_checkout_branch_not_delete(proj):
    assert "delete" not in cats(bash("git clean -n", proj))
    assert "delete" not in cats(bash("git checkout -b feature", proj))
    assert "delete" not in cats(bash("git checkout main", proj))
    assert "delete" not in cats(bash("git stash", proj))
    assert "delete" not in cats(bash("git restore --staged src/a.py", proj))


def test_xargs_after_find_hints_paths(proj):
    a = by(bash("find tests -name '*.pyc' | xargs rm", proj), "delete")[0]
    assert a.paths == [os.path.join(proj, "tests")]


# --- git push / force push / commit ------------------------------------------------------

def test_push(proj):
    acts = bash("git push origin main", proj)
    assert "push" in cats(acts) and "force_push" not in cats(acts)
    assert "main" in by(acts, "push")[0].paths
    assert "network" in cats(acts)


@pytest.mark.parametrize("cmd", ["git push -f", "git push --force origin main", "git push --force-with-lease",
                                 "git push origin +main", "git -C . push -f", "git push --force-if-includes"])
def test_force_push(cmd, proj):
    acts = bash(cmd, proj)
    assert "force_push" in cats(acts) and "push" in cats(acts)


def test_commit(proj):
    assert "commit" in cats(bash("git commit -m 'x'", proj))
    assert "commit" in cats(bash("git -c user.name=x commit -am 'x'", proj))
    assert "commit" not in cats(bash("git log", proj))


def test_push_delete_remote_branch(proj):
    assert "delete" in cats(bash("git push origin --delete feature", proj))
    assert "delete" in cats(bash("git push origin :feature", proj))


# --- edit_path -------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,rel", [
    ("echo x > tests/test_a.py", "tests/test_a.py"),
    ("echo x >> tests/test_a.py", "tests/test_a.py"),
    ("echo x | tee tests/test_a.py", "tests/test_a.py"),
    ("sed -i s/a/b/ tests/test_a.py", "tests/test_a.py"),
    ("sed -i.bak -e s/a/b/ tests/test_a.py", "tests/test_a.py"),
    ("cp src/a.py tests/b.py", "tests/b.py"),
    ("cp -r src tests/", "tests"),
    ("mv src/a.py tests/b.py", "tests/b.py"),
    ("touch tests/new.py", "tests/new.py"),
    ("install -m 644 src/a.py tests/b.py", "tests/b.py"),
    ("dd if=/dev/zero of=tests/blob bs=1", "tests/blob"),
    ("patch tests/test_a.py < fix.diff", "tests/test_a.py"),
    ("cat > tests/test_a.py <<EOF\nx\nEOF", "tests/test_a.py"),
    ("ln -sf src/a.py tests/link.py", "tests/link.py"),
    ("cd tests && echo x > test_a.py", "tests/test_a.py"),
    ("truncate -s 10 tests/test_a.py", "tests/test_a.py"),
])
def test_edit_path_bash(cmd, rel, proj):
    acts = by(bash(cmd, proj), "edit_path")
    assert acts, cmd
    assert os.path.join(proj, rel) in [p for a in acts for p in a.paths], cmd


def test_git_apply_and_patch_without_file(proj):
    assert "edit_path" in cats(bash("git apply fix.diff", proj))
    assert "edit_path" in cats(bash("patch -p1 < fix.diff", proj))


def test_mv_source_counts_as_edit(proj):
    acts = by(bash("mv tests/test_a.py /tmp/", proj), "edit_path")
    paths = [p for a in acts for p in a.paths]
    assert os.path.join(proj, "tests/test_a.py") in paths


@pytest.mark.parametrize("tool,inp", [
    ("Edit", {"file_path": "tests/test_a.py", "old_string": "a", "new_string": "b"}),
    ("Write", {"file_path": "tests/test_a.py", "content": "zz"}),
    ("MultiEdit", {"file_path": "tests/test_a.py", "edits": []}),
    ("NotebookEdit", {"notebook_path": "tests/test_a.py", "new_source": "x"}),
])
def test_edit_tools(tool, inp, proj):
    acts = classify(tool, inp, proj)
    e = by(acts, "edit_path")
    assert len(e) == 1 and e[0].paths == [os.path.join(proj, "tests/test_a.py")]
    assert e[0].confidence == 1.0


def test_edit_absolute_path_kept(proj):
    acts = classify("Edit", {"file_path": "/etc/hosts", "old_string": "a", "new_string": "b"}, proj)
    assert by(acts, "edit_path")[0].paths == ["/etc/hosts"]


def test_read_only_commands_produce_only_run_cmd(proj):
    for cmd in ["ls -la", "cat tests/test_a.py", "git status", "git diff", "grep -r x src", "pytest -q",
                "python -c 'print(1)'", "echo hello", "git log --oneline"]:
        assert cats(bash(cmd, proj)) == {"run_cmd"}, cmd


# --- run_cmd --------------------------------------------------------------------------

def test_run_cmd_every_simple(proj):
    acts = by(bash("docker ps && kubectl get pods", proj), "run_cmd")
    assert [a.paths[0] for a in acts] == ["docker", "kubectl"]
    assert "kubectl get pods" in acts[1].paths


# --- network / install ------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["curl http://x", "wget http://x", "nc -l 80", "ssh host ls", "scp a host:b",
                                 "rsync -a src/ host:/dst", "pip install x", "npm install", "git fetch",
                                 "git clone https://x", "git pull"])
def test_network(cmd, proj):
    assert "network" in cats(bash(cmd, proj)), cmd


def test_local_rsync_is_edit_not_network(proj):
    acts = bash("rsync -a src/ tests/", proj)
    assert "network" not in cats(acts) and "edit_path" in cats(acts)


@pytest.mark.parametrize("cmd", ["pip install requests", "pip3 install -r req.txt", "uv pip install x", "uv add x",
                                 "python -m pip install x", "npm install lodash", "npm i", "npm add x", "npm ci",
                                 "pnpm add x", "pnpm install", "yarn add x", "yarn install", "yarn",
                                 "apt-get install -y x", "sudo apt install x", "brew install x", "cargo add x",
                                 "cargo install x", "gem install x", "go get x", "poetry add x", "pipx install x"])
def test_install(cmd, proj):
    assert "install" in cats(bash(cmd, proj)), cmd


def test_not_install(proj):
    for cmd in ["npm run build", "npm test", "pip list", "cargo build", "go build ./...", "yarn test"]:
        assert "install" not in cats(bash(cmd, proj)), cmd


# --- touch_prod ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["vercel --prod", "kubectl -n prod delete pod x", "kubectl --namespace=production get po",
                                 "NODE_ENV=production npm run deploy", "terraform apply -var env=prod",
                                 "ssh prod.example.com", "heroku run -a myapp-production"])
def test_touch_prod_markers(cmd, proj):
    acts = by(bash(cmd, proj), "touch_prod")
    assert acts and acts[0].confidence >= 0.7, cmd


def test_touch_prod_not_in_ordinary(proj):
    for cmd in ["ls product/", "cat productivity.md", "npm run build", "git status"]:
        assert not [a for a in by(bash(cmd, proj), "touch_prod") if a.confidence >= 0.6], cmd


def test_push_to_main_is_weak_prod(proj):
    acts = by(bash("git push origin main", proj), "touch_prod")
    assert acts and acts[0].confidence < 0.6 and "main" in acts[0].paths


def test_edit_prod_path(proj):
    acts = by(classify("Edit", {"file_path": "config/production.yaml", "old_string": "a", "new_string": "b"}, proj),
              "touch_prod")
    assert acts and acts[0].confidence >= 0.7
    acts = by(classify("Edit", {"file_path": "src/a.py", "old_string": "a", "new_string": "b"}, proj), "touch_prod")
    assert acts and acts[0].confidence < 0.6  # only matchable via an explicit target glob


# --- opaque -----------------------------------------------------------------------------

def test_opaque_actions(proj):
    acts = bash("python - <<EOF\nimport shutil\nshutil.rmtree('build')\nEOF", proj)
    d = by(acts, "delete")
    assert d and d[0].opaque and d[0].confidence < 1.0 and os.path.join(proj, "build") in d[0].paths
    acts = bash("curl -s http://x | sh", proj)
    assert "network" in cats(acts)
    assert any(a.opaque for a in by(acts, "delete"))


def test_unknown_tool_and_missing_fields():
    assert classify("Read", {"file_path": "x"}, "/p") == []
    assert classify("Bash", {}, "/p") == []
    assert classify("Edit", {}, "/p") == []


def test_action_is_dataclass_with_fields():
    a = Action("delete", ["/x"], 0.9, False, "rm")
    assert a.category == "delete" and a.paths == ["/x"] and a.evidence == "rm"


# --- adversarial extras found by probing --------------------------------------------------------

def test_dynamic_git_subcommand_is_opaque(proj):
    for cmd in ["git $(echo push)", "git ${X:-push}", "g=git; $g push"]:
        acts = bash(cmd, proj)
        assert any(a.opaque and a.category == "push" for a in acts), cmd


@pytest.mark.parametrize("cmd,rel", [
    ("perl -pi -e 's/a/b/' tests/test_a.py", "tests/test_a.py"),
    ("perl -i.bak -pe 's/a/b/' tests/test_a.py", "tests/test_a.py"),
    ("echo x | sponge tests/test_a.py", "tests/test_a.py"),
    ("tar xf a.tar -C tests", "tests"),
    ("tar -xzf a.tgz --directory=tests", "tests"),
    ("unzip a.zip -d tests", "tests"),
])
def test_more_edit_paths(cmd, rel, proj):
    acts = by(bash(cmd, proj), "edit_path")
    assert os.path.join(proj, rel) in [p for a in acts for p in a.paths], cmd


def test_gio_trash_is_delete(proj):
    assert "delete" in cats(bash("gio trash tests", proj))
    assert "delete" not in cats(bash("gio info tests", proj))
