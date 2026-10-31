"""Tests for holdfast.shell: shell command -> effects, and the test-success heuristic."""
import pytest

from holdfast.effects import EVERYTHING, UNKNOWN, normalize_path
from holdfast.shell import classify_command, is_test_success

CWD = "/proj/sub"
ROOT = "/proj"


def fx(cmd, cwd=CWD, root=ROOT):
    return classify_command(cmd, cwd, root)


def acts(cmd, **kw):
    return [e.action for e in fx(cmd, **kw)]


def pairs(cmd, **kw):
    return [(e.action, e.target) for e in fx(cmd, **kw)]


def only(cmd, action, **kw):
    """Effects with the given action."""
    return [e for e in fx(cmd, **kw) if e.action == action]


# ---------------------------------------------------------------- deletion

class TestDelete:
    @pytest.mark.parametrize("cmd,target", [
        ("rm -rf data/", "sub/data"),
        ("rm data/a.txt", "sub/data/a.txt"),
        ("rmdir empty", "sub/empty"),
        ("unlink f.txt", "sub/f.txt"),
        ("shred -u secret.txt", "sub/secret.txt"),
        ("rm -- -weird", "sub/-weird"),
    ])
    def test_single_target(self, cmd, target):
        assert pairs(cmd) == [("delete", target)]

    def test_multiple_targets(self):
        assert pairs("rm data/a.txt data/b.txt") == [("delete", "sub/data/a.txt"), ("delete", "sub/data/b.txt")]

    @pytest.mark.parametrize("cmd", ["rm -rf /", "rm -rf *", "rm -rf .", "rm -rf ./", "rm -rf ~", "rm -rf /*"])
    def test_everything(self, cmd):
        assert pairs(cmd) == [("delete", EVERYTHING)]

    def test_no_args_is_unknown(self):
        assert pairs("rm") == [("delete", UNKNOWN)]

    def test_variable_target_is_unknown(self):
        e, = fx("rm -rf $DIR")
        assert e.action == "delete" and e.target == UNKNOWN

    def test_rsync_delete(self):
        assert ("delete", "sub/dst") in pairs("rsync -a --delete src/ dst/")
        assert ("write", "sub/dst") in pairs("rsync -a --delete src/ dst/")


class TestFind:
    def test_find_delete(self):
        e, = fx("find data -name '*.tmp' -delete")
        assert (e.action, e.target, e.certain) == ("delete", "sub/data", False)

    def test_find_exec_rm(self):
        e, = fx("find data -type f -exec rm {} \\;")
        assert (e.action, e.target) == ("delete", "sub/data")

    def test_find_dot_delete_is_everything(self):
        assert pairs("find . -delete") == [("delete", EVERYTHING)]

    def test_find_without_delete_is_read(self):
        assert acts("find data -name x") == ["read"]


# ---------------------------------------------------------------- mv / cp

class TestMoveCopy:
    def test_mv(self):
        assert pairs("mv a.txt b.txt") == [("move", "sub/a.txt"), ("write", "sub/b.txt")]

    def test_mv_many_into_dir(self):
        assert pairs("mv a b c/") == [("move", "sub/a"), ("move", "sub/b"), ("write", "sub/c")]

    def test_mv_without_args(self):
        e, = fx("mv")
        assert e.action == "move" and e.target == UNKNOWN and not e.certain

    def test_cp_writes_destination_only(self):
        assert pairs("cp a.txt b.txt") == [("write", "sub/b.txt")]
        assert pairs("cp -r src dst") == [("write", "sub/dst")]

    def test_git_mv(self):
        assert pairs("git mv a b") == [("move", "sub/a")]


# ---------------------------------------------------------------- redirects / tee / truncate / dd

class TestRedirects:
    @pytest.mark.parametrize("cmd", ["echo hi > out.txt", "echo hi >> out.txt", "cmd 2> out.txt", "cmd &> out.txt", "cmd &>> out.txt"])
    def test_redirect_is_write(self, cmd):
        assert ("write", "sub/out.txt") in pairs(cmd)

    def test_dev_null_and_fd_dup_are_not_writes(self):
        assert "write" not in acts("cmd > /dev/null")
        assert "write" not in acts("cmd 2>&1")
        assert "write" not in acts("cmd >&2")

    def test_input_redirect_is_not_write(self):
        assert "write" not in acts("cat < in.txt")

    def test_redirect_inside_chain(self):
        assert ("write", "sub/data/x") in pairs("ls && echo y > data/x")

    def test_tee(self):
        assert ("write", "sub/out.txt") in pairs("echo hi | tee out.txt")
        assert pairs("tee -a a.log b.log") == [("write", "sub/a.log"), ("write", "sub/b.log")]

    def test_truncate(self):
        assert ("write", "sub/big.log") in pairs("truncate -s 0 big.log")
        assert pairs("truncate") == [("write", UNKNOWN)]

    def test_dd(self):
        assert pairs("dd if=/dev/zero of=disk.img bs=1M") == [("write", "sub/disk.img")]
        assert pairs("dd if=x") == [("write", UNKNOWN)]

    def test_touch_mkdir_write_chmod_edit(self):
        assert pairs("touch f") == [("write", "sub/f")]
        assert pairs("mkdir d") == [("write", "sub/d")]
        assert ("edit", "sub/f") in pairs("chmod 644 f")


# ---------------------------------------------------------------- sed / perl

class TestInPlaceEdit:
    def test_sed_i_script_positional(self):
        assert pairs("sed -i 's/a/b/' f.py") == [("edit", "sub/f.py")]

    def test_sed_i_with_e(self):
        assert pairs("sed -i -e 's/a/b/' f.py g.py") == [("edit", "sub/f.py"), ("edit", "sub/g.py")]

    def test_sed_i_suffix_and_long_flag(self):
        assert pairs("sed -i.bak 's/a/b/' f.py") == [("edit", "sub/f.py")]
        assert pairs("sed --in-place 's/a/b/' f.py") == [("edit", "sub/f.py")]

    def test_sed_i_no_files_is_unknown_edit(self):
        e, = fx("sed -i")
        assert e.action == "edit" and e.target == UNKNOWN and not e.certain

    def test_sed_without_i_is_read(self):
        assert pairs("sed 's/a/b/' f.py") == [("read", "sub/f.py")]

    @pytest.mark.parametrize("cmd", ["perl -pi -e 's/a/b/' f.py", "perl -i -pe 's/a/b/' f.py", "perl -pi.bak -e 's/a/b/' f.py"])
    def test_perl_pi(self, cmd):
        assert pairs(cmd) == [("edit", "sub/f.py")]

    def test_perl_script_is_opaque(self):
        e, = fx("perl script.pl")
        assert e.action == "exec_opaque" and not e.certain


# ---------------------------------------------------------------- git

class TestGit:
    def test_push_defaults(self):
        e, = fx("git push")
        assert (e.action, e.target, e.kind) == ("push", "origin/HEAD", "branch")

    def test_push_remote_branch(self):
        assert pairs("git push origin main") == [("push", "origin/main")]
        assert pairs("git push -u origin feat") == [("push", "origin/feat")]

    def test_push_refspec(self):
        assert pairs("git push origin HEAD:main") == [("push", "origin/main")]

    @pytest.mark.parametrize("cmd", ["git push -f origin main", "git push --force origin main",
                                     "git push --force-with-lease origin main", "git push origin +main",
                                     "git push --force-if-includes origin main"])
    def test_force_push(self, cmd):
        assert pairs(cmd) == [("force_push", "origin/main")]

    @pytest.mark.parametrize("cmd", ["git push origin --delete feature", "git push -d origin feature"])
    def test_push_delete_branch(self, cmd):
        ps = pairs(cmd)
        assert ("delete", "origin/feature") in ps
        assert all(e.kind == "branch" for e in fx(cmd))

    def test_reset_hard(self):
        assert pairs("git reset --hard") == [("discard", EVERYTHING)]
        assert pairs("git reset --hard HEAD~1") == [("discard", EVERYTHING)]
        assert pairs("git reset --merge") == [("discard", EVERYTHING)]

    def test_reset_soft_is_run(self):
        assert acts("git reset --soft HEAD~1") == ["run"]

    def test_checkout_paths_discard(self):
        e, = fx("git checkout -- src/a.py")
        assert (e.action, e.target) == ("discard", "sub/src/a.py")
        assert pairs("git checkout .") == [("discard", EVERYTHING)]
        assert pairs("git checkout src/a.py") == [("discard", "sub/src/a.py")]

    def test_checkout_branch_is_run(self):
        assert acts("git checkout feature") == ["run"]
        assert acts("git checkout -b new") == ["run"]

    def test_restore(self):
        assert pairs("git restore src/a.py") == [("discard", "sub/src/a.py")]
        assert pairs("git restore .") == [("discard", EVERYTHING)]
        assert pairs("git restore") == [("discard", UNKNOWN)]

    @pytest.mark.parametrize("cmd,target", [("git clean -f", "sub"), ("git clean -fd", "sub"), ("git clean --force", "sub"),
                                            ("git clean -fdx build/", "sub/build")])
    def test_clean_force(self, cmd, target):
        assert pairs(cmd) == [("delete", target)]

    def test_clean_dry_run_is_read(self):
        assert acts("git clean -n") == ["read"]

    @pytest.mark.parametrize("cmd", ["git branch -D feat", "git branch -d feat", "git branch --delete feat"])
    def test_branch_delete(self, cmd):
        e, = fx(cmd)
        assert (e.action, e.target, e.kind) == ("delete", "branch/feat", "branch")

    def test_branch_list_is_run(self):
        assert acts("git branch") == ["run"]

    def test_commit(self):
        e, = fx("git commit -m 'x'")
        assert (e.action, e.target, e.kind) == ("commit", "HEAD", "branch")

    @pytest.mark.parametrize("cmd", ["git commit --amend", "git rebase -i HEAD~3", "git rebase main", "git filter-branch"])
    def test_history_rewrite(self, cmd):
        assert acts(cmd) == ["history_rewrite"]

    @pytest.mark.parametrize("cmd", ["git add .", "git status", "git log", "git diff", "git stash", "git -C /x status"])
    def test_benign_git_is_run(self, cmd):
        assert acts(cmd) == ["run"]

    def test_pull_is_network(self):
        assert "network" in acts("git pull")

    def test_rm_tag_stash(self):
        assert pairs("git rm a.py") == [("delete", "sub/a.py")]
        assert pairs("git tag -d v1") == [("delete", "tag/v1")]
        assert pairs("git stash drop") == [("discard", "stash")]


# ---------------------------------------------------------------- network / install / deploy

class TestNetworkInstall:
    def test_curl(self):
        e, = fx("curl https://example.com")
        assert (e.action, e.target, e.kind) == ("network", "https://example.com", "url")

    def test_curl_output(self):
        ps = pairs("curl -o out.html https://example.com")
        assert ("network", "https://example.com") in ps
        assert ("write", "sub/out.html") in ps

    def test_curl_capital_O_takes_no_value(self):
        assert pairs("curl -O https://x.com/f.tgz") == [("network", "https://x.com/f.tgz")]

    def test_wget(self):
        assert pairs("wget https://x.com/f.tgz") == [("network", "https://x.com/f.tgz")]
        ps = pairs("wget -O f.tgz https://x.com/f.tgz")
        assert ("network", "https://x.com/f.tgz") in ps and ("write", "sub/f.tgz") in ps

    def test_curl_host_port(self):
        assert pairs("curl localhost:8000") == [("network", "localhost:8000")]

    @pytest.mark.parametrize("cmd,pkg", [
        ("pip install requests", "requests"), ("pip3 install -r requirements.txt", "requirements.txt"),
        ("npm install lodash", "lodash"), ("npm i lodash", "lodash"), ("uv pip install x", "x"), ("uv add x", "x"),
        ("python -m pip install x", "x"), ("poetry add x", "x"), ("cargo add serde", "serde"), ("go get x", "x"),
        ("brew install x", "x"), ("apt-get install x", "x"),
    ])
    def test_install(self, cmd, pkg):
        effs = fx(cmd)
        inst = [e for e in effs if e.action == "install"]
        assert len(inst) == 1 and inst[0].target == pkg and inst[0].kind == "package"
        assert "network" in [e.action for e in effs]

    def test_bare_npm_install(self):
        inst, = only("npm install", "install")
        assert inst.target == UNKNOWN

    def test_uninstall_is_delete(self):
        assert pairs("pip uninstall x") == [("delete", "package/x")]
        assert pairs("npm uninstall x") == [("delete", "package/x")]

    def test_publish_is_deploy(self):
        assert acts("npm publish") == ["deploy"]

    def test_infra(self):
        assert acts("terraform destroy") == ["deploy_destroy"]
        assert acts("kubectl delete pod x") == ["deploy_destroy"]
        assert acts("psql -c 'drop table x'") == ["deploy_destroy"]
        assert acts("terraform apply") == ["deploy"]
        assert acts("docker ps") == ["run"]


# ---------------------------------------------------------------- tests / benign

class TestRunners:
    @pytest.mark.parametrize("cmd", ["pytest", "pytest -q tests/", "py.test", "python -m pytest", "python3 -m pytest -x",
                                     "npm test", "npm run test", "yarn test", "go test ./...", "cargo test", "make test",
                                     "python -m unittest", "tox", "uv run pytest", "uv run --with x pytest -q"])
    def test_test_runner(self, cmd):
        assert acts(cmd) == ["test"]

    def test_make_build_is_run(self):
        assert acts("make build") == ["run"]

    @pytest.mark.parametrize("cmd,target", [("ls", "."), ("cat README.md", "sub/README.md"), ("grep -r foo src", "."),
                                            ("echo hello", "."), ("wc -l x", "sub/x"), ("pwd", "."), ("ls -la /tmp", "/tmp")])
    def test_benign_reads(self, cmd, target):
        assert pairs(cmd) == [("read", target)]

    def test_unknown_executable_is_run(self):
        e, = fx("unknowncmd arg")
        assert e.action == "run" and e.kind == "command" and e.certain

    def test_local_script_is_opaque(self):
        e, = fx("./script.sh")
        assert (e.action, e.target, e.certain) == ("exec_opaque", "sub/script.sh", False)
        e, = fx("/usr/bin/foo")
        assert e.action == "exec_opaque" and e.target == "/usr/bin/foo"

    def test_empty_command(self):
        assert fx("") == [] and fx("   ") == []

    def test_unbalanced_quotes_do_not_crash(self):
        assert fx("echo 'unbalanced")


# ---------------------------------------------------------------- chaining / shells / eval / xargs

class TestChaining:
    @pytest.mark.parametrize("cmd", ["ls && rm -rf data", "ls; rm -rf data", "ls || rm -rf data", "cat f | rm -rf data", "ls & rm -rf data"])
    def test_chained(self, cmd):
        assert ("delete", "sub/data") in pairs(cmd)
        assert acts(cmd)[0] == "read"

    def test_subshell_and_group(self):
        assert ("delete", "sub/x") in pairs("(rm x)")
        assert ("delete", "sub/x") in pairs("{ rm x; }")

    def test_newline_separated(self):
        assert ("delete", "sub/x") in pairs("ls\nrm x")
        assert pairs("ls\n\n\nrm x") == [("read", "."), ("delete", "sub/x")]
        assert pairs("ls &&\n rm x") == [("read", "."), ("delete", "sub/x")]
        assert pairs("(\nrm x\n)") == [("delete", "sub/x")]

    def test_newline_inside_quotes_is_not_a_separator(self):
        assert pairs("echo 'a\nb' > f") == [("write", "sub/f"), ("read", ".")]
        assert pairs("python -c \"import os\nos.remove('x')\"") == [("delete", "sub/x")]

    def test_line_continuation(self):
        assert pairs("cat f \\\n  | rm x") == [("read", "sub/f"), ("delete", "sub/x")]

    def test_combined_delete_and_force_push(self):
        assert pairs("rm -rf data && git push -f origin main") == [("delete", "sub/data"), ("force_push", "origin/main")]


class TestShellsEvalXargs:
    @pytest.mark.parametrize("cmd", ["bash -c 'rm -rf data'", "sh -c \"rm -rf data\"", "zsh -c 'rm -rf data'",
                                     "eval rm -rf data", "eval 'rm -rf data'", "sudo bash -c 'rm -rf data'"])
    def test_shell_c_and_eval_recurse(self, cmd):
        assert pairs(cmd) == [("delete", "sub/data")]

    def test_shell_c_force_push(self):
        assert pairs("zsh -c 'git push -f'") == [("force_push", "origin/HEAD")]

    def test_nested_shells(self):
        assert ("delete", "sub/data") in pairs("bash -c \"sh -c 'rm -rf data'\"")

    def test_bash_script_is_opaque(self):
        e, = fx("bash script.sh")
        assert (e.action, e.target, e.certain) == ("exec_opaque", "sub/script.sh", False)

    def test_bare_shell_is_run(self):
        assert acts("bash") == ["run"]

    def test_source_is_opaque(self):
        for cmd in ("source env.sh", ". env.sh"):
            e, = fx(cmd)
            assert e.action == "exec_opaque" and e.target == "sub/env.sh"

    @pytest.mark.parametrize("cmd", ["find . -name '*.pyc' | xargs rm", "ls | xargs rm -rf", "cat list | xargs -n1 rm -f"])
    def test_xargs_rm_is_uncertain_delete(self, cmd):
        d = only(cmd, "delete")
        assert len(d) == 1 and d[0].target == UNKNOWN and not d[0].certain

    def test_bare_xargs_is_opaque(self):
        e, = fx("xargs")
        assert e.action == "exec_opaque" and not e.certain


# ---------------------------------------------------------------- interpreters

class TestInterpreters:
    def test_python_rmtree(self):
        e, = fx("python -c \"import shutil; shutil.rmtree('data')\"")
        assert (e.action, e.target, e.certain) == ("delete", "sub/data", False)

    def test_python_os_remove(self):
        e, = fx("python -c 'import os; os.remove(\"data/x\")'")
        assert (e.action, e.target) == ("delete", "sub/data/x")

    def test_python_open_write(self):
        e, = fx("python -c \"open('out.txt','w').write('x')\"")
        assert (e.action, e.target, e.certain) == ("write", "sub/out.txt", False)

    def test_python_open_append(self):
        e, = fx("python3 -c \"open('log.txt', 'a').write('x')\"")
        assert (e.action, e.target) == ("write", "sub/log.txt")

    def test_python_subprocess_is_opaque(self):
        e, = fx("python -c 'import subprocess; subprocess.run([\"rm\"])'")
        assert e.action == "exec_opaque" and e.target == UNKNOWN

    def test_python_requests_is_network(self):
        e, = fx("python -c 'import requests; requests.get(u)'")
        assert e.action == "network"

    def test_harmless_inline_is_opaque(self):
        e, = fx("python3 -c 'print(1)'")
        assert e.action == "exec_opaque" and e.target == UNKNOWN and not e.certain

    def test_node_rm(self):
        e, = fx("node -e \"require('fs').rmSync('data')\"")
        assert (e.action, e.target) == ("delete", "sub/data")

    def test_ruby_delete(self):
        e, = fx("ruby -e 'File.delete(\"x\")'")
        assert (e.action, e.target) == ("delete", "sub/x")

    def test_script_file_is_opaque(self):
        for cmd in ("python script.py", "python3.12 script.py", "node app.js"):
            e, = fx(cmd)
            assert e.action == "exec_opaque" and e.target.endswith(("script.py", "app.js")) and not e.certain

    def test_bare_interpreter(self):
        e, = fx("python")
        assert e.action == "exec_opaque" and e.target == UNKNOWN

    def test_uv_run_recurses(self):
        assert pairs("uv run python -c \"import shutil; shutil.rmtree('data')\"") == [("delete", "sub/data")]


# ---------------------------------------------------------------- wrappers / substitution

class TestWrappers:
    @pytest.mark.parametrize("cmd", ["sudo rm -rf data", "sudo -u root rm -rf data", "sudo -n -u root rm -rf data",
                                     "env FOO=1 rm -rf data", "FOO=1 BAR=2 rm -rf data", "env -i rm -rf data",
                                     "nohup rm -rf data", "timeout 5 rm -rf data", "timeout -s KILL 5 rm -rf data",
                                     "time rm -rf data", "nice -n 5 rm -rf data", "command rm -rf data", "exec rm -rf data"])
    def test_wrapper_stripped(self, cmd):
        assert pairs(cmd) == [("delete", "sub/data")]

    def test_sudo_pip(self):
        assert "install" in acts("sudo pip install x")


class TestSubstitution:
    def test_command_substitution_inner_is_scanned(self):
        effs = fx("echo `rm x`")
        d = [e for e in effs if e.action == "delete"]
        assert d and d[0].target == "sub/x" and not d[0].certain

    def test_dollar_paren_inner_is_scanned(self):
        effs = fx("echo $(rm -rf data)")
        assert any(e.action == "delete" and e.target == "sub/data" and not e.certain for e in effs)

    def test_rm_with_substituted_args_is_delete(self):
        effs = fx("rm $(ls data/)")
        assert any(e.action == "delete" for e in effs)
        assert all(e.target in (UNKNOWN, "sub/data", "sub/ls") for e in effs if e.action == "delete")

    def test_process_substitution_inner_is_scanned(self):
        effs = fx("cat <(rm -rf data)")
        assert any(e.action == "delete" and e.target == "sub/data" and not e.certain for e in effs)

    def test_depth_limit_yields_opaque(self):
        effs = classify_command("rm x", CWD, ROOT, depth=4)
        assert effs[0].action == "exec_opaque" and not effs[0].certain


# ---------------------------------------------------------------- path normalization

class TestPaths:
    def test_relative_to_cwd_within_root(self):
        assert pairs("rm ./x") == [("delete", "sub/x")]
        assert pairs("rm x") == [("delete", "sub/x")]

    def test_dotdot_within_root(self):
        assert pairs("rm ../x") == [("delete", "x")]
        assert pairs("rm ../other/x") == [("delete", "other/x")]

    def test_dotdot_is_root(self):
        assert pairs("rm ..") == [("delete", ".")]

    def test_escape_root_is_absolute(self):
        assert pairs("rm ../../x") == [("delete", "/x")]
        assert pairs("rm /etc/passwd") == [("delete", "/etc/passwd")]

    def test_absolute_inside_root_is_relativized(self):
        assert pairs("rm /proj/data/x") == [("delete", "data/x")]

    def test_no_root_uses_cwd(self):
        assert pairs("rm data/x", cwd="/w", root=None) == [("delete", "data/x")]

    def test_normalize_path(self):
        assert normalize_path("a/../b", "/p", "/p") == "b"
        assert normalize_path("", "/p/s", "/p") == "s"
        assert normalize_path(".", "/p", "/p") == "."
        assert normalize_path("$HOME/x", "/p", "/p") == UNKNOWN
        assert normalize_path("/p/", "/p/s", "/p") == "."
        assert normalize_path("~", "/p", "/p").startswith("/")


# ---------------------------------------------------------------- is_test_success

class TestIsTestSuccess:
    def test_not_a_test_command(self):
        assert is_test_success("ls", "5 passed", "") is None
        assert is_test_success("echo passed", "passed", "") is None

    @pytest.mark.parametrize("out", ["5 passed in 0.1s", "....  [100%]\n4 passed", "5 passed, 2 skipped in 1s", "Tests: 3 passed, 3 total"])
    def test_pass(self, out):
        assert is_test_success("pytest", out, "") is True

    @pytest.mark.parametrize("out", ["1 failed, 4 passed", "FAILED tests/test_a.py::t", "5 passed, 1 error", "Tests: 1 failed, 2 passed"])
    def test_fail(self, out):
        assert is_test_success("pytest", out, "") is False

    def test_error_in_stderr(self):
        assert is_test_success("pytest", "", "ERROR: file not found") is False

    def test_no_tests_ran(self):
        assert is_test_success("pytest", "no tests ran in 0.01s", "") is False
        assert is_test_success("pytest -q", "collected 0 items", "") is False

    def test_empty_output_is_not_success(self):
        assert is_test_success("pytest", "", "") is False

    def test_go(self):
        assert is_test_success("go test ./...", "ok  pkg 0.1s", "") is True
        assert is_test_success("go test ./...", "FAIL pkg", "") is False

    def test_npm(self):
        assert is_test_success("npm test", "Tests: 3 passed, 3 total", "") is True
        assert is_test_success("npm test", "Tests: 1 failed, 2 passed, 3 total", "") is False
