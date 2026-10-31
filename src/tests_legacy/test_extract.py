"""Tests for holdfast.extract: labelled NL sentences -> rules / confirmations / revocations."""
import json
import subprocess

import pytest

from holdfast import extract_llm
from holdfast.extract import (
    Confirmation,
    extract,
    extract_all,
    extract_confirmations,
    is_revocation,
)

# Each entry: sentence -> set of (kind, action, target, requires) expected (order-free).
POSITIVES = [
    # delete / confirm
    ("don't delete anything", {("forbid", "delete", None, None)}),
    ("Never delete anything.", {("forbid", "delete", None, None)}),
    ("Do not delete any files", {("forbid", "delete", None, None)}),
    ("please do not remove files without asking me", {("confirm", "delete", None, None)}),
    ("You must never remove anything", {("forbid", "delete", None, None)}),
    ("ask me before deleting anything", {("confirm", "delete", None, None)}),
    ("Ask before removing files", {("confirm", "delete", None, None)}),
    ("confirm before you delete", {("confirm", "delete", None, None)}),
    ("Confirm with me before deleting stuff", {("confirm", "delete", None, None)}),
    ("check with me first before you remove anything", {("confirm", "delete", None, None)}),
    ("don't delete anything without my approval", {("confirm", "delete", None, None)}),
    ("get my permission before deleting files", {("confirm", "delete", None, None)}),
    ("don't rm anything under tests/", {("forbid", "delete", "tests", None)}),
    ("never delete the tests", {("forbid", "delete", "tests", None)}),
    ("ask me before you delete anything in build/", {("confirm", "delete", "build", None)}),
    # force push / push
    ("never force push", {("forbid", "force_push", None, None)}),
    ("Don't force-push.", {("forbid", "force_push", None, None)}),
    ("no force pushes", {("forbid", "force_push", None, None)}),
    ("never use git push --force", {("forbid", "force_push", None, None)}),
    ("don't push", {("forbid", "push", None, None)}),
    ("don't push to main", {("forbid", "push", "main", None)}),
    ("never push to master", {("forbid", "push", "master", None)}),
    ("do not push to prod", {("forbid", "push", "prod*", None)}),
    ("don't push anything to production", {("forbid", "push", "prod*", None)}),
    ("don't push without asking me", {("confirm", "push", None, None)}),
    ("ask me before pushing", {("confirm", "push", None, None)}),
    # order
    ("only push after the tests pass", {("order", "push", None, "tests_passed")}),
    ("Only push after tests pass.", {("order", "push", None, "tests_passed")}),
    ("only push once the tests are green", {("order", "push", None, "tests_passed")}),
    ("run tests before pushing", {("order", "push", None, "tests_passed")}),
    ("run the tests before committing", {("order", "commit", None, "tests_passed")}),
    ("always run the tests before pushing or committing",
     {("order", "push", None, "tests_passed"), ("order", "commit", None, "tests_passed")}),
    ("make sure the tests pass before you push", {("order", "push", None, "tests_passed")}),
    ("don't push until tests pass", {("order", "push", None, "tests_passed")}),
    ("tests must pass before you commit", {("order", "commit", None, "tests_passed")}),
    # edit scope / forbid edit
    ("don't touch the tests", {("forbid", "edit_path", "tests", None)}),
    ("Don't modify tests/", {("forbid", "edit_path", "tests", None)}),
    ("do not edit anything under tests", {("forbid", "edit_path", "tests", None)}),
    ("never change the test files", {("forbid", "edit_path", "tests", None)}),
    ("don't touch or delete anything in migrations/",
     {("forbid", "edit_path", "migrations", None), ("forbid", "delete", "migrations", None)}),
    ("leave src/legacy/ alone", {("forbid", "edit_path", "src/legacy", None)}),
    ("don't modify config/settings.py", {("forbid", "edit_path", "config/settings.py", None)}),
    ("only edit files in src/", {("scope", "edit_path", "src", None)}),
    ("Only modify src/foo", {("scope", "edit_path", "src/foo", None)}),
    ("only make changes under src/", {("scope", "edit_path", "src", None)}),
    ("keep all changes inside lib/", {("scope", "edit_path", "lib", None)}),
    ("don't edit anything outside of src/", {("scope", "edit_path", "src", None)}),
    # prod
    ("don't touch prod", {("forbid", "touch_prod", None, None)}),
    ("Never touch production.", {("forbid", "touch_prod", None, None)}),
    ("do not deploy to production", {("forbid", "touch_prod", None, None)}),
    ("stay away from prod", {("forbid", "touch_prod", None, None)}),
    # network / install / commit
    ("no network", {("forbid", "network", None, None)}),
    ("don't make network calls", {("forbid", "network", None, None)}),
    ("do not make any network requests", {("forbid", "network", None, None)}),
    ("work offline, no internet access", {("forbid", "network", None, None)}),
    ("don't install packages", {("forbid", "install", None, None)}),
    ("do not install any dependencies", {("forbid", "install", None, None)}),
    ("no new dependencies", {("forbid", "install", None, None)}),
    ("don't install anything", {("forbid", "install", None, None)}),
    ("don't commit", {("forbid", "commit", None, None)}),
    ("Do not make any commits.", {("forbid", "commit", None, None)}),
    ("don't commit without asking", {("confirm", "commit", None, None)}),
    # run_cmd
    ("never run docker", {("forbid", "run_cmd", "docker", None)}),
    ("don't use sudo", {("forbid", "run_cmd", "sudo", None)}),
    ("do not run terraform apply", {("forbid", "run_cmd", "terraform apply", None)}),
    # multi-sentence
    ("Fix the bug in src/parser.py. Don't touch the tests, and never force push.",
     {("forbid", "edit_path", "tests", None), ("forbid", "force_push", None, None)}),
    ("ask me before deleting anything; only push after tests pass",
     {("confirm", "delete", None, None), ("order", "push", None, "tests_passed")}),
    ("don't do anything destructive", {("forbid", "delete", None, None), ("forbid", "force_push", None, None)}),
    ("ask before doing anything irreversible", {("confirm", "delete", None, None), ("confirm", "force_push", None, None)}),
]

NEGATIVES = [
    "fix the failing test in tests/test_x.py",
    "delete the TODO comment in main.py",
    "remove the unused import at the top of utils.py",
    "push the branch when you're done",
    "run the tests",
    "install the dependencies and then run the build",
    "please add a test for the parser",
    "can you delete the old migration file?",
    "the tests are failing on CI",
    "I don't think the tests cover this case",
    "don't forget to update the changelog",
    "don't worry about the tests for now",
    "what does rm -rf do?",
    "we don't have network access in CI, so mock the API",
    "the production config lives in config/production.yaml",
    "I removed the old tests yesterday",
    "use pytest to run the suite",
    "commit your changes when done",
    "force push is what broke it last time",
    "the delete button is in the top right corner",
    "refactor the push notification service",
    "hello",
    "",
    "install pytest and run it",
    "it should never return None",
    "never mind, go with the first approach",
]


def _tuples(rules):
    return {(r["kind"], r["action"], r.get("target"), r.get("requires")) for r in rules}


@pytest.mark.parametrize("text,expected", POSITIVES, ids=[p[0][:40] for p in POSITIVES])
def test_positive_extraction(text, expected):
    got = _tuples(extract(text))
    assert got == expected, f"{text!r}: got {got}"


@pytest.mark.parametrize("text", NEGATIVES)
def test_negative_extraction(text):
    assert extract(text) == [], text


def test_extract_returns_validatable_rule_dicts():
    rules = extract("don't delete anything", turn=4)
    r = rules[0]
    assert r["source"]["turn"] == 4 and r["source"]["via"] == "pattern"
    assert "delete" in r["source"]["text"]


def test_positive_count_is_substantial():
    assert len(POSITIVES) >= 45 and len(NEGATIVES) >= 15


# --- confirmations ------------------------------------------------------------------------

@pytest.mark.parametrize("text,action,target", [
    ("yes", "any", None), ("Yes.", "any", None), ("go ahead", "any", None), ("approved", "any", None),
    ("ok do it", "any", None), ("confirmed", "any", None), ("yes delete it", "delete", None),
    ("yes, go ahead and delete build", "delete", "build"), ("you can delete the cache", "delete", None),
    ("sure, remove the build directory", "delete", "build"), ("go ahead and push", "push", None),
    ("ok, force push it", "force_push", None), ("approved: rm the cache dir", "delete", "cache"),
    ("fine, install it", "install", None), ("yes please", "any", None), ("do it", "any", None),
    ("feel free to delete tests/old/", "delete", "tests/old"), ("I confirm, delete them", "delete", None),
    ("yes go ahead and delete build", "delete", "build"),
])
def test_confirmations(text, action, target):
    got = extract_confirmations(text)
    assert got and got[0].action == action and got[0].target == target, (text, got)
    assert isinstance(got[0], Confirmation)


@pytest.mark.parametrize("text", ["no", "not yet", "ok, fix the failing test first", "yes the tests are failing",
                                  "don't", "wait", "fix the bug", "okay so the problem is in the parser",
                                  "sure, but first explain what rm -rf does"])
def test_not_confirmations(text):
    assert extract_confirmations(text) == [], text


@pytest.mark.parametrize("text", ["actually never mind", "cancel that", "nevermind", "scratch that, don't delete it",
                                  "wait, don't", "no, don't do that", "stop", "hold off on that", "I changed my mind"])
def test_revocations(text):
    assert is_revocation(text), text


@pytest.mark.parametrize("text", ["yes", "don't delete anything", "fix the tests", "go ahead"])
def test_not_revocations(text):
    assert not is_revocation(text), text


def test_extract_all_combined():
    res = extract_all("yes, delete it. But never force push.", turn=9)
    assert res.confirmations and res.confirmations[0].action == "delete"
    assert _tuples(res.rules) == {("forbid", "force_push", None, None)}
    assert res.revoked is False
    res = extract_all("never mind", turn=1)
    assert res.revoked and not res.rules and not res.confirmations


def test_pronoun_refusal_is_not_a_rule():
    assert extract("don't delete it") == []
    assert extract("no, don't remove that") == []


# --- corpus sentences that the first extractor missed (corpus/constraints.jsonl) ------------

CORPUS_FIXED = [
    # order: tests_ok before push/commit in every phrasing
    ("commit only after running pytest", {("order", "commit", None, "tests_passed")}),
    ("run the tests before every commit", {("order", "commit", None, "tests_passed")}),
    ("Before you push, the test suite must pass.", {("order", "push", None, "tests_passed")}),
    ("Rule: tests must be run and pass before any push.", {("order", "push", None, "tests_passed")}),
    ("pls make sure pytest is green before u commit", {("order", "commit", None, "tests_passed")}),
    ("Only commit once the tests are passing", {("order", "commit", None, "tests_passed")}),
    # confirm: askme cues
    ("check with me before running kubectl", {("confirm", "run_cmd", "kubectl", None)}),
    ("ask before touching production", {("confirm", "touch_prod", None, None)}),
    ("don't push until I say so", {("confirm", "push", None, None)}),
    ("Kindly refrain from pushing to the remote unless I explicitly approve.", {("confirm", "push", None, None)}),
    ("Going forward, every push needs my sign-off.", {("confirm", "push", None, None)}),
    ("I want to review before anything gets pushed", {("confirm", "push", None, None)}),
    ("Confirm with me prior to any git push.", {("confirm", "push", None, None)}),
    ("pls dont delete stuff w/o asking", {("confirm", "delete", None, None)}),
    ("before delete any file, must ask me first", {("confirm", "delete", None, None)}),
    ("Any deletion → ask first.", {("confirm", "delete", None, None)}),
    ("let me know before you delete anything, ok?", {("confirm", "delete", None, None)}),
    ("you may delete things but ask first", {("confirm", "delete", None, None)}),
    ("Please ask for permission before you remove any file.", {("confirm", "delete", None, None)}),
    ("don't push to main without checking with me", {("confirm", "push", "main", None)}),
    # a push rule subsumes force_push (classify labels a force push as push + force_push)
    ("One more thing — from here on, ask before any push or force push.", {("confirm", "push", None, None)}),
    # forbid delete / install / commit / network / force push
    ("never rm -rf", {("forbid", "delete", None, None)}),
    ("deleting files is off limits", {("forbid", "delete", None, None)}),
    ("No destructive git commands (reset --hard, clean, etc.)", {("forbid", "delete", None, None)}),
    ("whatever you do, do not rm the data/ dir", {("forbid", "delete", "data", None)}),
    ("from now on, no more installs", {("forbid", "install", None, None)}),
    ("Dont instal anything new", {("forbid", "install", None, None)}),
    ("I'd prefer you not install new dependencies for this task.", {("forbid", "install", None, None)}),
    ("No apt-get, no pip install, nothing like that.", {("forbid", "install", None, None)}),
    ("don't pull in new deps", {("forbid", "install", None, None)}),
    ("donot push force, ever", {("forbid", "force_push", None, None)}),
    ("NO FORCE PUSH", {("forbid", "force_push", None, None)}),
    ("No commits — I'll commit myself.", {("forbid", "commit", None, None)}),
    ("you are not allowed to run anything with curl or wget", {("forbid", "network", None, None)}),
    ("Add logging to the request handler. Also: you are not allowed to run anything with curl or wget.",
     {("forbid", "network", None, None)}),
    # prod vs explicit path
    ("Hands off production.", {("forbid", "touch_prod", None, None)}),
    ("Do not run kubectl against the prod cluster.", {("forbid", "touch_prod", None, None)}),
    ("don't deploy to prod", {("forbid", "touch_prod", None, None)}),
    ("Don't touch the prod config (config/prod.yml)", {("forbid", "edit_path", "config/prod.yml", None)}),
    ("do not edit config/production.yml", {("forbid", "edit_path", "config/production.yml", None)}),
    ("Do not overwrite README.md", {("forbid", "edit_path", "README.md", None)}),
    ("never ever touch anything in the .github folder", {("forbid", "edit_path", ".github", None)}),
    ("Stay out of the tests directory.", {("forbid", "edit_path", "tests", None)}),
    ("You can edit anything except the migrations folder.", {("forbid", "edit_path", "migrations", None)}),
    ("Please refactor auth.py. Oh and don't touch anything under tests/ — I'll handle those.",
     {("forbid", "edit_path", "tests", None)}),
    # scope
    ("Keep your edits to src/api/ only.", {("scope", "edit_path", "src/api", None)}),
    ("Avoid making changes to files outside of packages/core/.", {("scope", "edit_path", "packages/core", None)}),
    ("Work only inside the frontend/ directory.", {("scope", "edit_path", "frontend", None)}),
    # antecedents: a path earlier in the message scopes a pronoun / demonstrative
    ("Fix the flaky test in tests/test_io.py, but don't delete it.", {("forbid", "delete", "tests/test_io.py", None)}),
    ("Implement the cache layer in src/cache/. Stay within that folder and don't pull in new deps.",
     {("scope", "edit_path", "src/cache", None), ("forbid", "install", None, None)}),
    # two rules in one sentence
    ("no deleting, no force pushing", {("forbid", "delete", None, None), ("forbid", "force_push", None, None)}),
    ("ask before you delete or install anything", {("confirm", "delete", None, None), ("confirm", "install", None, None)}),
]

CORPUS_NEGATIVES = [
    "delete the unused imports", "push your changes when done", "remove the tests for the old API",
    "install pytest and run it", "the prod config lives in config/prod.yml", "force push is fine here, the branch is mine",
    "can you delete the build dir and rerun?", "we deleted the staging cluster last week", "the network module is in src/net.py",
    "curl the health endpoint and tell me what it returns", "run docker compose up and check the logs", "rm the .pyc files please",
    "go ahead and push", "the previous engineer asked us not to delete logs, but that's no longer relevant",
    "git push origin main failed yesterday, can you check why?", "What does 'force push' mean?", "add a test that deletes a temp file",
    "I don't think we need to delete anything for this task", "edit tests/test_auth.py to cover the new endpoint",
    "make sure the tests pass", "Install ripgrep if it's missing.", "touch src/__init__.py",
    "run kubectl get pods -n prod and paste the output", "No, the other one.",
    # extra traps: filler "no", task verbs after a non-action head, cue words used literally
    "no worries, push whenever", "never mind, push it", "don't forget to run the tests", "don't worry about prod for now",
    "no need to run the full suite", "check the logs and tell me what you see", "let me know when the tests pass",
    "only the parser is broken", "keep it simple", "stay focused on the parser", "leave a comment explaining why",
    "ask the user for their name in the CLI", "confirm the email address format with a regex", "avoid the temptation to over-engineer",
    "You can delete the build dir if you want.", "feel free to install whatever you need", "prod is down", "tests pass",
]


@pytest.mark.parametrize("text,expected", CORPUS_FIXED, ids=[p[0][:40] for p in CORPUS_FIXED])
def test_corpus_paraphrases(text, expected):
    got = _tuples(extract(text))
    assert got == expected, f"{text!r}: got {got}"


@pytest.mark.parametrize("text", CORPUS_NEGATIVES)
def test_corpus_negatives_yield_no_rule(text):
    assert extract(text) == [], text


def test_scoped_pronoun_needs_antecedent_in_same_message():
    assert extract("don't delete it") == []
    assert _tuples(extract("Fix tests/test_io.py. Then don't delete it.")) == {("forbid", "delete", "tests/test_io.py", None)}


def test_push_rule_subsumes_force_push():
    assert _tuples(extract("ask before any push or force push")) == {("confirm", "push", None, None)}
    # but a lone force-push rule, or one next to a different kind, survives
    assert _tuples(extract("never force push; ask before pushing")) == {("forbid", "force_push", None, None),
                                                                        ("confirm", "push", None, None)}


@pytest.mark.parametrize("text,action", [
    ("yes, but only the .pyc files", "any"), ("okay, but push to a feature branch, not main", "push"),
    ("Yes please, and then run the tests", "any"), ("Go ahead and delete them. Then push.", "delete"),
    ("proceed with caution", "any"), ("LGTM, ship it", "any"), ("yep, nuke it", "delete"),
])
def test_narrowed_and_followup_confirmations(text, action):
    got = extract_confirmations(text)
    assert got and got[0].action == action, (text, got)


@pytest.mark.parametrize("text", [
    "yes to the push, no to the force push", "yes I saw it, but don't delete yet", "go ahead with the refactor but not the deletion",
    "sure, but hold off on the delete", "ok but first run the tests", "yes please but not the config", "yes?", "what would you delete exactly?",
    "hold on, let me check", "I said no", "nah, leave it", "I'll delete it myself", "deleted", "the answer is no",
    "yes the tests pass now", "Delete the old migrations too", "no don't delete, just move them",
])
def test_contrastive_or_deferred_answers_are_not_confirmations(text):
    assert extract_confirmations(text) == [], text


# --- LLM extractor (stubbed; never calls the network in tests) -----------------------------

def test_llm_prompt_is_strict_json_schema():
    p = extract_llm.build_prompt("never delete anything")
    assert "JSON" in p and '"kind"' in p and "forbid" in p and "never delete anything" in p


def test_llm_extract_parses_stubbed_output(monkeypatch):
    payload = {"type": "result", "result": json.dumps({"rules": [
        {"kind": "forbid", "action": "delete", "target": None, "requires": None},
        {"kind": "bogus", "action": "delete"}]})}

    def fake_run(cmd, **kw):
        assert cmd[:2] == ["claude", "-p"] and "--model" in cmd and "haiku" in cmd
        assert "--output-format" in cmd and "json" in cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(extract_llm.subprocess, "run", fake_run)
    rules = extract_llm.extract_llm("never delete anything", turn=2)
    assert _tuples(rules) == {("forbid", "delete", None, None)}
    assert rules[0]["source"]["via"] == "llm"


def test_llm_extract_handles_failure(monkeypatch):
    def fake_run(cmd, **kw):
        raise FileNotFoundError("claude not installed")
    monkeypatch.setattr(extract_llm.subprocess, "run", fake_run)
    assert extract_llm.extract_llm("never delete anything") == []
