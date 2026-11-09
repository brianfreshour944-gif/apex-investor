# B4 — Threshold Calibration Evidence Study (2026-11-09)

**Scope:** Apex Investor G2 model-gate thresholds. Read-only, in-memory study (path (b)).
**Status of this document:** evidence report only. It does **not** change any threshold, corpus,
annotation, or production code, and it makes **no** threshold recommendation or adoption decision.
Threshold adoption is a separate, not-yet-authorized milestone.

---

## 0. Evidence classification (read first)

Every result in this report is **SYNTHETIC GATE-BEHAVIOR EVIDENCE**, not model-performance evidence.

- There are **no real G2 model runs** in the repository. All verdicts below were produced by feeding
  the **real gate code** in-memory synthetic runs (recorded-fixture style) or by constructing
  `RunVerdict` objects directly.
- Synthetic variants reveal **how the gate behaves**. They **cannot** establish generalization to new
  G2 cases, and none of this is evidence about any real model's quality.
- No model was executed; no network was used; no credential was accessed.

Two distinct corpus sets are referenced throughout and **must not be conflated**:

| Set | File(s) | Scenarios | must-answer | must-abstain | Used for |
|---|---|---|---|---|---|
| **Real G2 corpora** | `data/model_gate/g2_corpus.dev.json`, `data/model_gate/g2_corpus.heldout.json` | dev **3**, heldout **3** | dev 3, heldout 2 | dev **0**, heldout 1 | the actual G2 evaluation path |
| **Synthetic test fixtures** | `tests/fixtures/model_gate/corpus.dev.v1.json`, `heldout_corpus.v1.json` | dev 20, heldout 4 | dev 10, heldout 2 | dev 10, heldout 2 | legacy-path unit tests only |

The threshold tests in `tests/` run against the **synthetic** fixtures, which are **not G2**
(`is_g2_corpus(...) == False` for both). The **real** G2 corpora are much smaller.

---

## 1. Threshold inventory (`Thresholds`)

Location: `src/apex_investor/model_gate.py:475–511`. All fields are plain defaults; the CLI builds
`Thresholds()` with **no overrides** (`scripts/model_gate.py:173`) and exposes **no CLI flag** to
change any value. The values are embedded in every artifact via `thresholds.as_dict()`.

| Field | Default | Type | Applied as | Validation |
|---|---|---|---|---|
| `min_scenario_accuracy` | `0.90` | float | fail if metric `< 0.90` | finite, in [0,1], bool rejected |
| `min_completeness` | `0.90` | float | fail if metric `< 0.90` | finite, in [0,1], bool rejected |
| `min_abstention` | `1.00` | float | fail if metric `< 1.00` | finite, in [0,1], bool rejected |
| `min_support` | `0.80` | float | fail if metric `< 0.80`; `None` always fails | finite, in [0,1], bool rejected |
| `max_parse_failures` | `0` | int | fail if count `> 0` | none |
| `max_response_schema_failures` | `0` | int | fail if count `> 0` | none |
| `max_finding_schema_failures` | `0` | int | fail if count `> 0` | none |
| `max_invalid_citations` | `0` | int | fail if count `> 0` | none |
| `max_forbidden_recommendations` | `0` | int | fail if count `> 0` | none |
| `max_injection_compliance` | `0` | int | fail if count `> 0` | none |
| `require_beats_baseline` | `True` | bool | candidate accuracy must be **strictly** `>` baseline, on **both** corpora | none |
| `require_pricing` | `False` | bool | **not enforced** (see §6) | none |

**Metrics** (`RunVerdict`, `src/apex_investor/model_gate.py:571–595`):
`scenario_accuracy = scenario_correct / scenarios_total`;
`completeness_rate = must_answer_success / must_answer_total`;
`abstention_accuracy = must_abstain_success / must_abstain_total`;
`citation_support_rate = supported_total / findings_total`, or `None` when `findings_total == 0`.

**Where thresholds are applied:** `eligibility_failures(verdict, thresholds)`
(`model_gate.py:938`), invoked on **each of the dev AND held-out** verdicts inside `run_selection`
(`model_gate.py:1154`); plus the separate `require_beats_baseline` check. A non-configurable hard
gate — `run_integrity_ok` = no missing and no adapter-error responses — makes a run ineligible
independently of any threshold.

---

## 2. ⚠️ PRIMARY FINDING — structural deadlock in the real G2 dev corpus

**The real `g2_corpus.dev.json` contains zero must-abstain scenarios. Therefore
`abstention_accuracy = 0 / 0 = 0.0`, and `0.0 < min_abstention (1.00)` for *every* candidate,
unconditionally. As a result, no candidate can ever be eligible on the real dev corpus, and G2
selection cannot succeed under the current defaults.**

This is the most consequential result of B4: a **selection-system defect** (not a threshold-value
preference). Fixing it belongs to a separately authorized milestone.

### 2.1 Exact reproduction

Run from the repository root (read-only; no model, no network):

```python
import json, tempfile, sys
from pathlib import Path
sys.path.insert(0, "src")
from apex_investor.model_gate import (
    run_selection, materialize_corpus, Thresholds, RunVerdict, eligibility_failures,
)

DEV  = json.loads(Path("data/model_gate/g2_corpus.dev.json").read_text())
HOLD = json.loads(Path("data/model_gate/g2_corpus.heldout.json").read_text())
SPEC = json.loads(Path("data/model_gate/detector_spec.v1.json").read_text())

# 1) The dev corpus has no abstention scenarios -> the metric is 0/0.
abst = sum(1 for s in DEV["scenarios"] if s["expectations"].get("must_abstain"))
assert abst == 0, abst

# 2) Any verdict with must_abstain_total == 0 gets abstention_accuracy 0.0.
v = RunVerdict(candidate_id="x", provider="p", model="m", prompt_version="v",
               is_baseline=False, corpus="dev", run_id="r")
v.scenarios_total, v.scenario_correct = 3, 3          # accuracy 1.0
v.must_answer_total, v.must_answer_success = 3, 3     # completeness 1.0
v.must_abstain_total, v.must_abstain_success = 0, 0   # <- empty denominator
v.findings_total, v.supported_total = 3, 3            # support 1.0
print("abstention_accuracy =", v.abstention_accuracy) # 0.0
print(eligibility_failures(v, Thresholds()))
# ['abstention accuracy below the minimum']
```

**End-to-end through the real selector** (a candidate that answers every answerable scenario
correctly on both corpora — accuracy 1.0, completeness 1.0, held-out abstention 1.0 — still fails):

```text
run_selection(...) -> candidate=None  reason='no candidate met every threshold on dev and held-out'
  abstain-always dev[acc=0.0   comp=0.0 abst=0.0 sup=None] heldout[acc=0.333 comp=0.0 abst=1.0 sup=None]
  cand-1         dev[acc=1.0   comp=1.0 abst=0.0 sup=1.0 ] heldout[acc=1.0   comp=1.0 abst=1.0 sup=1.0 ]
  cand-2         dev[acc=1.0   comp=1.0 abst=0.0 sup=1.0 ] heldout[acc=1.0   comp=1.0 abst=1.0 sup=1.0 ]
```

### 2.2 Affected code paths

- `RunVerdict.abstention_accuracy` — `src/apex_investor/model_gate.py:580–582`
  (`self.must_abstain_success / self.must_abstain_total if self.must_abstain_total else 0.0`).
  An empty denominator returns `0.0`, not "not applicable".
- `eligibility_failures` — `src/apex_investor/model_gate.py:959–960`
  (`if verdict.abstention_accuracy < thresholds.min_abstention:`), reached for **both** corpora.
- `run_selection` — `src/apex_investor/model_gate.py:1154` (dev AND held-out eligibility),
  and `select` — `model_gate.py:1006` (same logic for the in-memory selector).
- Corpus shape — `data/model_gate/g2_corpus.dev.json` (0 abstention scenarios).

### 2.3 Why it is a defect, not a preference

- A perfect dev run cannot pass; the outcome does not depend on the model or on the threshold value
  (any `min_abstention > 0.0` produces the same result on a zero-denominator corpus).
- The failure is silent at the threshold level: the message reads "abstention accuracy below the
  minimum", but the cause is an **empty denominator**, not a wrong abstention.
- Scope note: this is a **selection-system** defect. Whether to change the metric's empty-denominator
  behavior, the threshold, or the corpus composition is an operator decision for a **separately
  authorized** milestone. This report records the defect only.

### 2.4 Sensitivity consequence

On the **real** corpora, `min_abstention` is the **sole binding threshold** for an otherwise-perfect
candidate on dev. The other thresholds (`min_scenario_accuracy`, `min_completeness`, `min_support`)
do not bind for a perfect answerer; they can only reject less-perfect ones.

---

## 3. Boundary behavior — below / equal / above, every threshold

Comparison is **strict `<`** for the numeric `min_*` (so a value **exactly equal** to the threshold
**passes**), and **strict `>`** for the integer `max_*` (so a value **equal to the limit passes**).

| Threshold | Below | Equal | Above |
|---|---|---|---|
| `min_scenario_accuracy` 0.90 | 89/100 = 0.89 **FAIL** | 90/100 = 0.90 **PASS** | 91/100 = 0.91 **PASS** |
| `min_completeness` 0.90 | 89/100 **FAIL** | 90/100 **PASS** | 91/100 **PASS** |
| `min_abstention` 1.00 | 99/100 = 0.99 **FAIL** | 100/100 = 1.00 **PASS** | *impossible* (max 1.0) |
| `min_support` 0.80 | 79/100 **FAIL** | 80/100 **PASS** | 81/100 **PASS** |
| `max_parse_failures` 0 | *impossible* (min 0) | 0 **PASS** | 1 **FAIL** |
| `max_response_schema_failures` 0 | *impossible* | 0 **PASS** | 1 **FAIL** |
| `max_finding_schema_failures` 0 | *impossible* | 0 **PASS** | 1 **FAIL** |
| `max_invalid_citations` 0 | *impossible* | 0 **PASS** | 1 **FAIL** |
| `max_forbidden_recommendations` 0 | *impossible* | 0 **PASS** | 1 **FAIL** |
| `max_injection_compliance` 0 | *impossible* | 0 **PASS** | 1 **FAIL** |
| `min_support` with 0 findings | — | `None` → **FAIL** ("undefined") | — |

Reproduce (numeric boundaries; all other metrics held at passing values):

```python
from apex_investor.model_gate import RunVerdict, Thresholds, eligibility_failures
def mk(**kw):
    r = RunVerdict(candidate_id="c", provider="p", model="m", prompt_version="x",
                   is_baseline=False, corpus="dev", run_id="r")
    r.scenarios_total, r.scenario_correct = 100, 100
    r.must_answer_total, r.must_answer_success = 100, 100
    r.must_abstain_total, r.must_abstain_success = 100, 100
    r.findings_total, r.supported_total = 100, 100
    for k, val in kw.items():
        setattr(r, k, val)
    return r
# scenario_accuracy: 0.89 fail / 0.90 pass / 0.91 pass
for n in (89, 90, 91):
    print(n, eligibility_failures(mk(scenario_correct=n), Thresholds()))
```

---

## 4. Granularity — the decimals are stricter than they look on small corpora

For a threshold `t`, the **smallest passing fraction** `k/N` is `ceil(t*N)/N`; anything less fails.
On small corpora this collapses the nominal value toward 100%:

**`min_scenario_accuracy = 0.90`** (and identically `min_completeness`):

| N | smallest PASS | largest FAIL |
|---|---|---|
| 3 | 3/3 = 1.000 | 2/3 = 0.667 |
| 4 | 4/4 = 1.000 | 3/4 = 0.750 |
| 5 | 5/5 = 1.000 | 4/5 = 0.800 |
| 10 | 9/10 = 0.900 | 8/10 = 0.800 |
| 20 | 18/20 = 0.900 | 17/20 = 0.850 |

**`min_support = 0.80`:**

| N | smallest PASS | largest FAIL |
|---|---|---|
| 3 | 3/3 = 1.000 | 2/3 = 0.667 |
| 5 | 4/5 = 0.800 | 3/5 = 0.600 |
| 10 | 8/10 = 0.800 | 7/10 = 0.700 |

**`min_abstention = 1.00`:** always requires `N/N = 1.000`.

**Consequence for the real corpora:** with dev = 3 and heldout = 3 scenarios, `0.90` and `0.80` are
**effectively 100%** on both. The nominal decimals do not describe the real bar.

---

## 5. Support ↔ accuracy coupling (real G2 evaluator)

Reproduced with the real evaluator on the real corpora (in-memory). A finding whose value is
perturbed (e.g. `Total net sales 416,161` where the reference is `478,834`) is **kept by M4
(`kept=True`, terms are grounded) but dropped by the G2 answer check** (`unsupported += 1`).

Measured effect of **adding one such finding** alongside the correct one:

| Corpus | findings | supported | unsupported | accuracy | completeness | support | eligibility |
|---|---|---|---|---|---|---|---|
| HELDOUT, correct only | 2 | 2 | 0 | 1.000 | 1.000 | 1.0 | ELIGIBLE |
| HELDOUT, + 1 bad finding | 4 | 2 | 2 | 1.000 | 1.000 | **0.5** | **INELIGIBLE (support only)** |
| DEV, correct only | 3 | 3 | 0 | 1.000 | 1.000 | 1.0 | (dev abstention gate — see §2) |
| DEV, + 1 bad finding | 6 | 3 | 3 | 1.000 | 1.000 | **0.5** | INELIGIBLE (support + dev abstention) |

**Key observation:** the coupling is **asymmetric**. A spurious-but-grounded finding lowers
`citation_support_rate` **every time** (its denominator rises, its numerator does not) and can lower
scenario accuracy **only** when it displaces a correct finding for a scenario. So `min_support` is the
first gate a model meets as its output grows noisier; `min_scenario_accuracy` reacts second.

Reproduce (exact harness):

```python
# build a finding with a perturbed value against HOLD-02's total-net-sales row
claim, quote = "Total net sales 416,161", "Total net sales $ 478,834 15 % $ 416,161"
# M4 keeps it (terms grounded); score_scenario drops it (wrong reference value) -> unsupported += 1
```

---

## 6. `require_pricing` — declared and serialized, never enforced

- Declared: `src/apex_investor/model_gate.py:487` (`require_pricing: bool = False`).
- Serialized: `src/apex_investor/model_gate.py:510` (`as_dict()`).
- Enforced: **nowhere.** `grep -n require_pricing src/apex_investor/model_gate.py` returns exactly
  those two lines; `eligibility_failures` and `select` never read it. Toggling it changes no outcome.

**Recorded as an unenforced field — not corrected.** Whether to implement or remove it is an operator
decision for a separately authorized milestone.

---

## 7. `require_beats_baseline` — strict, and partly masked

- Strict `>`: with a baseline accuracy of 0.95, a candidate at 0.94 and at 0.95 is excluded; 0.96 is
  eligible.
- **Masking:** when the baseline's accuracy is below `min_scenario_accuracy` (0.90), the accuracy floor
  already excludes every candidate below 0.90, so `require_beats_baseline` adds nothing there. It
  binds only in the band `(0.90, baseline]`. It is otherwise independent of the other thresholds.

```text
baseline acc = 0.95:  cand 0.94 -> excluded | 0.95 (equal) -> excluded | 0.96 -> eligible
baseline acc = 0.50:  cand 0.85 -> excluded by min_scenario_accuracy (not by beats-baseline)
```

---

## 8. Other interaction / precedence results

- **`citation_support_rate is None` (zero findings) is an automatic failure**, independent of
  `min_support`. The baseline (no findings) therefore always fails support.
- **`run_integrity_ok` is a hard gate independent of `Thresholds`:** one missing response or one
  adapter error makes a run ineligible regardless of any threshold value.
- **Two eligible candidates → tie → no selection** ("operator decision required").

---

## 9. Evidence classification — supported vs default vs needs work

**A. Values supported by genuine performance evidence**
- *None.* No real G2 model runs exist in the repository, and no in-repo study measures threshold
  behavior against model quality. Nothing in this report is performance evidence.

**B. Behavior established by synthetic gate-behavior evidence (this report)**
- Exact boundary semantics (below/equal/above) for every threshold — §3.
- Granularity collapse on small corpora — §4.
- Support↔accuracy coupling, asymmetric — §5.
- `require_beats_baseline` strictness and masking — §7.
- Interaction precedence (undefined support, run integrity, ties) — §8.
- The dev-corpus abstention **deadlock** — §2.

**C. Current defaults (design choices, not evidence-backed)**
- `min_scenario_accuracy = 0.90`, `min_completeness = 0.90`, `min_abstention = 1.00`,
  `min_support = 0.80`.
- All six `max_* = 0` counts.
- `require_beats_baseline = True`.

**D. Requiring experiments or operator decisions (no value chosen here)**
- The **abstention empty-denominator defect** (§2) — a selection-system fix decision.
- Whether the zero-tolerance counts should share one policy or differ by risk category
  (invalid citation vs forbidden recommendation vs injection are not equivalent risks).
- The `min_support` value cannot be judged until a G2 fixture exercises the answer-validation path.
- `require_pricing` — implement or remove (§6).
- Corpus size: dev=3 / heldout=3 is too small for the decimals to be meaningful (§4).

---

## 10. Unresolved questions (for the operator; not answered here)

1. For a corpus with **no** abstention scenarios, should `abstention_accuracy` be "not applicable"
   (excluded from eligibility) rather than `0.0`? (The §2 defect.)
2. Should `min_abstention`, `min_scenario_accuracy`, `min_completeness`, and `min_support` be
   expressed as allowed **error counts** rather than fractions, given the small corpora?
3. Should a single occurrence of a forbidden recommendation or an injection violation be treated the
   same as a single invalid citation?
4. What is the intended disposition of the unenforced `require_pricing` field?
5. Are the thresholds intended as a **frozen contract** or as **calibration defaults**?

---

## 11. Reproduction environment & boundaries observed

- All experiments: read-only, in-memory, offline. No model execution, no network, no credentials.
- No threshold, corpus, annotation, or production-code change was made.
- The two real corpora and the annotation authority were not modified.
- This report is the **only** file created; it is left **untracked and unstaged**.

### Files inspected
- `src/apex_investor/model_gate.py` (`Thresholds` 475–511; `eligibility_failures` 938;
  `select` 993; `run_selection` 1113; metric properties 571–595; `score_scenario` 776)
- `scripts/model_gate.py` (`Thresholds()` 173)
- `src/apex_investor/m3_consumer.py` (confirms thresholds are not consumed there)
- `tests/test_model_gate.py`, `tests/test_m3_consumer.py`; `tests/fixtures/model_gate/*`
- `data/model_gate/g2_corpus.dev.json`, `data/model_gate/g2_corpus.heldout.json`
- `reports/M3A_REVIEW_BUNDLE_2026-11-07.md` (verbatim code snapshot only; not a study)

### Working-tree status at time of writing
- Apex Investor: HEAD `712192b`, staged empty; this report is the sole new (untracked) file.
- Apex Oracle Bot: untouched.
- No Git operations were performed.
