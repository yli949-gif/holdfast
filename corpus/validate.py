#!/usr/bin/env python3
"""Schema validator for the holdfast corpus. Exit 0 iff every file is valid.

Usage: python corpus/validate.py [corpus_dir]
"""
import json
import os
import sys

CATEGORIES = {"delete", "force_push", "push", "commit", "edit_path", "run_cmd", "network", "install", "touch_prod"}
RULE_KINDS = {"forbid", "confirm", "order", "scope"}
RULE_ACTIONS = CATEGORIES | {"any"}
REQUIRES = {"tests_passed", "user_confirmed"}
TOOL_TIERS = {"plain", "adversarial", "benign"}
CONSTRAINT_TIERS = {"plain", "paraphrase", "negative"}
TOOL_SHAPES = {
    "Bash": {"command"},
    "Edit": {"file_path", "old_string", "new_string", "replace_all"},
    "Write": {"file_path", "content"},
    "MultiEdit": {"file_path", "edits"},
    "NotebookEdit": {"notebook_path", "new_source"},
    "Read": {"file_path"},
    "Grep": {"pattern"},
    "Glob": {"pattern"},
}
EXPECTED_KEYS = {"categories", "paths", "cmds", "debatable", "debatable_categories", "prod_if_main",
                 "suspicious", "test_runner"}

errors = []


def err(where, msg):
    errors.append(f"{where}: {msg}")


def load(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line.strip():
                err(f"{path}:{n}", "blank line")
                continue
            try:
                rows.append((n, json.loads(line)))
            except json.JSONDecodeError as e:
                err(f"{path}:{n}", f"bad JSON: {e}")
    return rows


def check_unique_ids(path, rows, prefix):
    seen = set()
    for n, r in rows:
        i = r.get("id")
        if not isinstance(i, str) or not i.startswith(prefix):
            err(f"{path}:{n}", f"id must start with {prefix!r}: {i!r}")
        if i in seen:
            err(f"{path}:{n}", f"duplicate id {i}")
        seen.add(i)


def validate_toolcalls(path):
    rows = load(path)
    check_unique_ids(path, rows, "tc-")
    for n, r in rows:
        w = f"{path}:{n} ({r.get('id')})"
        for k in ("id", "tier", "tool_name", "tool_input", "cwd", "expected", "note"):
            if k not in r:
                err(w, f"missing key {k}")
        if r.get("tier") not in TOOL_TIERS:
            err(w, f"bad tier {r.get('tier')!r}")
        if r.get("cwd") != "/proj":
            err(w, "cwd must be /proj")
        tn = r.get("tool_name")
        if tn not in TOOL_SHAPES:
            err(w, f"unknown tool_name {tn!r}")
        else:
            ti = r.get("tool_input", {})
            missing = TOOL_SHAPES[tn] - set(ti)
            if missing:
                err(w, f"tool_input missing {sorted(missing)}")
            for pk in ("file_path", "notebook_path"):
                if pk in ti and not str(ti[pk]).startswith("/"):
                    err(w, f"{pk} must be absolute")
            if tn == "MultiEdit":
                for e in ti.get("edits", []):
                    if not {"old_string", "new_string"} <= set(e):
                        err(w, "MultiEdit edit entry missing old_string/new_string")
        exp = r.get("expected", {})
        extra = set(exp) - EXPECTED_KEYS
        if extra:
            err(w, f"unknown expected keys {sorted(extra)}")
        cats = exp.get("categories")
        if not isinstance(cats, list) or set(cats) - CATEGORIES:
            err(w, f"bad categories {cats!r}")
        elif cats != sorted(set(cats)):
            err(w, "categories must be sorted and unique")
        if "force_push" in (cats or []) and "push" not in cats:
            err(w, "force_push must co-occur with push")
        paths = exp.get("paths")
        if not isinstance(paths, list):
            err(w, "paths must be a list")
        else:
            for p in paths:
                if not isinstance(p, str) or not p.startswith("/"):
                    err(w, f"path not absolute: {p!r}")
            if paths != sorted(set(paths)):
                err(w, "paths must be sorted and unique")
            if paths and not ({"delete", "edit_path"} & set(cats or [])):
                err(w, "paths given but no path-bearing category (delete/edit_path)")
        if tn == "Bash":
            if not isinstance(exp.get("cmds"), list):
                err(w, "Bash items need expected.cmds (list of executables)")
        elif tn in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            if "edit_path" not in (cats or []):
                err(w, "editor tool calls must be labelled edit_path")
        else:
            if cats:
                err(w, f"{tn} tool must have no categories")
        dc = exp.get("debatable_categories")
        if dc is not None and (not isinstance(dc, list) or set(dc) - set(cats or [])):
            err(w, "debatable_categories must be a subset of categories")
        if r.get("tier") == "adversarial":
            if r.get("difficulty") not in (1, 2, 3):
                err(w, "adversarial items need difficulty in {1,2,3}")
        elif "difficulty" in r:
            err(w, "difficulty only on adversarial items")
        if exp.get("prod_if_main") and "push" not in (cats or []):
            err(w, "prod_if_main only makes sense on push items")
        if exp.get("prod_if_main") and "touch_prod" in (cats or []):
            err(w, "prod_if_main items must not also carry touch_prod")
        if r.get("tier") == "benign":
            bad = {"delete", "force_push", "install", "touch_prod"} & set(cats or [])
            if bad and not exp.get("debatable"):
                err(w, f"benign item carries firing categories {sorted(bad)} without debatable flag")
            if "edit_path" in (cats or []) and any(p.startswith("/proj/tests") for p in paths or []):
                err(w, "benign item edits under tests/")
    return rows


def validate_constraints(path):
    rows = load(path)
    check_unique_ids(path, rows, "c-")
    for n, r in rows:
        w = f"{path}:{n} ({r.get('id')})"
        for k in ("id", "text", "expected", "tier"):
            if k not in r:
                err(w, f"missing key {k}")
        if r.get("tier") not in CONSTRAINT_TIERS:
            err(w, f"bad tier {r.get('tier')!r}")
        if not isinstance(r.get("text"), str) or not r["text"].strip():
            err(w, "empty text")
        for rules in [r.get("expected", [])] + list(r.get("alt_expected", [])):
            if not isinstance(rules, list):
                err(w, "expected must be a list of rules")
                continue
            for rule in rules:
                if rule.get("kind") not in RULE_KINDS:
                    err(w, f"bad kind {rule.get('kind')!r}")
                if rule.get("action") not in RULE_ACTIONS:
                    err(w, f"bad action {rule.get('action')!r}")
                if rule.get("kind") == "order" and rule.get("requires") not in REQUIRES:
                    err(w, "order rule needs requires")
                if rule.get("kind") == "scope" and not rule.get("target"):
                    err(w, "scope rule needs target")
                if set(rule) - {"kind", "action", "target", "requires"}:
                    err(w, f"unknown rule keys {sorted(set(rule) - {'kind', 'action', 'target', 'requires'})}")
        if r.get("tier") == "negative" and r.get("expected"):
            err(w, "negative items must expect no rules")
        if r.get("tier") != "negative" and not r.get("expected"):
            err(w, "non-negative items must expect at least one rule")
    return rows


def validate_confirmations(path):
    rows = load(path)
    check_unique_ids(path, rows, "cf-")
    for n, r in rows:
        w = f"{path}:{n} ({r.get('id')})"
        for k in ("id", "text", "pending_action", "expected"):
            if k not in r:
                err(w, f"missing key {k}")
        if r.get("pending_action") not in CATEGORIES:
            err(w, f"bad pending_action {r.get('pending_action')!r}")
        exp = r.get("expected", {})
        if not isinstance(exp.get("is_confirmation"), bool):
            err(w, "expected.is_confirmation must be bool")
        if exp.get("is_confirmation"):
            if exp.get("action") != r.get("pending_action"):
                err(w, "confirmed action must equal pending_action")
        elif exp.get("action") is not None:
            err(w, "non-confirmation must have action null")
    return rows


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    tc = validate_toolcalls(os.path.join(d, "toolcalls.jsonl"))
    cs = validate_constraints(os.path.join(d, "constraints.jsonl"))
    cf = validate_confirmations(os.path.join(d, "confirmations.jsonl"))
    if errors:
        print("\n".join(errors))
        print(f"FAIL: {len(errors)} error(s)")
        sys.exit(1)
    print(f"OK: {len(tc)} tool calls, {len(cs)} constraints, {len(cf)} confirmations")


if __name__ == "__main__":
    main()
