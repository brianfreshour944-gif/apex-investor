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

