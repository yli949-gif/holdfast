"""Tests for holdfast.rules.extract_rules: natural language -> rules (precision-first)."""
import pytest

from holdfast.rules import Scope, _PATH_ALIASES, extract_rules

TESTS_GLOBS = _PATH_ALIASES["the tests"]


def one(text):
    rules = extract_rules(text)
    assert len(rules) == 1, [r.describe() for r in rules]
    return rules[0]


def kinds(text):
    return [(r.kind, r.action) for r in extract_rules(text)]


# ---------------------------------------------------------------- forbid delete / modify

class TestForbidDeleteModify:
    @pytest.mark.parametrize("text", ["Don't delete anything in data/", "Never delete files under data/",
                                      "Never remove anything from the data directory", "Do not delete the data"])
    def test_delete_scoped_to_data(self, text):
        r = one(text)
        assert (r.kind, r.action, r.scope.paths) == ("forbid", "delete", ["data/**"])
        assert r.text == text and r.source == "extracted"

    def test_delete_backticked_file(self):
        r = one("Do not remove `build/cache.db`")
        assert (r.kind, r.action, r.scope.paths) == ("forbid", "delete", ["build/cache.db"])

    def test_delete_alias_migrations(self):
        r = one("Do not delete the migrations")
        assert (r.action, r.scope.paths) == ("delete", ["**/migrations/**"])

    @pytest.mark.parametrize("text", ["Don't delete anything", "Never delete files", "Do not remove any files"])
    def test_delete_universal(self, text):
        r = one(text)
        assert (r.kind, r.action) == ("forbid", "delete") and r.scope.is_universal()

    @pytest.mark.parametrize("text", ["Don't modify the tests", "Do not touch the tests", "Never change the test files"])
    def test_modify_tests(self, text):
        r = one(text)
        assert (r.kind, r.action) == ("forbid", "modify")
        assert "tests/**" in r.scope.paths and "**/test_*.py" in r.scope.paths

    def test_modify_explicit_dir(self):
        r = one("Don't change anything in src/legacy/")
        assert (r.kind, r.action, r.scope.paths) == ("forbid", "modify", ["src/legacy/**"])

    def test_modify_two_dirs(self):
        r = one("Do not modify anything under tests/ or fixtures/")
        assert r.scope.paths == ["tests/**", "fixtures/**"]

    def test_modify_aliases(self):
        assert "config/**" in one("Do not modify config").scope.paths
        assert "package-lock.json" in one("Don't touch the lockfile").scope.paths

    def test_leave_alone_and_hands_off(self):
        r = one("Leave config.yaml alone")
        assert (r.kind, r.action, r.scope.paths) == ("forbid", "modify", ["config.yaml"])
        r = one("Hands off the migrations")
        assert (r.kind, r.action, r.scope.paths) == ("forbid", "modify", ["**/migrations/**"])

    def test_read_only_and_frozen_are_forbid(self):
        r = one("tests/ is read-only")
        assert (r.kind, r.action, r.scope.paths) == ("forbid", "modify", ["tests/**"])
        r = one("The tests are frozen")
        assert (r.kind, r.action) == ("forbid", "modify") and "tests/**" in r.scope.paths

    def test_without_asking_is_confirm_not_forbid(self):
        r = one("Do not delete anything without asking me first")
        assert (r.kind, r.action) == ("confirm", "delete") and r.scope.is_universal()
        r = one("Don't modify the tests without my approval")
        assert (r.kind, r.action) == ("confirm", "modify") and "tests/**" in r.scope.paths

    def test_unless_clause_stripped(self):
        r = one("Never delete anything in data/ unless I say so")
        assert r.scope.paths == ["data/**"] and r.kind == "forbid"


# ---------------------------------------------------------------- git / install / network / prod

class TestForbidGitInstallNetwork:
    @pytest.mark.parametrize("text", ["Never force push", "Don't force-push", "No force pushes please", "Do not ever force push to main"])
    def test_force_push(self, text):
        r = one(text)
        assert (r.kind, r.action) == ("forbid", "force_push") and r.scope.is_universal()

    def test_push_to_main(self):
        r = one("Don't push to main")
        assert (r.kind, r.action, r.scope.branches) == ("forbid", "push", ["main", "*/main"])
        r = one("Never push directly to origin/main")
        assert r.scope.branches == ["origin/main", "*/origin/main"]

    def test_push_to_remote_is_universal(self):
        r = one("Do not push to the remote")
        assert (r.kind, r.action) == ("forbid", "push") and r.scope.is_universal()

    @pytest.mark.parametrize("text", ["Don't push anything", "No pushing", "Never push"])
    def test_push_universal(self, text):
        r = one(text)
        assert (r.kind, r.action) == ("forbid", "push") and r.scope.is_universal()

    @pytest.mark.parametrize("text", ["Don't commit anything", "Never commit", "Do not make any commits", "No commits please"])
    def test_commit(self, text):
        r = one(text)
        assert (r.kind, r.action) == ("forbid", "commit")

    @pytest.mark.parametrize("text", ["Don't install any new packages", "Do not add dependencies", "No new dependencies",
                                      "Never install third-party libraries"])
    def test_install(self, text):
        r = one(text)
        assert (r.kind, r.action) == ("forbid", "install")

    @pytest.mark.parametrize("text", ["Don't make any network calls", "No network access", "Stay offline", "Do not access the internet"])
    def test_network(self, text):
        r = one(text)
        assert (r.kind, r.action) == ("forbid", "network")

    def test_history(self):
        r = one("Don't rewrite history")
        assert (r.kind, r.action) == ("forbid", "git_history")

    @pytest.mark.parametrize("text", ["Never touch production", "Don't deploy to prod", "Do not run anything against production"])
    def test_prod(self, text):
        rules = extract_rules(text)
        assert {(r.kind, r.action) for r in rules} == {("forbid", "modify"), ("forbid", "deploy"), ("forbid", "run")}
        assert all(r.scope.pattern == "prod" or r.scope.command for r in rules)


# ---------------------------------------------------------------- confirm-before

class TestConfirm:
    @pytest.mark.parametrize("text", ["Ask me before deleting anything", "Deleting requires my approval", "Ask before you delete files"])
    def test_delete_universal(self, text):
        r = one(text)
        assert (r.kind, r.action, r.mode) == ("confirm", "delete", "once") and r.scope.is_universal()

    def test_delete_scoped(self):
        r = one("Check with me before you delete anything in data/")
        assert (r.kind, r.action, r.scope.paths) == ("confirm", "delete", ["data/**"])
        r = one("Get my approval before deleting files under build/")
        assert (r.kind, r.action, r.scope.paths) == ("confirm", "delete", ["build/**"])
        r = one("Ask me before deleting anything in `data/`")
        assert r.scope.paths == ["data/**"]

    def test_delete_scoped_alias_with_first(self):
        r = one("Ask me first before you delete the tests directory")
        assert (r.kind, r.action) == ("confirm", "delete") and "tests/**" in r.scope.paths

    @pytest.mark.parametrize("text", ["Confirm with me before pushing", "Always ask before any push", "Pushing requires confirmation"])
    def test_push(self, text):
        r = one(text)
        assert (r.kind, r.action) == ("confirm", "push") and r.scope.is_universal()

    def test_push_to_branch_gets_branch_scope(self):
        r = one("Ask me before you push to main")
        assert (r.kind, r.action) == ("confirm", "push")
        assert r.scope.branches == ["main", "*/main"] and not r.scope.paths

    def test_push_to_remote_is_universal(self):
        r = one("Ask me before pushing to origin")
        assert (r.kind, r.action) == ("confirm", "push") and r.scope.is_universal()

    def test_deploy(self):
        r = one("Ask before deploying")
        assert (r.kind, r.action) == ("confirm", "deploy") and r.scope.is_universal()
        r = one("Check with me before deploying to production")
        assert (r.kind, r.action, r.scope.pattern) == ("confirm", "deploy", "prod")


# ---------------------------------------------------------------- require_before tests_pass

class TestRequireBefore:
    @pytest.mark.parametrize("text,action", [
        ("Only push after tests pass", "push"), ("Don't push until the tests pass", "push"),
        ("Run the tests before pushing", "push"), ("Make sure tests pass before you commit", "commit"),
        ("Tests must pass before any push", "push"), ("Never deploy unless tests are green", "deploy"),
        ("Only commit once the tests are passing", "commit"), ("Ensure the tests pass before merging", "push"),
    ])
    def test_tests_pass(self, text, action):
        r = one(text)
        assert (r.kind, r.action, r.prerequisite) == ("require_before", action, "tests_pass")
        assert r.scope.is_universal()


# ---------------------------------------------------------------- freeze

class TestFreeze:
    @pytest.mark.parametrize("text", ["Don't modify the tests after you start", "Do not change the tests once you begin"])
    def test_after_you_start(self, text):
        r = one(text)
        assert (r.kind, r.action) == ("freeze", "modify") and "tests/**" in r.scope.paths

    @pytest.mark.parametrize("text", ["Leave the tests as they are", "Keep the tests as they are"])
    def test_as_they_are(self, text):
        r = one(text)
        assert (r.kind, r.action) == ("freeze", "modify") and "tests/**" in r.scope.paths

    def test_explicit_file(self):
        r = one("Don't change src/core.py once you begin")
        assert (r.kind, r.action, r.scope.paths) == ("freeze", "modify", ["src/core.py"])


# ---------------------------------------------------------------- explicit syntax

class TestExplicitSyntax:
    def test_forbid_with_paths(self):
        r = one("holdfast: forbid delete data/**")
        assert (r.kind, r.action, r.scope.paths) == ("forbid", "delete", ["data/**"])

    def test_forbid_multiple_paths(self):
        r = one("holdfast: forbid delete data/**, build/**")
        assert r.scope.paths == ["data/**", "build/**"]

    def test_pattern_syntax(self):
        r = one(r"holdfast: forbid network /example\.com/")
        assert r.scope.pattern == r"example\.com" and not r.scope.paths

    def test_confirm(self):
        r = one("holdfast: confirm push")
        assert (r.kind, r.action) == ("confirm", "push") and r.scope.is_universal()

    def test_require_before(self):
        r = one("holdfast: require_before push tests_pass")
        assert (r.kind, r.action, r.prerequisite) == ("require_before", "push", "tests_pass")
        r = one("holdfast: require_before push")
        assert r.prerequisite == "tests_pass"

    def test_freeze(self):
        r = one("holdfast: freeze modify tests/**")
        assert (r.kind, r.action, r.scope.paths) == ("freeze", "modify", ["tests/**"])


# ---------------------------------------------------------------- multi-sentence, dedup, metadata

class TestMultiple:
    def test_multiple_sentences(self):
        rules = extract_rules("Fix the login bug. Don't touch the tests. Ask me before pushing.")
        assert [(r.kind, r.action) for r in rules] == [("forbid", "modify"), ("confirm", "push")]
        assert rules[0].text == "Don't touch the tests." and rules[1].text == "Ask me before pushing."

    def test_and_also(self):
        rules = extract_rules("Never delete anything in data/ and also don't force push.")
        assert [(r.kind, r.action) for r in rules] == [("forbid", "delete"), ("forbid", "force_push")]

    def test_duplicate_sentences_dedup(self):
        rules = extract_rules("Never force push. Never force push.")
        assert len(rules) == 1

    def test_source_and_turn(self):
        r = extract_rules("Never force push", source="user", turn=7)[0]
        assert r.source == "user" and r.created_turn == 7 and r.active

    def test_ids_are_deterministic(self):
        assert extract_rules("Never force push")[0].id == extract_rules("Never force push")[0].id

    def test_empty(self):
        assert extract_rules("") == [] and extract_rules("   ") == []


# ---------------------------------------------------------------- NEGATIVES: task instructions must extract nothing

NEGATIVES = [
    "Fix the bug in auth.py",
    "Delete the unused imports",
    "Push your changes when done",
    "Add tests for the parser",
    "Remove the deprecated function",
    "Install the dependencies then run the tests",
    "Refactor the parser and add type hints",
    "Run the test suite and fix failures",
    "Commit when you're done",
    "Update the README",
    "Remove the TODO comments in utils.py",
    "Clean up the formatting",
    "Deploy the new version to staging",
    "The tests are failing, please fix them",
    "Can you delete the old migration file?",
    "Don't forget to delete the temp files",
    "Don't worry about the tests",
    "No need to run the tests",
    "Don't just delete the function, refactor it",
    "Update tests/test_auth.py to cover the new path",
    "Keep the tests passing",
    "yes",
    "ok go ahead",
]


@pytest.mark.parametrize("text", NEGATIVES)
def test_negative_extracts_nothing(text):
    assert extract_rules(text) == [], [r.describe() for r in extract_rules(text)]


@pytest.mark.xfail(reason="false positive: 'no commits' inside a descriptive sentence is read as a standing forbid-commit rule", strict=True)
def test_negative_no_commits_yet():
    assert extract_rules("There are no commits yet on this branch") == []


@pytest.mark.xfail(reason="false positive: 'the API keys' is not a path but becomes a forbid-modify rule scoped to keys/**", strict=True)
def test_negative_api_keys_off_limits_scope():
    rules = extract_rules("The API keys are off-limits")
    assert not rules or rules[0].scope.paths != ["keys/**"]
