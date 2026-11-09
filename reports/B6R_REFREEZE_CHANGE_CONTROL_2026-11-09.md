# B6-R — Re-freeze Change-Control Package (2026-11-09)

**Purpose:** prepare, for operator approval, the change-control record required before any
re-freeze of the Apex Investor G2 scoring closure. This document is **planning only**.

**Status of this document:** proposal. **No code change, no authority regeneration, no corpus
change, no threshold change, no model execution.** The original B1 freeze remains intact and
verified. Approval to *perform* the re-freeze is a separate, later decision.

> **Core principle (operator):** a new authority anchor establishes a **new trusted evaluator
> identity**. It does **not** retroactively make evidence produced under the old evaluator
> equivalent to evidence produced under the new one.

---

## 1. Original B1 authority anchor and frozen closure (current, unchanged)

| Item | Value |
|---|---|
| Authority anchor (SHA-256 of `data/model_gate/freeze_authority.v1.json`) | `c01ab9901c2bbff28f57562a73de2c7fef1ac80867245d7df4a894f25ae584d9` |
| Inventory file SHA-256 (`data/model_gate/evaluator_closure.v1.json`) | `151128235b615624b23c09daae7e9eda54f14de917baa5169e332555e1272539` |
| `closure_sha256` | `705caa2eed57c57f2e7fdb12cc6a09a7fb9b40a6d6a386bb837b861b972f47ed` |
| `format_version` (authority + inventory) | `"1"` |
| Authority `inventory_path` | `data/model_gate/evaluator_closure.v1.json` |

**Scoring closure members** (`SCORING_CLOSURE_MEMBERS`, `evaluator_identity.py:28–33`) and their
pinned hashes:

| Member | Pinned SHA-256 |
|---|---|
| `src/apex_investor/citation.py` | `49067e65c8bc5118ce4f8b811ed43976c6b14580d17395ccc8eb1d4da62c3c01` |
| `src/apex_investor/docstore.py` | `5655a7d7bf8b2a20ccf1c09b406c4d57a6e3579f8a4241d4a93455d839464edd` |
| `src/apex_investor/model_gate.py` | `1c433f41b1a5fa1dd2c98db895ed3fbc44aeb083477fa80e5c5e1f1c584e1207` |
| `src/apex_investor/sanitize.py` | `099a7c6723b9eccfd12e2c36226c57fffbb1531cda97c59fafe3a780858896dc` |

Verified this session: `verify_freeze_authorization(root=…, authority_sha256="c01ab990…")`
returns **PASS** (closure `705caa2e…`, 4 members).

---

## 2. Exact Option A changes to `model_gate.py`

**Selected behavior (B6):** when a metric's denominator is zero, the metric is **N/A (`None`)**,
not `0.0`. **Scope is limited to the abstention metric.** `completeness_rate` and
`scenario_accuracy` are **left untouched** (their empty-denominator behavior is deferred).

**Change 1 — `RunVerdict.abstention_accuracy`** (`src/apex_investor/model_gate.py:580–582`)

```python
# BEFORE
@property
def abstention_accuracy(self) -> float:
    return self.must_abstain_success / self.must_abstain_total if self.must_abstain_total else 0.0

# AFTER
@property
def abstention_accuracy(self) -> float | None:
    if self.must_abstain_total == 0:
        return None  # no abstention cases -> not applicable (not a failure)
    return self.must_abstain_success / self.must_abstain_total
```

**Change 2 — `eligibility_failures`** (`src/apex_investor/model_gate.py:959–960`)

```python
# BEFORE
if verdict.abstention_accuracy < thresholds.min_abstention:
    problems.append("abstention accuracy below the minimum")

# AFTER
abstention = verdict.abstention_accuracy
if abstention is not None and abstention < thresholds.min_abstention:
    problems.append("abstention accuracy below the minimum")
```

**Change 3 (consequential) — serialization.** `as_dict()` (`model_gate.py:614`) already emits
`"abstention_accuracy": self.abstention_accuracy`; with the change it serializes `null` for
empty-denominator runs. No code edit is required for this, but it is a **visible artifact shape
change** (see §3).

**Semantics preserved (must remain true after the change):**
- When abstention cases **exist**, `min_abstention = 1.00` is **fully enforced** (one miss fails).
- The **held-out** corpus (1 abstention case) still evaluates abstention.
- `run_integrity_ok` and **every other threshold** are unchanged.
- No threshold value changes; no corpus change.

---

## 3. Artifacts whose hash or behavior will change

**Hash changes (must be regenerated at re-freeze time — values are TBD here):**

| Artifact | What changes | Old → New |
|---|---|---|
| `src/apex_investor/model_gate.py` | member content | `1c433f41…` → **TBD** |
| `data/model_gate/evaluator_closure.v1.json` | `members["src/apex_investor/model_gate.py"]` and `closure_sha256`; file SHA | member `1c433f41…`→TBD; closure `705caa2e…`→TBD; file `15112823…`→TBD |
| `data/model_gate/freeze_authority.v1.json` | `expected_inventory_sha256`, `expected_closure_sha256`; **file SHA = the anchor** | `15112823…`→TBD; `705caa2e…`→TBD; anchor `c01ab990…`→**new anchor TBD** |

**Behavior changes (no hash impact on closure):**

| Artifact / surface | Change |
|---|---|
| `RunVerdict.as_dict()["abstention_accuracy"]` | `0.0` → `null` when `must_abstain_total == 0` |
| schema-`"2"` artifact `evaluator` record | `closure_sha256` and `members["…/model_gate.py"]` reflect the new closure |
| eligibility outcome on the real dev corpus | deadlock removed; abstention no longer disqualifies when absent |

**Tests / docs touched by the value change (not part of the closure):**

| File | Impact |
|---|---|
| `tests/test_model_gate_freeze.py` | derives `AUTHORITY_SHA` from the live file (`hashlib.sha256(AUTHORITY.read_bytes())`) → auto-adapts; no literal anchor pinned |
| `tests/test_evaluator_identity.py` | builds temporary authority/inventory pairs → auto-adapts |
| `tests/test_model_gate.py:102` | `assert v.abstention_accuracy == 1.0` (abstention *present*) → unaffected |
| **New test required** | empty denominator → `None` and eligibility **skips** it; abstention present → still enforced |
| `README.md` (B1 section) | prose references the closure; review for accuracy after re-freeze |
| `reports/B5_…md` | describes the defect; historical, not updated (see §4) |

**No literal old-anchor constant is pinned in any test**, so the suite is not hard-coded to
`c01ab990…`; the new anchor propagates automatically.

---

## 4. Keeping old-evaluator results distinguishable from new-evaluator results

1. **Closure hash is the discriminator.** Every schema-`"2"` artifact carries
   `evaluator.closure_sha256` and the member map. Old artifacts carry `705caa2e…`; new artifacts
   will carry the new closure hash. Any artifact can therefore be attributed to the evaluator
   that produced it, by hash.
2. **Do not reuse `705caa2e…`.** The new closure hash must differ (guaranteed, since
   `model_gate.py` bytes change).
3. **`m3_consumer` self-validates per artifact.** It checks
   `closure_sha256 == compute_closure_sha256(evaluator["members"])` using the **artifact's own**
   members map (`m3_consumer.py:261`), **not** the live authority. Consequence: an **old**
   schema-`"2"` artifact remains internally valid and consumable under the new code — it is not
   silently re-labelled as new.
4. **Recommended (decision, §7):** consider a format/schema-version bump (e.g. artifact schema
   `"3"`, or a `closure_format_version`) so a human reader can distinguish evaluator generations
   without computing hashes. If adopted, `m3_consumer` schema dispatch and `_SUPPORTED_SCHEMA_VERSIONS`
   would need matching support — a **separate change**, not part of the minimal Option A edit.
5. **Prohibition:** never treat an old-closure artifact as equivalent to a new-closure artifact.
   Mixing them in one selection set is a provenance error.

**Inventory of currently persisted scoring artifacts:** none found (no `model_selection_*.json`
anywhere; `data/model_gate/` holds only corpora, annotations, authority, inventory, and the example
manifest). So there is no existing artifact to migrate or invalidate — but the rule above still
governs any artifact produced from here on.

---

## 5. Independent verification requirements for the new authority

Before the new authority is accepted, all of the following must hold (each reproduced by fresh
output):

1. **Out-of-band anchor.** The new authority file's SHA-256 is computed independently
   (`sha256sum`) and supplied on the command line — never read from the inventory it authorizes.
2. **Chain verifies.** `freeze-verify --authority-sha256 <new-anchor>` returns success and prints
   the new `closure_sha256` and environment.
3. **Live-file recomputation matches.** The verifier recomputes each member's hash from the live
   files and the closure hash from the member map, and both match the authority's expectations
   (`verify_freeze_authorization`, `evaluator_identity.py:236–245`).
4. **Membership exact.** `expected_members` equals `SCORING_CLOSURE_MEMBERS` exactly.
5. **Old anchor rejected.** The **old** anchor `c01ab990…` must now **fail** against the new files
   (proving the change took effect and no stale authority survives).
6. **Environment identity present.** `python_implementation`, `python_version`, `os_system`,
   `os_release`, `architecture`, `libc`, `dependencies` are all recorded.
7. **Two-person / dual-channel check (recommended).** The new anchor is confirmed through a
   channel independent of the file that produced it.

---

## 6. Why the change does not invalidate previously accepted evidence

- **The frozen G2 evidence is data, not evaluator code.** The annotation authority
  (`g2_annotations.json`, `0bc0257b…`), `g2_annotation_approval.json`, `g2_corpus.dev.json`
  (`ec054cb5…`), and `g2_corpus.heldout.json` (`52570647…`) are **unchanged** and remain valid —
  they were never in the scoring closure.
- **No accepted scoring artifact exists** to invalidate (see §4).
- **The change is narrowly scoped** to how an *absent* abstention signal is treated. It does not
  alter scoring of any scenario that has abstention cases, and does not alter any other metric or
  threshold.
- **Caveat that must be stated:** the re-freeze **does** change the evaluator identity. Any
  artifact produced **before** the re-freeze belongs to the **old** evaluator and is **not**
  equivalent to one produced after; the two must never be pooled as if identical (§4).

---

## 7. Open decisions for the operator (to settle before the re-freeze is executed)

1. **Anchor acceptance:** accept the newly generated anchor, or require dual-channel confirmation?
2. **Schema/format version:** keep schema `"2"` (distinguish generations by `closure_sha256`
   only), or bump to `"3"` for human-readable generation marking? (The latter adds `m3_consumer`
   work beyond the minimal edit.)
3. **Deferred siblings:** `completeness_rate` and `scenario_accuracy` empty-denominator handling
   remain `0.0` for now — confirm that is intended (they are latent, not live).
4. **Change-control sign-off:** the exact approval phrase that authorizes the re-freeze execution.
5. **Rollback:** how to revert to anchor `c01ab990…` if the re-freeze is rejected after the fact.

---

## 8. Boundaries observed in preparing this package

- Documentation only. **No** code change, **no** authority regeneration, **no** corpus change,
  **no** threshold change, **no** model execution, **no** network, **no** Git operations.
- The original B1 freeze is **intact** (verified: anchor `c01ab990…`, closure `705caa2e…`).
- This document is the **only** file created; it is left **untracked and unstaged**.

### Files inspected
- `src/apex_investor/evaluator_identity.py` — `SCORING_CLOSURE_MEMBERS` 28–33;
  `build_inventory` 138–147; `build_authority` 149–164; `verify_freeze_authorization` 166–245.
- `src/apex_investor/model_gate.py` — metric properties 571–595; `as_dict` 601–630;
  `eligibility_failures` 938–966; `build_artifact` ~1270–1290.
- `src/apex_investor/m3_consumer.py` — schema-`"2"` evaluator validation ~240–272.
- `scripts/model_gate.py` — `_frozen_run`, `_ordinary`, subcommand dispatch.
- `tests/test_model_gate_freeze.py`, `tests/test_evaluator_identity.py`, `tests/test_model_gate.py`.
- `data/model_gate/{evaluator_closure.v1.json, freeze_authority.v1.json}`.

### Working-tree status at time of writing
- Apex Investor: HEAD `712192b`, staged empty; this document is the sole new (untracked) file.
- Apex Oracle Bot: untouched.
- No Git operations were performed.
