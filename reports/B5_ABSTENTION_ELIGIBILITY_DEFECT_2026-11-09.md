# B5 — Abstention Eligibility Defect Investigation (2026-11-09)

**Scope:** Apex Investor G2 model gate. Investigation only.
**Status:** evidence report. **No fix is implemented.** No threshold, corpus, annotation, or
production-code change; no model execution; no network; no Git operations. Any repair requires a
separate, explicit authorization.

---

## 0. Evidence classification (read first)

Results combine **code tracing** with **synthetic gate-behavior** runs (in-memory verdicts and the
real gate code). There are **no real G2 model runs** in the repository, so nothing here is
model-performance evidence; it characterizes the gate. No model was executed; no network was used.

---

## 1. Summary — why selection is deadlocked

The real `data/model_gate/g2_corpus.dev.json` contains **zero must-abstain scenarios**. Because the
abstention metric returns `0.0` on an empty denominator, and the threshold is `1.00`, **every
candidate fails eligibility on the dev corpus**, so `run_selection` can never approve a candidate.

```
dev corpus: must_abstain_total = 0
abstention_accuracy = 0 / 0  -> (empty-denominator branch) -> 0.0
eligibility: 0.0 < min_abstention(1.00)  ->  "abstention accuracy below the minimum"   (always)
run_selection(dev, heldout, ...) -> Selection(None, ..., "no candidate met every threshold on dev and held-out")
```

The defect is **not isolated to abstention.** It is an **empty-denominator convention** shared by
**three of the four rate metrics** (§5). Abstention is simply the one currently triggered.

---

## 2. Trace 1 — the empty-denominator calculation

All four rate metrics are `@property` members of `RunVerdict`
(`src/apex_investor/model_gate.py:571–586`). Three of them collapse an empty denominator to `0.0`;
only `citation_support_rate` uses an explicit `None` ("undefined") branch:

| Metric | Line(s) | Empty-denominator behavior |
|---|---|---|
| `scenario_accuracy` | 572–573 | `scenario_correct / scenarios_total if scenarios_total else 0.0` → **`0.0`** |
| `completeness_rate` | 576–577 | `must_answer_success / must_answer_total if must_answer_total else 0.0` → **`0.0`** |
| `abstention_accuracy` | 580–582 | `must_abstain_success / must_abstain_total if must_abstain_total else 0.0` → **`0.0`** |
| `citation_support_rate` | 583–586 | `if findings_total == 0: return None` → **`None`** |

The three `else 0.0` branches are **identical in form**. The `citation_support_rate` comment
(`return None  # undefined -> cannot satisfy any minimum`) documents the *intended* treatment of an
empty denominator — a treatment the other three metrics do not follow.

Observed (in-memory):

```
scenario_accuracy      denom=0 -> 0.0   -> ['scenario accuracy below the minimum']
completeness_rate      denom=0 -> 0.0   -> ['completeness below the minimum']
abstention_accuracy    denom=0 -> 0.0   -> ['abstention accuracy below the minimum']
citation_support_rate  denom=0 -> None  -> ['citation support rate is undefined (no findings produced)']
```

---

## 3. Trace 2 — how `abstention_accuracy` reaches `eligibility_failures`

1. **Counting.** `score_run` tallies abstention scenarios
   (`src/apex_investor/model_gate.py:905–910`):

   ```python
   if scenario["expectations"].get("must_abstain"):
       verdict.must_abstain_total += 1
       verdict.must_abstain_success += int(sres.correct)
   else:
       verdict.must_answer_total += 1
       verdict.must_answer_success += int(sres.correct)
   ```

   If the corpus has no abstention scenarios, `must_abstain_total` stays `0`.

2. **Metric.** `RunVerdict.abstention_accuracy` (`model_gate.py:580–582`) divides by
   `must_abstain_total`; with `0`, the `else` branch yields `0.0`.

3. **Eligibility.** `eligibility_failures` (`model_gate.py:938`) compares
   (`model_gate.py:959–960`):

   ```python
   if verdict.abstention_accuracy < thresholds.min_abstention:
       problems.append("abstention accuracy below the minimum")
   ```

   With `abstention_accuracy == 0.0` and `min_abstention == 1.00`, this **always** appends the
   failure. The message names "abstention accuracy", but the **cause is an empty denominator**, not a
   wrong abstention — a misleading diagnostic.

---

## 4. Trace 3 — `select()` and `run_selection()`

Both selectors consume `eligibility_failures` identically:

- **`run_selection`** (`src/apex_investor/model_gate.py:1089`), used by the CLI
  (`scripts/model_gate.py:174`), applies eligibility to **both corpora**
  (`model_gate.py:1154`):

  ```python
  if eligibility_failures(dv, thresholds) or eligibility_failures(hv, thresholds):
      continue
  ```

  The dev verdict always fails (empty abstention denominator), so `passing` stays empty and
  `run_selection` returns `Selection(None, …, "no candidate met every threshold on dev and held-out")`.

- **`select`** (`model_gate.py:994`), test-only, applies the same rule to a single corpus
  (`model_gate.py:1006`). Its reason string ("… on dev and held-out") is **cosmetic**: `select`
  sees one corpus. The CLI never calls `select`.

End-to-end reproduction (read-only, in-memory; a candidate that answers every answerable scenario
correctly on both corpora):

```text
run_selection -> candidate=None  reason='no candidate met every threshold on dev and held-out'
  cand-1  dev[acc=1.0 comp=1.0 abst=0.0 sup=1.0]  heldout[acc=1.0 comp=1.0 abst=1.0 sup=1.0]
```

**Affected code paths**

| Role | Location |
|---|---|
| Counting | `model_gate.py:905–910` |
| Metric (empty denom → 0.0) | `model_gate.py:580–582` |
| Eligibility comparison | `model_gate.py:959–960` |
| Both-corpus application | `model_gate.py:1154` (`run_selection`); `model_gate.py:1006` (`select`) |
| Corpus shape | `data/model_gate/g2_corpus.dev.json` (0 abstention scenarios) |

---

## 5. Scope — isolated to abstention, or shared?

**Shared.** The `else 0.0` convention appears in `scenario_accuracy`, `completeness_rate`, and
`abstention_accuracy`. Only `citation_support_rate` returns `None`. Whether each empty denominator
can actually occur depends on corpus composition:

| Metric | Denominator | Empty when… | Reachable today? |
|---|---|---|---|
| `abstention_accuracy` | `must_abstain_total` | corpus has 0 abstention scenarios | **YES — real dev (0)** → live deadlock |
| `completeness_rate` | `must_answer_total` | corpus has 0 answerable scenarios | **LATENT** — symmetric; would deadlock a pure-abstention corpus |
| `scenario_accuracy` | `scenarios_total` | corpus has 0 scenarios | **guarded** — `is_g2_corpus` rejects an empty scenario list (`corpus_consistency.py:220–223`) |
| `citation_support_rate` | `findings_total` | model produced 0 findings | **handled** — returns `None` → "undefined" failure (by design) |

**Corpus shapes (measured):**

| Corpus | scenarios | must-answer | must-abstain |
|---|---|---|---|
| `g2_corpus.dev.json` (real) | 3 | 3 | **0** |
| `g2_corpus.heldout.json` (real) | 3 | 2 | 1 |

**The consistency gate does not catch this.** `assert_genuine_g2_corpora_consistent(dev, heldout)`
**passes** on the real corpora (verified) — it checks annotation agreement, reference values, and
coverage of *present* scenarios, but **no rule requires a minimum number of abstention or answerable
scenarios**. So the deadlock occurs **downstream** of the gate, during scoring/selection.

**Symmetric latent path (demonstrated):** a corpus with only abstention scenarios would make
`completeness_rate = 0/0 = 0.0` and fail identically ("completeness below the minimum"). The defect is
a **systemic empty-denominator convention**, not an abstention-only bug.

---

## 6. Remediation approaches and their consequences (documented; none selected or implemented)

Presented for the operator. **No approach is recommended, and none is implemented.** Approaches
differ in *what layer* they change; note that `src/apex_investor/model_gate.py` was treated as a
**protected module** during the B1 work, so any change there requires explicit operator approval, and
`data/model_gate/g2_corpus.*.json` are **operator-authored / frozen** evidence.

**A. Corpus authoring — add abstention scenarios to the dev corpus.**
- Changes: `data/model_gate/g2_corpus.dev.json` (+ matching annotations, since the consistency gate
  requires every scenario to match its annotation).
- Consequence: restores a meaningful abstention signal on dev; the current gate then behaves as
  intended. Requires new corpus + annotation content (authoring), which is out of B5's scope.
- Risk: if dev is *intentionally* abstention-free (abstention measured only on held-out), this
  contradicts the design intent and adds evidence the split may not want.

**B. Metric semantics — return `None` (not-applicable) on an empty denominator, and let eligibility
skip a `None` metric.**
- Changes: `model_gate.py` metric properties (make `abstention_accuracy`/`completeness_rate` return
  `None` when the denominator is 0, matching `citation_support_rate`) and the `eligibility_failures`
  comparisons (skip when the value is `None`).
- Consequence: a metric with no applicable cases is simply not evaluated. Makes the four metrics
  **consistent**. Changes the meaning of eligibility: "no abstention scenarios" no longer implies
  failure.
- Risk: must decide `None`-handling **per metric** and confirm no case should still fail closed
  (e.g., a corpus that *should* have abstentions but was mis-specified would now pass silently).

**C. Eligibility guard — skip a metric whose denominator is zero, at the eligibility layer only.**
- Changes: `eligibility_failures` (`model_gate.py:938`) only; metrics keep their `0.0`.
- Consequence: smallest-surface change; the deadlock clears for dev. But the metric still reports a
  misleading `0.0` in the artifact (`as_dict`), so downstream readers see "abstention 0.0" with no
  "not applicable" signal.
- Risk: hides the empty-denominator condition rather than representing it; inconsistent with the
  `None` convention already used for support.

**D. Consistency validation — require every genuine corpus to contain ≥1 abstention AND ≥1
answerable scenario (fail closed at the gate).**
- Changes: `corpus_consistency.py` validation.
- Consequence: the **current dev corpus would be rejected** by the gate (fail closed) — forcing a
  corpus fix before any run. Strong guarantee that the denominators are never empty.
- Risk: disruptive; it converts a silent deadlock into a hard gate failure and mandates corpus
  authoring (approach A) as a precondition. Also constrains future corpus designs.

**E. Evaluate abstention across the dev+held-out union rather than per-corpus.**
- Changes: `run_selection` aggregation (and possibly the metric).
- Consequence: abstention judged on combined coverage; dev-only runs are not penalized for the
  split's shape.
- Risk: larger semantic change; a per-corpus gate becomes a pooled gate; must define the union's
  denominators and keep the dev/held-out distinction meaningful elsewhere.

**Cross-cutting note:** approaches A and D touch **operator-authored data / validation** and would
require data authoring; B, C, and E touch **`model_gate.py` (protected)**. None is a "threshold
change" — the threshold `1.00` is a correct requirement for a corpus that *has* abstention cases; the
defect is the interaction of the threshold with an empty denominator.

---

## 7. Why it matters (failure path)

- The gate cannot reach a selection on the real corpora **at all**, regardless of model quality.
- The reported reason — "no candidate met every threshold on dev and held-out" — reads as a **model
  failure**, but is a **structural artifact** of the corpus/metric interaction. This is a diagnostic
  hazard: an operator could misattribute the outcome to the candidate.
- Any future frozen-run against the current dev corpus inherits the deadlock.

---

## 8. Open decisions (for the operator; not answered here)

1. Is the dev corpus **intended** to contain abstention scenarios, or is abstention meant to be
   measured only on held-out?
2. For a metric with an **empty denominator**, should the gate treat it as **not applicable (skip)**
   or **fail closed**?
3. Should the consistency gate **require a minimum scenario mix** (≥1 abstention, ≥1 answerable)?
4. Which layer should carry the fix — corpus, metric semantics, eligibility, or validation — and does
   the fix touch the protected `model_gate.py`?

---

## 9. Boundaries observed

- Read-only, in-memory, offline. No model execution, no network, no credentials.
- No threshold, corpus, annotation, or production-code change. `B3` and `B4` remain unchanged.
- The two real corpora and the annotation authority were **not** modified.
- This report is the **only** file created; it is left **untracked and unstaged**.

### Files inspected
- `src/apex_investor/model_gate.py` — metric properties 571–586; `score_run` 896–932;
  `eligibility_failures` 938–966; `select` 994–1018; `run_selection` 1089–1162.
- `src/apex_investor/corpus_consistency.py` — `is_g2_corpus` 212–223; validation ~300–340.
- `scripts/model_gate.py` — CLI entry 173–174.
- `data/model_gate/g2_corpus.dev.json`, `data/model_gate/g2_corpus.heldout.json`.
- `tests/test_model_gate.py` (no test covers a zero abstention/completeness denominator).

### Working-tree status at time of writing
- Apex Investor: HEAD `712192b`, staged empty; this report is the sole new (untracked) file.
- Apex Oracle Bot: untouched.
- No Git operations were performed.
