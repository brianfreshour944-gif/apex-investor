# Apex Investor

A long-term, evidence-driven **investment manager**. Its goal is to build wealth over 5–10
years while avoiding mistakes that can permanently damage the account — not to maximize
short-term profit.

**Status:** design phase. No real money, no broker connection, no live orders.
**Separate from** `Apex_oracle_bot` (the crypto bot). This project shares no credentials,
state, or code with it. Apex_oracle_bot is frozen and untouched.

---

## The problem this solves

You held a stock that surged enormously while you were at work, then gave back the gain before
you acted. You were also holding speculative microcaps that lost 80–99%. The goal is not to
predict the market — it is to **pay attention, apply discipline, and default to the index**.

## Core principles

1. **Long-term horizon.** Months to years, not days.
2. **The index is the default.** Any individual stock must *earn* its place by beating the
   index after costs, out of sample. Most professional managers do not clear this bar
   (SPIVA: ~90% of active large-cap funds trail the S&P 500 over 15 years).
3. **Evidence is mandatory.** No recommendation without a source and a falsifiable reason.
4. **Doing nothing is a valid outcome.** The system should prefer "no action" to a bad action.
5. **Deterministic software decides and enforces; AI only interprets.** AI never does
   arithmetic and never enforces limits.
6. **You approve every trade.** The system monitors, researches, and recommends. It does not
   place orders.

## Architecture

Three jobs, three tools — do not blur them:

| Job | Tool | Examples |
|---|---|---|
| Data gathering & math | **Deterministic software** | prices, financials, position sizes, drift, risk limits |
| Interpretation | **AI (advisory)** | read a filing/Fed statement/news; judge if a thesis holds |
| Decisions & enforcement | **Rules + your approval** | what to hold, whether a limit is breached |

## Roadmap

- **Phase 1 — Monitoring (build first).** Portfolio ledger, risk engine, monitor, and an
  advisory trailing-gain signal (no automatic selling). Validated on the real DFNS/NUKK
  December 2024 case.
- **Phase 2 — Interpretation (one brain, not four).** An advisory AI that reads filings/news
  and drafts explanations with citations.
- **Phase 3 — Selection (only if it earns it).** Test whether a stock/ETF selector beats the
  index out of sample, across rolling periods, after costs. Default expectation: probably not.

## Hard rules

- No real money, no broker credentials, no live orders until Phase 1–2 are validated and you
  explicitly authorize otherwise.
- Every recommendation cites its evidence.
- The index is the default; deviations must be justified and measured against it.
- Fail closed: when uncertain, do nothing and say so.

## Price integrity

Three gates guard every quote before it can reach valuation or risk:

1. **Freshness (per ticker).** A quote older than `MAX_PRICE_AGE_DAYS` is treated as missing, so
   a stale cache entry can never pass as a live price. Each ticker is checked individually — one
   fresh ticker cannot mask a stale one.
2. **Sanity.** The latest quote is compared with the previous valid close. A large move is
   **flagged for verification, not blindly rejected** — an earnings gap, a reverse split, or a
   known corporate action can all be legitimate. Only an extreme move with no explanation blocks
   valuation and risk. Thresholds are configurable (`PriceSanityLimits`). A flagged quote and its
   evidence are preserved in the report.
3. **Single basis.** All valuation and gain maths use the as-printed, split-adjusted `close`.
   `adj_close` (which differs by dividends) is retained only for audit and verification, so the
   snapshot and the alerts can never disagree for a dividend payer.

Optional `data/known_actions.json` lets the operator declare corporate actions that explain a
large move, so the guard flags rather than blocks them.

## Model gate CLI — run from the repository root

The offline model-quality gate is `scripts/model_gate.py`. Its default paths for the detector spec and
lexicon (`data/model_gate/detector_spec.v1.json`, `data/model_gate/lexicon.v1.json`) are **relative to
the current working directory**. Invoke the CLI from the repository root; from any other directory it
fails closed with a `detector spec not found` error unless every path is overridden explicitly. This
documents the existing requirement — it does not change CLI behavior or any default path.

```
python scripts/model_gate.py \
  --config <config.json> --freeze-manifest <manifest.json> \
  --dev-corpus <dev.json> --dev-runs <dev-runs.json> \
  --heldout-manifest <heldout-manifest.json> --heldout-runs <heldout-runs.json>
```

Run the test suite from the repository root as well (`pytest tests/`).

### Freeze authorization — evaluator identity (B1)

A frozen run must be provably scored by a known evaluator. The evaluator is not a single file: its
scoring closure is the set of modules whose code can change a score:

- `src/apex_investor/model_gate.py`
- `src/apex_investor/citation.py`
- `src/apex_investor/docstore.py`
- `src/apex_investor/sanitize.py`

Two source-controlled artifacts define and authorize that closure:

- `data/model_gate/evaluator_closure.v1.json` — the **inventory**: the exact member set and each
  member's SHA-256, plus a deterministic `closure_sha256` (sha256 over the canonical, sorted
  `{path: sha256}` map).
- `data/model_gate/freeze_authority.v1.json` — the **authority**: pins the inventory file's SHA-256
  (`expected_inventory_sha256`), the expected `closure_sha256`, and the exact `expected_members`.

Three distinct SHA-256 values are involved and must never be conflated:

- the **authority anchor** — the authority file's own SHA-256, supplied out of band on the command
  line (this is the trust root);
- the **inventory** — the inventory file's SHA-256, which the authority pins as
  `expected_inventory_sha256`;
- the **closure** — `closure_sha256`, the hash of the member map, carried by both the inventory and
  the authority.

The **authority file's own SHA-256 is the trust anchor and must be supplied independently** (on the
command line), never read from the inventory it authorizes. The file that is *verified* (the
inventory) is never the file that *authorizes* (the authority).

Two explicit subcommands perform fail-closed verification:

```
# verify the authority -> inventory -> closure -> members -> environment chain (no scoring)
python scripts/model_gate.py freeze-verify --authority-sha256 <authority-file-sha256>

# a freeze-required scoring run: verify FIRST, then score
python scripts/model_gate.py frozen-run --authority-sha256 <authority-file-sha256> \
  --config <config.json> --freeze-manifest <manifest.json> \
  --dev-corpus <dev.json> --dev-runs <dev-runs.json> \
  --heldout-manifest <heldout-manifest.json> --heldout-runs <heldout-runs.json>
```

Verification refuses (exit 2, no artifact) on: a missing or mismatched authority SHA-256; an
inventory whose bytes do not match `expected_inventory_sha256`; a membership set that differs from
`expected_members`; any member file whose SHA-256 differs; a `closure_sha256` that does not match the
recomputed value or the authority's expected value; or a missing/mismatched required
execution-environment field.

The execution-environment identity is recorded discretely: `python_implementation`, `python_version`,
`os_system`, `os_release`, `architecture`, `libc` (`[name, version]`), and `dependencies`
(`stdlib-only`).

The **bare command remains the ordinary, backward-compatible path** (`run` is an explicit alias); it
is unchanged and requires no authority. Freeze enforcement is reachable only through the explicit
subcommands, so it cannot be bypassed by omitting a flag.

Artifact provenance: a `frozen-run` artifact is schema `"2"` and carries the verified identity in an
`evaluator` record — the closure hash, the member `path: sha256` mapping, the environment identity,
and the pinned data-artifact identities (`data_artifacts`). It is written during the original
exclusive write and never backfilled. `prompt_sha256` is recorded as `null` until the executed prompt
artifact is threaded through the pipeline; the null is explicit and is not a substituted value. An
ordinary run is schema `"1"` and has no `evaluator` record (backward compatible).

The `--freeze-manifest` file and the authority chain are **separate layers**: the manifest binds a run
to its config, prompt version, and detector spec, while the authority chain binds it to the evaluator.
The freeze manifest does not carry an evaluator hash; evaluator identity is established solely by the
authority chain.
