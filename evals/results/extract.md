# Extractor hardening: natural language → rules (2026-10-31)

Scope: `holdfast/rules.py` (`extract_rules`, `extract_rules_llm`, `parse_llm_rules`, schema), `tests/test_extract.py`,
`evals/extract_eval.py`, `evals/corpus/constraints*.jsonl`. Reproduce every table with the commands in §7.

## 1. Summary

- **B1 is fixed.** "Don't change anything yet." used to become `forbid modify paths=['yet/**']`, a standing rule. It is
  now `forbid modify` (universal) with `expires="next_turn"`. The same applies to "for now", "at this point", "not yet",
  "hold off", "until I say" (on a universal modify) and "no edits" inside a "for now" sentence.
- **B6 is fixed.** "Only edit files under src/api/; src/core/ is vendored and must not be modified." now gives two rules:
  `forbid modify outside src/api/**` and `forbid modify on paths src/core/**`.
- **Precision held up on every corpus.** On the final code, no negative item fired on any of the four corpora (v2 dev 0/30,
  v2 held-out 0/24, blind 0/15, v1 held-out 0/30). The baseline fired on 3 v2 negatives and 2 blind negatives.
- **Recall on a corpus I never saw is still weak.** v1 `corpus/heldout` was written by someone else and never viewed. On it
  the extractor goes from 57/108 to 65/108 exact (kind+action level). 41 of its 43 misses are silent: no rule at all,
  never a wrong rule. The pattern system is precise but incomplete. Recall has to come from the LLM fallback.
- **`claude -p --model haiku` works in this environment.** On a fixed 15-item blind sample it scored 13/15 alone. The
  hybrid (pattern first, LLM only when the pattern finds nothing and the text has a cue) scored 15/15. Latency was
  2.5–4.1 s per call (median 2.8 s). Cost was $0 because it runs on the subscription.

## 2. Schema changes (the monitor consumes these)

| field | values | meaning |
|---|---|---|
| `Rule.expires` | `""` (standing), `"next_turn"` | A temporary hold. It lapses at the next user prompt that asks for work. Unknown values are rejected by `from_dict`. `describe()` appends "(temporary: until the user's next request)". |
| `Scope.exclude_paths` | list of globs | **In scope = NOT under any exclude glob.** "Only edit src/api/" becomes `forbid modify exclude_paths=["src/api/**"]`. Containment is strict: deleting the parent `src` IS in scope. Targets outside the project (`/tmp/x`, `~`, `../`) are never in scope, which fixes **B4** (scratch `tee /tmp/...`). Non-path effects are out of scope. `UNKNOWN` returns `None` and `EVERYTHING` returns `True`. `describe()` says "never modify outside src/api/**". |

`as_dict()` leaves out `scope.exclude_paths` when it is empty, so v1 on-disk state keeps its exact shape. `from_dict()`
loads old dicts unchanged. `test_rules.py` passes without modification.

**Not done here, and needed by the monitor owners:**
1. `handle_prompt` has to drop `expires="next_turn"` rules on the next work-asking prompt. Until it does, a hold behaves
   like a standing forbid, but now with a sane scope.
2. "Don't commit directly to main" becomes `forbid commit branches=[main]`. However, shell.py emits commits with target
   `HEAD`, so the rule never matches until the monitor resolves the current branch.
3. The monitor's LLM gate (`not rules and not AFFIRM and len>15`) should also require `rules.has_constraint_cue()`.
   Across the 69 negatives in the v2 and blind corpora, the current length gate would send 64 to the LLM (about 3 s each).
   Adding the cue gate cuts that to 30, and it drops no positive that the LLM would need.

## 3. Method

- **v2 corpus** `evals/corpus/constraints.jsonl`: 143 items (89 positive, 54 negative). Tiers: plain 26, paraphrase 42,
  embedded-in-task 11, temporary 10, negative 54. It covers every kind and action, scope rules, passive voice, holds,
  coordination ("X and don't Y") and tricky negatives: permissions, releases, questions, statements, and descriptions of
  code to write ("write a hook that blocks force pushes").
  - Each label is a list of `{kind, action, paths, exclude_paths, branches, pattern, command, prerequisite, expires}`.
  - Paths are compared as sets after collapsing alias expansions (`tests` → `tests/**`).
  - `pattern` and `command` labels are example strings that the predicted regex must match.
  - `{"prod": true}` is shorthand for the three-rule production bundle.
  - `alt_expected` holds genuinely ambiguous alternatives, for example "Don't push yet" as a hold or as a confirm.
- **Split:** frozen 60/40 by sha1(id) rank, giving dev 86 and held-out 57. The corpus was written and committed before any
  code change (commit `8db3baa`, with the baseline recorded in the message).
- **Contamination: the v2 held-out split is NOT blind.** While writing the TDD tests, I turned the task brief's example
  phrases into unit tests. The brief was also the source of most corpus items, so a large share of held-out items appear
  verbatim in `tests/test_extract.py`. I report its numbers below but they do not measure generalisation. Two sets do:
  - **`evals/corpus/constraints_blind.jsonl`** (43 items: 28 positive, 15 negative). Written after the extractor was built
    and scored once before any further change.
  - **v1 `corpus/heldout/constraints.jsonl`** (108 items, written by someone else). I never read it. Only aggregate scores
    were printed during development, and I did not tune on them.
- The v1 corpora use a different schema (`kind ∈ forbid/confirm/order/scope`, nine actions). They are scored at the
  `(kind, action)` level after mapping v2 rules onto v1 keys:
  - `require_before` → `order`
  - `exclude_paths` → `scope edit_path`
  - `modify` → `edit_path`
  - `run` → `run_cmd`
  - any rule with a prod pattern → `touch_prod`
  - `freeze` → `forbid`

  Some v1 labels conflict with v2 design choices. For example, v1 expects "never commit X" to give `forbid commit`, while
  v2 deliberately emits nothing.

## 4. Results: pattern extractor, before → after

"Exact" means the predicted rule set equals the label (or an `alt_expected`). "Partial" means kind and action are right
but the scope or expiry is wrong.

| corpus / split | n | baseline exact | baseline negatives fired | final exact | final negatives fired | final rule P / R |
|---|---|---|---|---|---|---|
| v2 dev (developed on) | 86 | 48 (56%); 20/56 pos; 9 partial | 2/30 | **86 (100%)** | 0/30 | 1.00 / 1.00 |
| v2 held-out, *contaminated, see §3* | 57 | 36 (63%); 13/33 pos; 8 partial | 1/24 | 57 (100%) | 0/24 | 1.00 / 1.00 |
| **blind v2**, first and only blind score | 43 | 17 (40%); 4/28 pos | 2/15 | **29 (67%)**; 14/28 pos | 0/15 | 0.94 / 0.53 |
| blind v2 after post-hoc fixes (no longer blind) | 43 | — | — | 42 (98%) | 0/15 | 1.00 / 0.97 |
| v1 `corpus/` (seen, not tuned on); kind+action | 102 | 64 (63%) | 0/28 | 99 (97%) | 0/28 | — |
| **v1 `corpus/heldout/` (never read)**; kind+action | 108 | 57 (53%); 27/78 pos | 0/30 | **65 (60%)**; 35/78 pos | 0/30 | — |

The honest generalisation estimate for the deterministic extractor is the two bold blind rows: 40% → 67% on my blind set
and 53% → 60% on the v1 held-out. Neither lost precision. The post-hoc fixes after the blind score were all generic
patterns, each pinned by a test in `TestPostBlindFixes`:
- "X needs my explicit OK"
- "changes are restricted to X"
- "make edits inside X"
- "hand-edited"
- "absolutely no …"
- "I need you to …"
- "until I've reviewed it"
- "once you've started"
- anaphora "them"
- appositive cuts (", it's shared with…")
- refusing a whole task sentence as the subject of a passive predicate

## 5. LLM vs pattern vs hybrid

**Setup.**
- `extract_rules_llm` runs `claude -p --model haiku --output-format json --json-schema <LLM_SCHEMA> --tools "" --no-session-persistence`.
- It runs from a neutral temp cwd, so project hooks and CLAUDE.md don't leak in.
- `parse_llm_rules()` validates the output:
  - drops unknown kinds, actions and expires values, and bad regexes
  - forces `prerequisite="tests_pass"` on `require_before`
  - normalises dirs to `dir/**`
  - strips remote names from branches
- It returns `[]` on any failure, including a missing CLI (covered by a test).

**Sample.** The first 15 items of the blind set by sha1(id) rank: 8 positive, 7 negative. That is 15 `claude -p` calls in
total, cached in `evals/results/extract_llm_cache.json`.

| extractor (same 15 blind items) | exact | positives exact | negatives fired | rule P / R |
|---|---|---|---|---|
| pattern (blind-time code) | 12/15 | 5/8 | 0/7 | 1.00 / 0.70 |
| pattern (final code) | 14/15 | 7/8 | 0/7 | 1.00 / 0.90 |
| llm (haiku) alone | 13/15 | 6/8 | 0/7 | 0.75 / 0.60 |
| **hybrid** (pattern; LLM only if pattern is empty AND `has_constraint_cue`) | **15/15** | 8/8 | 0/7 | 1.00 / 1.00 |

**Latency.** 15 calls: median 2.82 s, min 2.47 s, max 4.05 s. A cold first call outside the eval took 6.2 s. The hybrid
would have called the LLM on 4/15 items with the blind-time pattern code and on 2/15 with the final code.

**LLM errors** (both were representation errors, not hallucinated constraints):
- "Under no circumstances push to production" → `forbid push pattern=prod`. The label is the three-rule prod bundle.
- "keep your changes within the cli module" → `outside cli/**`. The label is `**/cli/**, **/cli.py`.

Neither error is harmful in practice. The LLM extracted nothing on any of the 7 negatives.

**How often the hybrid gate fires** on the final code (pattern empty AND cue present), without calling the LLM:

| corpus | items | gate fires | on negatives (wasted call, ~3 s) | on positives (recall the LLM could add) |
|---|---|---|---|---|
| v2 constraints | 143 | 23 | 23 | 0 |
| blind | 43 | 8 | 7 | 1 |
| v1 corpus | 102 | 6 | 3 | 3 |
| v1 heldout | 108 | 39 | 5 | **34** |

**Recommendation.** Ship the hybrid behind `HOLDFAST_LLM_EXTRACT=1`. On the v1 held-out, 34 of the pattern system's 43
silent misses pass the cue gate, so the LLM is where most of the remaining recall lies. The cost is about 3 s on a prompt
with a cue that the patterns miss. LLM output is schema-validated and marked `source="llm"`.

I did not score the LLM on the full v1 held-out, because the brief capped testing at ≤15 items. That run would be about
39 calls and is the obvious next measurement.

## 6. Remaining failures and reasons

**On my blind set (after fixes):**
- b-026 "Stay off the main branch: no direct pushes to it." The pattern system has no branch anaphora ("it" = main) and
  no "stay off <branch>" template. The LLM is the intended fallback.

**On the v1 held-out:** 43 misses, of which 41 produce no rule and 2 produce a wrong rule. By expected label:

| expected (v1) | missed |
|---|---|
| confirm delete | 10 |
| forbid delete | 6 |
| confirm push | 5 |
| scope edit_path | 4 |
| forbid network | 4 |
| forbid force_push | 3 |
| install (confirm or forbid) | 4 |
| other | 7 |

The two wrong rules:
- `forbid delete` where the label is `confirm delete`: a soft "unless …" form.
- `forbid deploy` where the label is `touch_prod`: a v1 vs v2 vocabulary difference.

Below the 0.6 mark the long tail is paraphrase. The template lexicon is finite and these phrasings fall outside it. That
is the design trade-off: precision is protected by refusing to emit a rule when a template or noun phrase doesn't
resolve, and it is the job the hybrid's LLM call is gated for.

**Known design limits** (deliberate and documented in tests):
- **Unscopable objects produce no rule.** Examples: "the API keys are off-limits", "never commit secrets", "don't change
  the function signature", "for now, leave the database schema alone". A universal rule would block the actual task
  (the lesson from B1).
- **Temporary holds only cover universal modify.** A hold on an unresolvable object is dropped. "Don't delete the old
  handlers for now" gives nothing, because a universal delete hold would block the rest of the turn's task.
- **"Until I say so" is split by action.** On a universal modify it is a temporary hold. On push, commit, delete or deploy
  it is a `confirm` (one confirmation per call). "Unless I say so" stays a `forbid`, as the existing test requires.
- **"Keep X as it is" vs "keep X as is".** "As it is" / "as they are" is `freeze`; "as is" / "alone" / "untouched" is
  `forbid`. The corpus accepts both.
- **Module names** without a path ("the auth module") become `**/auth/**` + `**/auth.py`. That is a heuristic.
- **Prod.** `forbid` on prod is the three-rule bundle. `confirm deploy to prod` is a single `deploy` rule with a prod pattern.

## 7. Reproduce

```
python3 -m pytest -q tests/test_extract.py tests/test_rules.py                 # 324 passed
python3 evals/extract_eval.py --split dev --show                                # v2 dev
python3 evals/extract_eval.py --split heldout                                   # v2 held-out (contaminated)
python3 evals/extract_eval.py --corpus evals/corpus/constraints_blind.jsonl     # blind
python3 evals/extract_eval.py --v1                                              # v1 corpora, kind+action
python3 evals/extract_eval.py --corpus evals/corpus/constraints_blind.jsonl --llm --llm-sample 15 --llm-limit 0   # LLM from cache
```

The baseline numbers come from running the same scorer against `holdfast/rules.py` at commit `8db3baa`. That code is
identical in behaviour to `cb27a12`.

Other test files in the full suite have unrelated failures:
- `test_shell.py` fails because of concurrent shell.py work.
- One `test_hook.py` failure (`test_main_event_from_payload`) also fails with the original rules.py, so it is not caused
  by this change.
