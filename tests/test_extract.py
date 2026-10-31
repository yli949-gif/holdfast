"""Tests for holdfast.rules.extract_rules: natural language -> rules (precision-first)."""
import pytest

from holdfast.effects import Effect, EVERYTHING, UNKNOWN
from holdfast.rules import Rule, Scope, _PATH_ALIASES, extract_rules, extract_rules_llm, has_constraint_cue, parse_llm_rules

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


def test_negative_no_commits_yet():
    assert extract_rules("There are no commits yet on this branch") == []


def test_negative_api_keys_off_limits_scope():
    rules = extract_rules("The API keys are off-limits")
    assert not rules or rules[0].scope.paths != ["keys/**"]



# ================================================================ v2 hardening (B1, B6, scope, passive, temporary holds)

def sig(text):
    """(kind, action, paths, exclude_paths, branches, expires) for every extracted rule."""
    return [(r.kind, r.action, r.scope.paths, r.scope.exclude_paths, r.scope.branches, r.expires) for r in extract_rules(text)]


class TestSchemaV2:
    def test_expires_default_and_roundtrip(self):
        r = Rule(id="R-1", kind="forbid", action="modify")
        assert r.expires == ""
        r2 = Rule.from_dict({**r.as_dict(), "expires": "next_turn"})
        assert r2.expires == "next_turn"

    def test_old_dicts_still_load(self):
        old = {"id": "R-1", "kind": "forbid", "action": "modify", "scope": {"paths": ["a/**"]}, "text": "x"}
        r = Rule.from_dict(old)
        assert r.expires == "" and r.scope.exclude_paths == [] and r.scope.paths == ["a/**"]

    def test_bad_expires_rejected(self):
        with pytest.raises(ValueError):
            Rule.from_dict({"id": "R-1", "kind": "forbid", "action": "modify", "expires": "tomorrow"})

    def test_describe_mentions_temporary(self):
        r = Rule(id="R-1", kind="forbid", action="modify", expires="next_turn")
        assert "until the user's next request" in r.describe()

    def test_exclude_scope_semantics(self):
        s = Scope(exclude_paths=["src/api/**"])
        assert not s.is_universal()
        assert s.matches(Effect("edit", "src/core/x.py")) is True
        assert s.matches(Effect("edit", "README.md")) is True
        assert s.matches(Effect("edit", "src/api/handlers.py")) is False
        assert s.matches(Effect("edit", "src/api")) is False
        assert s.matches(Effect("delete", "src")) is True            # deleting a parent of the allowed area is outside it
        assert s.matches(Effect("delete", EVERYTHING)) is True
        assert s.matches(Effect("edit", UNKNOWN)) is None
        assert s.matches(Effect("write", "/tmp/sched.txt")) is False  # B4: scratch writes outside the project are not edits to it
        assert s.matches(Effect("push", "origin/main", kind="branch")) is False

    def test_exclude_describe(self):
        r = Rule(id="R-1", kind="forbid", action="modify", scope=Scope(exclude_paths=["src/api/**"]))
        assert r.describe() == "never modify outside src/api/**"


class TestTemporaryHolds:
    """B1: 'Don't change anything yet.' is a hold that lapses, not a standing universal forbid."""

    @pytest.mark.parametrize("text", [
        "Don't change anything yet.", "Don't make any changes for now.", "Hold off on editing until I say.",
        "At this point, don't modify any files.", "Not yet — don't touch the code.", "Hold off on the migration for now.",
        "Let's just discuss the design first; don't write any code yet.",
        "Read through the repo and tell me how auth works. Don't change anything yet.",
        "Don't change anything until I say so.",
    ])
    def test_universal_modify_hold(self, text):
        assert sig(text) == [("forbid", "modify", [], [], [], "next_turn")]

    def test_other_action_holds(self):
        assert sig("Don't push yet.") == [("forbid", "push", [], [], [], "next_turn")]
        assert sig("Don't commit anything for now.") == [("forbid", "commit", [], [], [], "next_turn")]

    def test_scoped_hold_keeps_scope(self):
        assert sig("Don't touch src/api/ for now.") == [("forbid", "modify", ["src/api/**"], [], [], "next_turn")]

    def test_until_i_say_on_risky_action_is_confirm(self):
        r = one("Don't push until I say so.")
        assert (r.kind, r.action, r.expires) == ("confirm", "push", "")

    def test_standing_rules_have_no_expiry(self):
        assert all(r.expires == "" for r in extract_rules("Never force push. Don't touch the tests."))


class TestPathCase:
    def test_case_preserved(self):
        assert one("Don't overwrite README.md").scope.paths == ["README.md"]

    def test_dotdir(self):
        assert one("Please don't modify anything under .github/").scope.paths == [".github/**"]
        assert one("Don't edit .env.production.").scope.paths == [".env.production"]


class TestCoordination:
    def test_force_push_and_tests(self):
        rules = extract_rules("Fix the bug. Never force push and don't touch the tests.")
        assert [r.describe() for r in rules] == ["never force_push", "never modify on paths " + ", ".join(TESTS_GLOBS)]

    def test_no_x_no_y(self):
        assert kinds("No deleting, no force pushing.") == [("forbid", "delete"), ("forbid", "force_push")]

    def test_two_verbs_one_confirm(self):
        assert kinds("Ask before you delete or install anything.") == [("confirm", "delete"), ("confirm", "install")]

    def test_mixed(self):
        assert sig("Add logging to the request handler. Also: never push to main, and ask me before deleting anything.") == [
            ("forbid", "push", [], [], ["main", "*/main"], ""), ("confirm", "delete", [], [], [], "")]

    def test_task_then_order_then_install(self):
        assert kinds("Rename the helper in utils.py, run the tests before you commit, and don't install anything.") == [
            ("require_before", "commit"), ("forbid", "install")]


class TestScopeRules:
    @pytest.mark.parametrize("text,inside", [
        ("Only edit files under src/api/", "src/api/**"), ("Only touch src/.", "src/**"),
        ("Changes should be limited to frontend/.", "frontend/**"), ("Keep your edits to src/api/ only.", "src/api/**"),
        ("Do not change anything outside src/.", "src/**"), ("Work only inside the packages/ui directory.", "packages/ui/**"),
        ("Avoid making changes to files outside of packages/core/.", "packages/core/**"),
        ("Stay within src/billing/", "src/billing/**"),
    ])
    def test_exclude_scope(self, text, inside):
        assert sig(text) == [("forbid", "modify", [], [inside], [], "")]

    def test_module(self):
        assert sig("Stay within the auth module.") == [("forbid", "modify", [], ["**/auth/**", "**/auth.py"], [], "")]

    def test_b6_scope_plus_vendored(self):
        assert sig("Only edit files under src/api/; src/core/ is vendored and must not be modified.") == [
            ("forbid", "modify", [], ["src/api/**"], [], ""), ("forbid", "modify", ["src/core/**"], [], [], "")]

    def test_anaphora_that_folder(self):
        assert sig("Implement the cache layer in src/cache/. Stay within that folder and don't pull in new deps.") == [
            ("forbid", "modify", [], ["src/cache/**"], [], ""), ("forbid", "install", [], [], [], "")]

    def test_except(self):
        assert sig("You can edit anything except the migrations folder.") == [("forbid", "modify", _PATH_ALIASES["migrations"], [], [], "")]


class TestPassiveAndPredicates:
    @pytest.mark.parametrize("text,path", [
        ("src/core/ must not be modified.", "src/core/**"), ("The migrations/ folder is off-limits.", "migrations/**"),
        ("Leave vendor/ alone.", "vendor/**"), ("docs/api.md is read-only.", "docs/api.md"),
        ("Stay out of the legacy/ directory.", "legacy/**"), ("Keep setup.cfg as is.", "setup.cfg"),
        ("No changes to the public API in api/v1/.", "api/v1/**"), ("src/vendor/ is vendored.", "src/vendor/**"),
        ("Refactor the parser; src/parser/grammar.py is generated and must not be edited by hand.", "src/parser/grammar.py"),
        ("Stay away from the k8s/production manifests.", "k8s/production/**"),
        ("lib/ should not be touched", "lib/**"),
    ])
    def test_forbid_modify_path(self, text, path):
        assert sig(text) == [("forbid", "modify", [path], [], [], "")]

    def test_passive_delete(self):
        assert sig("data/raw/ must not be deleted.") == [("forbid", "delete", ["data/raw/**"], [], [], "")]

    def test_gerund_subject(self):
        assert kinds("Deleting files is off-limits.") == [("forbid", "delete")]

    def test_any_test_files(self):
        assert "tests/**" in one("Don't modify any test files.").scope.paths


class TestMorePhrasings:
    @pytest.mark.parametrize("text,expected", [
        ("Get my approval before installing packages.", ("confirm", "install")),
        ("Please ask for permission before you remove any file.", ("confirm", "delete")),
        ("Before deploying anything, ask me.", ("confirm", "deploy")),
        ("Add the endpoint, then check with me before you install anything new.", ("confirm", "install")),
        ("Let me know before you delete anything, ok?", ("confirm", "delete")),
        ("Every push needs my sign-off.", ("confirm", "push")),
        ("Ask before you commit.", ("confirm", "commit")),
        ("No deleting files.", ("forbid", "delete")),
        ("Don't hit the network.", ("forbid", "network")),
        ("Work offline.", ("forbid", "network")),
        ("I'd prefer you not install new dependencies for this task.", ("forbid", "install")),
        ("Don't ever use git push -f", ("forbid", "force_push")),
        ("Never force-push.", ("forbid", "force_push")),
        ("No pushes to origin.", ("forbid", "push")),
        ("Never rewrite history.", ("forbid", "git_history")),
        ("Don't amend.", ("forbid", "git_history")),
        ("Do not install any packages.", ("forbid", "install")),
    ])
    def test_single(self, text, expected):
        r = one(text)
        assert (r.kind, r.action) == expected and r.expires == ""

    def test_universal_scopes(self):
        for t in ["Get my approval before installing packages.", "Add the endpoint, then check with me before you install anything new.",
                  "Please ask for permission before you remove any file.", "No pushes to origin."]:
            assert one(t).scope.is_universal(), t

    def test_confirm_migrations_is_command_scoped(self):
        r = one("Confirm before running migrations.")
        assert (r.kind, r.action) == ("confirm", "run")
        assert r.scope.command and Scope(command=r.scope.command).matches(Effect("run", "x", kind="command", detail="python manage.py migrate"))

    def test_commit_to_main(self):
        assert sig("Don't commit directly to main.") == [("forbid", "commit", [], [], ["main", "*/main"], "")]

    def test_new_network_and_install(self):
        assert kinds("Write the parser; no internet access, and no new packages.") == [("forbid", "network"), ("forbid", "install")]

    @pytest.mark.parametrize("text", ["Don't touch anything production.", "Never touch prod.", "Hands off production."])
    def test_prod(self, text):
        assert {(r.kind, r.action) for r in extract_rules(text)} == {("forbid", "modify"), ("forbid", "deploy"), ("forbid", "run")}

    @pytest.mark.parametrize("text,action", [
        ("Only push after the test suite passes.", "push"), ("Don't push until tests are green.", "push"),
        ("Run the tests before committing.", "commit"), ("Make sure tests pass before you push.", "push"),
        ("Commit only after running pytest.", "commit"), ("Before you push, the test suite must pass.", "push"),
        ("pls make sure pytest is green before u commit", "commit"),
        ("Update the changelog, and only commit once the tests are passing.", "commit"),
    ])
    def test_order(self, text, action):
        r = one(text)
        assert (r.kind, r.action, r.prerequisite) == ("require_before", action, "tests_pass")

    @pytest.mark.parametrize("text,path", [
        ("Once you start implementing, don't modify tests/.", "tests/**"),
        ("Don't change the tests after this point.", "tests/**"),
        ("From this point on, leave tests/test_api.py as it is.", "tests/test_api.py"),
    ])
    def test_freeze(self, text, path):
        r = one(text)
        assert (r.kind, r.action) == ("freeze", "modify") and path in r.scope.paths


NEGATIVES_V2 = [
    "Fix the bug in auth.py and add a test", "Remove the old config loader and replace it with the new one",
    "Push your branch when done", "Install pytest if it's missing", "Don't worry about tests for now",
    "I don't want to break the tests", "Make sure tests pass", "Never mind, go ahead",
    "No need to ask before editing files.", "Go ahead and change the tests now.", "You can modify anything you want.",
    "Feel free to delete the build directory.", "Force push is fine here, the branch is mine.",
    "Why did the push to main fail?", "Is it okay to push to main yet?", "Don't stop until all the tests pass.",
    "Don't hesitate to refactor src/core if needed.", "Edit the deploy script so it only pushes after tests pass.",
    "Write a pre-commit hook that blocks force pushes.", "Add a check that prevents deleting files in data/.",
    "Previously you said not to push to main; that's no longer needed.", "Stay focused on the parser bug.",
    "Don't change the function signature, just the body.", "The prod config lives in config/prod.yml.",
    "Run kubectl get pods -n prod and paste the output.", "Leave a comment explaining the workaround.", "Keep going.",
    "The tests directory is a mess, clean it up.", "Only the last test is failing, fix that one.",
]


@pytest.mark.parametrize("text", NEGATIVES_V2)
def test_negative_v2_extracts_nothing(text):
    assert extract_rules(text) == [], [r.describe() for r in extract_rules(text)]


class TestHybridGate:
    def test_cue(self):
        assert has_constraint_cue("Please refrain from pushing")
        assert not has_constraint_cue("Fix the parser and add a test")


class TestLLMParsing:
    """parse_llm_rules validates model output against the schema (no network)."""

    def test_valid(self):
        rs = parse_llm_rules({"rules": [{"kind": "forbid", "action": "modify", "paths": ["src/core"], "text": "x"},
                                        {"kind": "forbid", "action": "modify", "exclude_paths": ["src/api/"], "text": "y"},
                                        {"kind": "forbid", "action": "modify", "expires": "next_turn", "text": "z"}]}, "msg")
        assert [(r.kind, r.scope.paths, r.scope.exclude_paths, r.expires, r.source) for r in rs] == [
            ("forbid", ["src/core/**"], [], "", "llm"), ("forbid", [], ["src/api/**"], "", "llm"), ("forbid", [], [], "next_turn", "llm")]

    def test_invalid_dropped(self):
        rs = parse_llm_rules({"rules": [{"kind": "maybe", "action": "modify"}, {"kind": "forbid", "action": "teleport"},
                                        {"kind": "require_before", "action": "push"}, {"kind": "forbid", "action": "push", "pattern": "("},
                                        "junk"]}, "msg")
        assert [(r.kind, r.action, r.prerequisite) for r in rs] == [("require_before", "push", "tests_pass")]

    def test_garbage_payload(self):
        assert parse_llm_rules(None, "m") == [] and parse_llm_rules({"rules": "x"}, "m") == []

    def test_cli_missing_returns_empty(self, monkeypatch):
        monkeypatch.setenv("PATH", "/nonexistent")
        assert extract_rules_llm("Never force push", timeout=5) == []
