# M6 — Isolation / Regression Harness — Design (2026-11-07)

**Status:** design. No code yet. Design-only authorization granted 2026-11-07; implementation
requires a separate, explicit operator authorization.
**Project:** `apex-investor` (Phase 2). **Baseline:** HEAD `8b71264`, branch `phase1-hardening`.
Full suite at time of design: 763 passed, 5 skipped; Ruff clean; `git diff --check` clean.

---

## 1. What M6 is, and why it is the "key demo"

From the Phase 2 build plan (§6): *"M6 is the key demo: the deterministic report must be
byte-identical except for the new advisory section. That is the proof the AI did not touch
accounting or enforcement."*

M6 turns that sentence into a **permanent, re-runnable regression harness**: a test module that any
future change to Phase 2 (the future interpretation brain, adapter wiring, new advisory states,
prompt changes) can be run against, proving the claim mechanically rather than arguing it.

The isolation invariant M6 proves: **the advisory/interpretation pipeline can only ADD a clearly-
labelled section to the daily report. It cannot change any Phase 1 byte — ledger figures, risk
verdicts, alerts, prices, section order, or formatting.**

## 2. Honest inventory: what already exists (M5 accepted tests)

M6 does not start from zero. `tests/test_advisory.py` (accepted with M5) already proves:

| Existing M5 test | What it covers |
|---|---|
| `test_advisory_none_is_byte_identical_to_baseline` | `advisory=None` output is byte-identical (UTF-8) to the captured pre-M5 fixture `tests/fixtures/report_pre_m5.md` |
| `test_existing_sections_unchanged_with_advisory` | with a populated advisory section, every line BEFORE the advisory block is unchanged vs the baseline |
| `test_no_mutation_of_phase1_fields` | building with an advisory does not mutate ledger/risk/report fields |
| `test_report_does_not_import_citation_or_context` / `test_interpret_imports_no_phase1_math` | static import-isolation guards |
| `test_deterministic_section` | same inputs → identical advisory markdown |
| State tests: `None` / unavailable / empty / populated; injection-safe rendering (`_md_safe`); date/context rendering; fail-closed builder paths; render-failure fallback |

**Gap analysis — what M6 adds beyond M5 (the reason M6 exists):**

1. **A single consolidated harness** rather than scattered cases: one parameterized module that
   enumerates the full advisory-state matrix and, for EACH state, asserts the complete identity
   contract (prefix-bytes identity + suffix-is-advisory-only + console/markdown consistency).
2. **Byte-level prefix assertion**, not per-line: M5's `test_existing_sections_unchanged_with_advisory`
   compares logical lines; M6 asserts the exact UTF-8 byte prefix (everything before the advisory
   heading) is IDENTICAL to the `advisory=None` rendering — the strictest possible form.
3. **Hostile-advisory regression set**: a dedicated battery of adversarial `AdvisoryItem` payloads
   (newlines, `#`/`-`/backticks, control characters, ANSI escapes, extremely long lines, unicode
   look-alikes of the advisory heading itself) asserting that non-advisory bytes remain untouched
   AND the hostile payload renders inert (single escaped line).
4. **Console/markdown consistency across all states** (`to_console() == to_markdown().rstrip()`),
   which M5 tests only implicitly.
5. **A capture-and-refresh protocol for the baseline fixture** (documented procedure; fixture only
   regenerated deliberately, never silently).
6. **Regression-catch demonstration**: the harness is validated by (temporary, reverted) mutation
   runs showing it FAILS when isolation is broken — proving the harness itself is not vacuous.

## 3. Harness shape

New module `tests/test_m6_isolation.py` (additive; no existing test modified or removed):

```
FIXTURES
  _phase1_inputs()        one fixed, deterministic Phase 1 input set (ledger, prices, alerts,
                          quote checks, as-of) reused by every case — from the accepted M5 helpers.
  advisory_states()       parameterized matrix: None / unavailable / empty / single-finding /
                          multi-finding-multi-evidence / hostile-text battery.

CONTRACT (asserted for EVERY advisory state S):
  A1  md_none  = build_report(inputs, advisory=None).to_markdown()
      md_S     = build_report(inputs, advisory=S).to_markdown()
      the advisory heading is the FIRST divergence point, i.e. md_S is
      md_none[:cut] + ADVISORY_BLOCK(S) with cut == len(md_none) or at most the advisory heading;
      equivalently: md_none.encode() is a byte-prefix of nothing after the heading — asserted by
      exact prefix equality: md_S.startswith(prefix_of_md_none_up_to_advisory_heading)
      and md_none has NO advisory heading at all.
  A2  every non-advisory section of md_S equals the corresponding bytes of md_none
      (section order, headings, numbers, formatting — whole-prefix byte equality).
  A3  the advisory block appears EXACTLY once, starts with the canonical heading
      "## 7. Interpretation (advisory)", and is the LAST section.
  A4  to_console() == to_markdown().rstrip() for both md_none and md_S.
  A5  (state=None) md_none byte-identical to tests/fixtures/report_pre_m5.md (retains M5 guarantee).

HOSTILE BATTERY (subset of states):
  H1  claim/quote/title containing \n, \r\n, \t, control chars, C1 range, DEL
  H2  markdown-structural payloads: "# ", "## ", "```", "- ", "|", "](", backticks, HTML
  H3  a fake advisory heading embedded in a quote/claim ("## 7. Interpretation (advisory)" as text)
  H4  10k-character single-line payload
  H5  unicode confusables of the heading (fullwidth, homoglyphs)
      For each: A1–A4 still hold; the payload appears on exactly one escaped line.

MUTATION VALIDATION (self-test of the harness; run manually, NOT a permanent test):
  temporarily patch the advisory renderer to emit into section 3 (alerts); assert the harness
  FAILS; revert. Document the output in the implementation report as evidence the harness can fail.
```

## 4. Acceptance criteria (operator-editable)

- **AC-M6-1:** For every advisory state in the matrix, A1–A4 hold. Any single-byte change outside
  the advisory block fails the suite.
- **AC-M6-2:** The `advisory=None` byte-identity to the captured pre-M5 fixture remains green
  (M5 guarantee preserved, not weakened).
- **AC-M6-3:** Hostile battery: every payload renders inert; non-advisory bytes untouched.
- **AC-M6-4:** The harness demonstrably fails under a deliberate isolation-breaking mutation
  (evidence shown in the implementation report, mutation reverted).
- **AC-M6-5:** No existing test is modified or deleted; the new module is purely additive.
- **AC-M6-6:** Full suite green after implementation; Ruff clean; `git diff --check` clean;
  nothing staged/committed/pushed.

## 5. Boundaries (binding)

- NO changes to `report.py`, `interpret.py`, `advisory.py`, `context.py`, `citation.py`,
  `model_gate.py`, `m3_consumer.py`, `model_adapter.py`, ledger/risk/monitor/sanity — read-only
  consumption of accepted modules. (If review exposes a defect in an accepted module, it is
  REPORTED, not fixed, without separate authorization.)
- NO live model calls, no credentials, no network. Fixture `AdvisorySection` objects only.
- NO changes to `.gitignore` or the pre-existing `report.py` working-tree change.
- NO M3-consumer changes; NO docstring cleanup (stays parked).
- Nothing staged, committed, or pushed. No Apex Oracle Bot work.
- The baseline fixture `tests/fixtures/report_pre_m5.md` is NEVER regenerated during M6.

## 6. Known limitations (restated, mandatory)

- The harness proves isolation for the TESTED advisory states and payloads. It is a regression
  guard, not a formal proof over all possible payloads.
- Static import guards (`test_report_does_not_import_citation_or_context`,
  `test_interpret_imports_no_phase1_math`) complement but do not replace the byte-identity tests;
  M6 adds the dynamic proof, static guards remain in force unchanged.
- Byte-identity is defined against THIS report renderer version; if the renderer legitimately
  changes later, the capture-and-refresh protocol (§2.5) applies deliberately, with operator review.

## 7. Open questions for the operator (answer before/during implementation review)

1. Confirm the hostile battery scope (§3 H1–H5) is sufficient, or nominate additional payload
   classes you want covered.
2. Confirm the mutation-validation evidence belongs in the implementation REPORT rather than as a
   permanent test (my recommendation: report-only — a permanent "must fail" test is fragile).
3. Confirm M6 may reuse the M5 test helpers by importing them, versus duplicating fixture code
   (my recommendation: import — single source of truth; it does not modify the M5 file).
