"""Tests for holdfast.bashparse: decomposition, unquoting, path resolution."""
import pytest

from holdfast.bashparse import decompose, resolve_path

CWD = "/proj"
HOME = "/home/u"


def argvs(cmd):
    return [s.argv for s in decompose(cmd, CWD, HOME)]


def first(cmd):
    return decompose(cmd, CWD, HOME)[0]


# --- splitting -----------------------------------------------------------------

def test_split_on_operators_and_newlines():
    assert argvs("rm -rf build && git push; echo a || echo b | cat\nls") == [
        ["rm", "-rf", "build"], ["git", "push"], ["echo", "a"], ["echo", "b"],
        ["cat"], ["ls"]]


def test_separators_recorded():
    segs = decompose("curl x | sh", CWD, HOME)
    assert segs[1].sep == "|" and segs[1].prev is segs[0]


def test_comments_and_subshells():
    assert argvs("(cd x && rm y) # comment ; rm z") == [["cd", "x"], ["rm", "y"]]
    assert argvs("{ rm a; rm b; }") == [["rm", "a"], ["rm", "b"]]


def test_background_amp():
    assert argvs("sleep 1 & rm x") == [["sleep", "1"], ["rm", "x"]]


# --- unquoting -------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ['r""m -rf x', "\\rm -rf x", "'r'm -rf x", "r'm' -rf x",
                                 '"rm" -rf x', "$'rm' -rf x"])
def test_quoting_tricks_unquote_to_rm(cmd):
    assert first(cmd).argv[:1] == ["rm"]


def test_quoted_operator_is_not_separator():
    assert argvs('echo "a; b" && echo \'c | d\'') == [["echo", "a; b"], ["echo", "c | d"]]


def test_unbalanced_quote_does_not_crash():
    segs = decompose('echo "unterminated', CWD, HOME)
    assert segs and segs[0].argv[0] == "echo"


# --- wrappers --------------------------------------------------------------------

def test_strip_sudo_env_time_nohup():
    s = first("sudo -u root env FOO=1 BAR=2 time nohup rm -rf x")
    assert s.argv == ["rm", "-rf", "x"]
    assert s.env == {"FOO": "1", "BAR": "2"}


def test_leading_assignment_kept_in_env():
    s = first("NODE_ENV=production npm run build")
    assert s.argv == ["npm", "run", "build"] and s.env["NODE_ENV"] == "production"


def test_timeout_and_nice():
    assert first("timeout 30 nice -n 5 rm x").argv == ["rm", "x"]


@pytest.mark.parametrize("cmd", ['sh -c "rm -rf /tmp/x; echo hi"', "bash -c 'rm -rf /tmp/x && echo hi'",
                                 'bash -lc "rm -rf /tmp/x; echo hi"', 'eval "rm -rf /tmp/x; echo hi"',
                                 "eval rm -rf /tmp/x '; echo hi'"])
def test_sh_c_and_eval_are_descended(cmd):
    av = argvs(cmd)
    assert ["rm", "-rf", "/tmp/x"] in av and ["echo", "hi"] in av


def test_nested_sh_c():
    av = argvs("""sh -c 'bash -c "rm x"'""")
    assert ["rm", "x"] in av


def test_command_substitution_and_backticks():
    av = argvs("echo $(rm x) `rm y`")
    assert ["rm", "x"] in av and ["rm", "y"] in av
    assert av[-1][0] == "echo"


def test_nontrivial_substitution_as_command_name_is_opaque():
    s = [s for s in decompose("$(cat cmd.txt) -rf x", CWD, HOME) if s.argv and s.argv[0] != "cat"][0]
    assert s.opaque


def test_unknown_variable_command_name_is_opaque():
    segs = decompose("$c -rf x", CWD, HOME)
    assert any(s.opaque for s in segs)


def test_xargs():
    segs = decompose("find . -name '*.log' | xargs -0 -n1 rm -f", CWD, HOME)
    s = segs[-1]
    assert s.argv[:2] == ["rm", "-f"] and s.unknown_args


def test_xargs_with_I():
    s = decompose("ls | xargs -I{} rm {}", CWD, HOME)[-1]
    assert s.argv[0] == "rm" and s.unknown_args


def test_find_exec_and_delete():
    segs = decompose("find ./tmp -name '*.pyc' -exec rm -f {} \\;", CWD, HOME)
    rm = [s for s in segs if s.argv[0] == "rm"][0]
    assert rm.argv == ["rm", "-f", "/proj/tmp"]
    assert rm.via and "find -exec" in rm.via
    f = first("find . -name '*.pyc' -delete")
    assert "-delete" in f.argv


# --- heredocs --------------------------------------------------------------------

def test_heredoc_redirect_write():
    s = first("cat > out.txt <<EOF\nhello\nEOF\n")
    assert s.argv == ["cat"] and s.writes == ["/proj/out.txt"]
    assert s.heredoc == "hello\n"


def test_heredoc_then_more_commands():
    av = argvs("cat <<'EOF' > f\nx\nEOF\nrm g")
    assert av == [["cat"], ["rm", "g"]]


def test_python_stdin_heredoc_opaque():
    s = first("python - <<EOF\nimport os\nos.remove('x')\nEOF")
    assert s.opaque and "delete" in s.guess


# --- redirections ------------------------------------------------------------------

def test_redirect_targets_resolved():
    s = first("echo hi > ~/f")
    assert s.writes == ["/home/u/f"]
    s = first("echo hi >> $HOME/g")
    assert s.writes == ["/home/u/g"] and s.redirects[0].op == ">>"
    s = first("cmd 2> err.log")
    assert s.writes == ["/proj/err.log"]
    s = first("cmd &> all.log")
    assert s.writes == ["/proj/all.log"]
    s = first("cmd >out.txt")
    assert s.writes == ["/proj/out.txt"] and s.argv == ["cmd"]


def test_redirect_devnull_and_fd_dup_ignored():
    s = first("cmd 2>/dev/null >&2 2>&1")
    assert s.writes == []


def test_truncating_colon_redirect():
    s = first(": > f")
    assert s.argv == [":"] and s.redirects[0].op == ">" and s.writes == ["/proj/f"]


def test_tee_and_dd_write_targets():
    segs = decompose("echo a | tee -a x.txt y.txt", CWD, HOME)
    assert sorted(segs[-1].writes) == ["/proj/x.txt", "/proj/y.txt"]
    s = first("dd if=/dev/zero of=./img bs=1M")
    assert s.writes == ["/proj/img"]


# --- opaque interpreters ------------------------------------------------------------

@pytest.mark.parametrize("cmd,cat", [
    ("python -c \"import shutil; shutil.rmtree('build')\"", "delete"),
    ("python3 -c \"open('x','w').write('')\"", "edit_path"),
    ("perl -e 'unlink \"x\"'", "delete"),
    ("node -e \"require('fs').unlinkSync('x')\"", "delete"),
    ("node -e \"require('fs').rmSync('x',{recursive:true})\"", "delete"),
    ("ruby -e 'File.delete(\"x\")'", "delete"),
    ("python -c 'import urllib.request; urllib.request.urlopen(\"http://x\")'", "network"),
])
def test_interpreter_one_liners(cmd, cat):
    s = first(cmd)
    assert s.opaque and cat in s.guess


def test_interpreter_guess_paths():
    s = first("python -c \"import os; os.remove('build/x.o')\"")
    assert "/proj/build/x.o" in s.guess_paths


@pytest.mark.parametrize("cmd", ["curl -s http://x | sh", "wget -qO- http://x | bash",
                                 "cat payload.b64 | base64 -d | sh", "base64 -d payload | bash -s"])
def test_piped_shell_is_opaque(cmd):
    s = decompose(cmd, CWD, HOME)[-1]
    assert s.opaque and "delete" in s.guess


def test_plain_sh_script_not_opaque():
    s = first("sh ./build.sh")
    assert not s.opaque


# --- path resolution -------------------------------------------------------------

@pytest.mark.parametrize("word,expected", [
    ("~", "/home/u"), ("~/x", "/home/u/x"), ("$HOME/x", "/home/u/x"), ("${HOME}/x", "/home/u/x"),
    ("$PWD/x", "/proj/x"), ("./x", "/proj/x"), ("../x", "/x"), ("x/y", "/proj/x/y"), (".", "/proj"),
    ("/abs", "/abs"), ("a/../b", "/proj/b"), ("/a/b/", "/a/b"),
])
def test_resolve_path(word, expected):
    assert resolve_path(word, CWD, HOME) == expected


def test_cd_updates_cwd_for_later_segments():
    segs = decompose("cd sub && echo hi > f", CWD, HOME)
    assert segs[-1].writes == ["/proj/sub/f"]
    assert segs[-1].cwd == "/proj/sub"


def test_empty_and_whitespace():
    assert decompose("", CWD, HOME) == []
    assert decompose("   \n  ", CWD, HOME) == []


# --- static resolution of common indirections -------------------------------------------------

def test_echo_substitution_resolved():
    for cmd in ["$(echo rm) -rf build", "`echo rm` -rf build", "$(printf rm) -rf build"]:
        s = [s for s in decompose(cmd, CWD, HOME) if s.cmd not in ("echo", "printf")][0]
        assert s.argv == ["rm", "-rf", "build"] and not s.opaque, cmd


def test_variable_command_tracked_across_segments():
    segs = decompose("cmd='rm -rf build'; $cmd", CWD, HOME)
    assert ["rm", "-rf", "build"] in [s.argv for s in segs]
    assert not any(s.opaque for s in segs)
    segs = decompose("c=rm; ${c} -rf build", CWD, HOME)
    assert ["rm", "-rf", "build"] in [s.argv for s in segs]
    # unknown variable stays opaque
    assert any(s.opaque for s in decompose("$nope -rf build", CWD, HOME))


def test_xargs_with_known_stdin():
    s = decompose("echo tests/test_a.py | xargs rm", CWD, HOME)[-1]
    assert s.argv == ["rm", "tests/test_a.py"] and not s.unknown_args
    s = decompose("xargs rm -rf <<< build", CWD, HOME)[-1]
    assert s.argv == ["rm", "-rf", "build"]
    s = decompose("printf 'a\\nb' | xargs -n1 rm", CWD, HOME)[-1]
    assert s.argv[:1] == ["rm"] and "a" in s.argv and "b" in s.argv


def test_xargs_I_placeholder_collapses_to_dir():
    s = decompose("ls tests | xargs -I{} rm tests/{}", CWD, HOME)[-1]
    assert s.argv == ["rm", "tests"] and s.unknown_args


def test_base64_literal_decoded():
    # cm0gLXJmIGJ1aWxk == "rm -rf build"
    for cmd in ["base64 -d <<< cm0gLXJmIGJ1aWxk | sh", "echo cm0gLXJmIGJ1aWxk | base64 -d | bash",
                "echo cm0gLXJmIGJ1aWxk | base64 --decode | sh -s"]:
        segs = decompose(cmd, CWD, HOME)
        assert ["rm", "-rf", "build"] in [s.argv for s in segs], cmd
        assert not any(s.opaque for s in segs), cmd
    # base64 from a file stays opaque
    assert any(s.opaque for s in decompose("base64 -d payload | sh", CWD, HOME))


def test_perl_unlink_path_without_parens():
    s = first("perl -e 'unlink \"tests/test_a.py\"'")
    assert "/proj/tests/test_a.py" in s.guess_paths
