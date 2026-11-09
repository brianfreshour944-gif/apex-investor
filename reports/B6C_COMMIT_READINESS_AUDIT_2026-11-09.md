# B6-C — Commit Readiness Audit (2026-11-09)

**Purpose:** determine the correct, reproducible commit boundary for the B6 re-freeze, before any
Git operation. **Read-only.** No staging, no committing, no deleting, no file modification.

**Headline:** the "four B6 files" are **not** a self-contained commit. A clean checkout of them
alone cannot import the CLI, cannot run `freeze-verify`, and cannot pass the test suite. The B6
change depends on an **uncommitted B1 foundation**. The correct boundary is a set, not four files.

---

## 1. Method

- Listed `HEAD`-tracked files; diffed the working tree against `HEAD`; traced imports from the CLI
  entry points and the new modules; identified data files read at runtime and by tests.
- Simulated a clean checkout **in `/tmp`** (outside the repo) of candidate file sets, and ran the
  verifier there. The repository working tree was not modified.

---

## 2. B1 foundation files required for a clean checkout

The B6 freeze chain is reproduced by exactly these files, all of which must be present:

**A. Scoring-closure members** (4; `SCORING_CLOSURE_MEMBERS`)

| File | Tracked at HEAD? | B6-touched? |
|---|---|---|
| `src/apex_investor/citation.py` | yes | no |
| `src/apex_investor/docstore.py` | yes | no |
| `src/apex_investor/model_gate.py` | yes (modified) | **yes** |
| `src/apex_investor/sanitize.py` | yes | no |

**B. New B1 source modules** (all **untracked** at HEAD)

| File | Role | Needed by |
|---|---|---|
| `src/apex_investor/evaluator_identity.py` | closure hash + freeze verification | CLI, `m3_consumer`, tests |
| `src/apex_investor/corpus_consistency.py` | G2 corpus/annotation consistency gate | CLI, G2 tests |
| `src/apex_investor/prompt_loader.py` | byte-exact approved-prompt loader | `render`, tests |
| `src/apex_investor/render.py` | G2 renderer v2 (test-only; no production importer) | `test_render` |
| `src/apex_investor/prompts/g2_system_prompt.txt` | the approved prompt bytes (pinned by SHA) | `prompt_loader` |

**C. Freeze artifacts** (both **untracked** at HEAD)

| File | Role |
|---|---|
| `data/model_gate/evaluator_closure.v1.json` | inventory (member hashes + closure hash) |
| `data/model_gate/freeze_authority.v1.json` | **authority (the trust anchor)** |

**D. G2 evidence** (all **untracked**; needed to run the G2 gate/tests, and are the pinned
annotation authority)

| File | Role |
|---|---|
| `data/model_gate/g2_annotations.json` | authoritative annotations (pinned by SHA in `corpus_consistency`) |
| `data/model_gate/g2_annotation_approval.json` | operator approval record (pinned by SHA) |
| `data/model_gate/g2_corpus.dev.json` | dev corpus |
| `data/model_gate/g2_corpus.heldout.json` | held-out corpus |
| `data/model_gate/g2_heldout_manifest.json` | held-out manifest |

**E. Modified tracked files that are part of the change**

| File | Why |
|---|---|
| `scripts/model_gate.py` | imports the new modules; `freeze-verify` / `frozen-run` subcommands |
| `src/apex_investor/m3_consumer.py` | imports `evaluator_identity` (schema-`"2"` validation) |
| `data/model_gate/frozen_run_manifest.example.json` | example manifest updated in B1 |
| `README.md` | B1 documentation of the freeze mechanism |
| `tests/test_model_gate.py` | **B6 tests + B1 additions**; imports `evaluator_identity` |
| `tests/test_m3_consumer.py` | imports `evaluator_identity` |

**F. New B1 test files** (all **untracked**)

`tests/test_corpus_consistency.py`, `tests/test_evaluator_identity.py`,
`tests/test_g2_answer_validation.py`, `tests/test_model_gate_freeze.py`,
`tests/test_model_gate_g2_gate.py`, `tests/test_prompt_loader.py`, `tests/test_render.py`.

---

## 3. Dependency mapping (why the four files are not enough)

Verified by isolated import simulation (`PYTHONPATH` pointed at a sandbox containing only the
candidate files):

| Sandbox contains | Result |
|---|---|
| **only the four B6 files** (`model_gate.py` + inventory + authority + test) | `import apex_investor.model_gate` → **fails** (`No module named 'apex_investor.citation'`); `evaluator_identity` / `corpus_consistency` **absent** → CLI and `freeze-verify` cannot run |
| the freeze set (`citation`+`docstore`+`sanitize`+`model_gate`+`evaluator_identity` + inventory + authority) | **`verify_freeze_authorization` SUCCEEDS**; closure `77411945…` reproduced; members correct |

Import edges:

```
scripts/model_gate.py
  ├─ apex_investor.corpus_consistency        (untracked)
  ├─ apex_investor.evaluator_identity        (untracked)
  └─ apex_investor.model_gate  ── .citation, .docstore   (tracked)
src/apex_investor/m3_consumer.py ── .evaluator_identity    (untracked)
src/apex_investor/render.py ── .docstore, .prompt_loader   (prompt_loader untracked)
```

**Consequence:** committing `model_gate.py` without `evaluator_identity.py`, `corpus_consistency.py`,
`prompt_loader.py`, and the two freeze artifacts yields a commit whose `scripts/model_gate.py`
**cannot be imported** and whose test suite **cannot run**. The re-freeze is unreproducible.

---

## 4. Which files belong in one atomic commit

The **minimum reproducible unit** is the **freeze + G2 foundation + B6 delta**, staged together:

**Core (required for reproducibility of the new authority):**
- `src/apex_investor/{citation.py, docstore.py, sanitize.py}` — already at HEAD (no-op to include)
- `src/apex_investor/model_gate.py` — **B6 delta** + B1 additions
- `src/apex_investor/evaluator_identity.py` (new)
- `src/apex_investor/corpus_consistency.py` (new)
- `src/apex_investor/prompt_loader.py` (new)
- `src/apex_investor/prompts/g2_system_prompt.txt` (new)
- `data/model_gate/evaluator_closure.v1.json` (new)
- `data/model_gate/freeze_authority.v1.json` (new)
- `data/model_gate/g2_annotations.json`, `g2_annotation_approval.json`,
  `g2_corpus.dev.json`, `g2_corpus.heldout.json`, `g2_heldout_manifest.json` (new)
- `scripts/model_gate.py`, `src/apex_investor/m3_consumer.py` (modified)
- `data/model_gate/frozen_run_manifest.example.json` (modified)
- `README.md` (modified)
- `tests/test_model_gate.py`, `tests/test_m3_consumer.py` (modified)
- `tests/test_{corpus_consistency,evaluator_identity,g2_answer_validation,model_gate_freeze,model_gate_g2_gate,prompt_loader,render}.py` (new)
- `src/apex_investor/render.py` (new — test-only, but `test_render` needs it)

**A note on atomicity:** this is one coherent unit (the G2 freeze + B6 re-freeze). Whether to split
into "B1 foundation" and "B6 delta" commits is a judgment call; if split, the **B1 commit must come
first** so the B6 commit is reproducible. A single commit is simpler and matches "reproducible from a
clean checkout".

---

## 5. Files that must remain UNTRACKED (do not commit)

| Path | Reason |
|---|---|
| `data/model_gate/.refreeze_backup_2026-11-09/` | **recovery path** to the old B1 state; **not source**; would corrupt the tree if committed |
| `reports/B4_THRESHOLD_CALIBRATION_EVIDENCE_2026-11-09.md` | research evidence (working note) |
| `reports/B5_ABSTENTION_ELIGIBILITY_DEFECT_2026-11-09.md` | research evidence |
| `reports/B6R_REFREEZE_CHANGE_CONTROL_2026-11-09.md` | change-control record |
| `reports/B6C_*` (this file) | audit record |
| `.gitattributes` | **pre-existing** (mtime 01:33, not from B6); operator's call, not part of B6 |
| `.openhands/` | gitignored already (agent memory) |

**Warning:** the backup directory is **not** gitignored. `git add -A` / `git add .` would commit it.

---

## 6. Exact staging commands (for the operator; NOT executed here)

Stage the boundary explicitly by path — never `git add -A`:

```bash
# B6 delta + B1 foundation (one reproducible commit)
git add \
  src/apex_investor/model_gate.py \
  src/apex_investor/evaluator_identity.py \
  src/apex_investor/corpus_consistency.py \
  src/apex_investor/prompt_loader.py \
  src/apex_investor/render.py \
  src/apex_investor/prompts/g2_system_prompt.txt \
  data/model_gate/evaluator_closure.v1.json \
  data/model_gate/freeze_authority.v1.json \
  data/model_gate/g2_annotations.json \
  data/model_gate/g2_annotation_approval.json \
  data/model_gate/g2_corpus.dev.json \
  data/model_gate/g2_corpus.heldout.json \
  data/model_gate/g2_heldout_manifest.json \
  scripts/model_gate.py \
  src/apex_investor/m3_consumer.py \
  data/model_gate/frozen_run_manifest.example.json \
  README.md \
  tests/test_model_gate.py \
  tests/test_m3_consumer.py \
  tests/test_corpus_consistency.py \
  tests/test_evaluator_identity.py \
  tests/test_g2_answer_validation.py \
  tests/test_model_gate_freeze.py \
  tests/test_model_gate_g2_gate.py \
  tests/test_prompt_loader.py \
  tests/test_render.py

git diff --cached --stat      # review exactly what will be committed
```

Confirm the backup and reports are **not** staged:

```bash
git status --porcelain | grep -E 'refreeze_backup|reports/B[456]'
# expect: all still '??' (untracked, unstaged)
```

> Note: `citation.py` / `docstore.py` / `sanitize.py` are already at HEAD unchanged, so they need no
> `git add`. `.gitattributes` is deliberately excluded.

---

## 7. Reproducibility verification (performed, in a sandbox)

A clean checkout of the **freeze set** was simulated in `/tmp` and the verifier run there:

```
sandbox = {citation, docstore, sanitize, model_gate, evaluator_identity}
        + {evaluator_closure.v1.json, freeze_authority.v1.json}
verify_freeze_authorization(root=sandbox, authority_sha256=<new anchor>)
  -> closure_sha256 = 77411945fc28d16ca7810b0ee2b455db49d07fadaca85913ac79b15d5688e2d4
  -> members = [citation.py, docstore.py, model_gate.py, sanitize.py]
RESULT: REPRODUCED (new authority verifies from a clean checkout)
```

Conversely, the **four-files-only** sandbox **failed to import** `model_gate` and had no
`evaluator_identity`/`corpus_consistency`, so `freeze-verify` could not run at all.

---

## 8. Findings summary

| ID | Finding | Severity |
|---|---|---|
| B6C-1 | The "four B6 files" are not a self-contained commit; the CLI cannot import and tests cannot run without the untracked B1 foundation. | HIGH |
| B6C-2 | Both freeze artifacts (`evaluator_closure.v1.json`, `freeze_authority.v1.json`) are **untracked at HEAD**; the original B1 freeze was never committed. | HIGH |
| B6C-3 | The G2 annotation authority + corpora + approval are **untracked**; the G2 gate and its tests require them. | MEDIUM |
| B6C-4 | `git add -A` is unsafe: it would commit the recovery backup and research reports. | MEDIUM |
| B6C-5 | Tracked `tests/test_m3_consumer.py` and `tests/test_model_gate.py` import the untracked `evaluator_identity`; `m3_consumer.py` does too. | HIGH |
| B6C-6 | `render.py` is test-only (no production importer) but is required by `test_render.py`. | LOW |
| B6C-7 | The freeze set alone reproduces the new authority from a clean checkout (verified). | INFO |

---

## 9. Recommended boundary

**One atomic commit** containing the B6 delta **and** the full B1 foundation (closure members + new
modules + prompts + freeze artifacts + G2 evidence + modified tracked files + new tests), staged
**explicitly by path**. Keep the backup directory, the reports, and `.gitattributes` untracked.

If a split is preferred, commit the **B1 foundation first**, then the **B6 delta**, so the B6 commit
is reproducible on its own.

---

## 10. Boundaries observed

- Read-only: no staging, committing, deleting, or file modification in the repository.
- Sandbox simulation occurred only under `/tmp` (outside the repo).
- No network, no model execution, no Git operations.
- Working tree unchanged: HEAD `712192b`, staged empty, porcelain `31`.
- This document is the only file created; left **untracked and unstaged**.

### Files inspected
- `git ls-files`, `git status --porcelain`, `git diff --name-only`, `.gitignore`.
- `scripts/model_gate.py` (import block), `src/apex_investor/{evaluator_identity,corpus_consistency,prompt_loader,render,m3_consumer,model_gate}.py`.
- `tests/{test_model_gate,test_m3_consumer,test_corpus_consistency,test_evaluator_identity,test_g2_answer_validation,test_model_gate_freeze,test_model_gate_g2_gate,test_prompt_loader,test_render}.py`.
- `data/model_gate/*`.
