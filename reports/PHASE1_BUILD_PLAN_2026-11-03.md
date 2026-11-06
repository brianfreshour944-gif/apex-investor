# Phase 1 — Revised Build Plan (design; no code yet)

**Status:** design. No code, no broker, no orders. Awaiting operator approval to implement.
**Date:** 2026-11-03
**Supersedes:** the original `PHASE1_MONITORING_SPEC_2026-11-03.md` (which proposed an
automatic trailing sale). This plan is revised per `TRAILING_RULE_VALIDATION_2026-11-03.md`
and `EVIDENCE_AUDIT_2026-11-03.md`.

---

## 0. What changed and why

The trailing-gain rule was validated. It passed on DFNS but **failed as a general rule**: on a
pre-specified 505-name universe it fired on 47% of monthly entries and, in **65%** of firings
(46–74% depending on definition), the price later rose above the pre-fire peak — it sells
winners too early. The **alert** is valuable; the **automatic sale** is not.

So Phase 1 is now: **awareness first, concentration discipline second, no automatic selling.**

## 1. Scope

**In scope (five deliverables):**

1. **Portfolio ledger** — holdings, shares, cost basis, cash, realized & unrealized gains, fees.
2. **Market monitor** — unusual price moves, volume, company news, allocation drift.
3. **Position-risk controls** — configurable concentration limits, speculative-position limits,
   portfolio drawdown warnings.
4. **Alerts & explanations** — what happened, why it matters, what deserves attention, with the
   evidence cited.
5. **No automatic orders** — you approve every trade.

**Explicitly out of scope (Phase 1):** stock selection, any AI interpretation, broker
connection, real money, live orders, tax logic, automatic selling of any kind.

## 2. Architecture — deterministic first

| Component | Responsibility | Type |
|---|---|---|
| `ledger` | positions, cost basis, cash, fees, P&L | deterministic |
| `prices` | fetch & cache daily (and intraday where free) prices + volume | data |
| `monitor` | compute signals (moves, volume, drift, drawdown) | deterministic |
| `rules` | concentration / speculative / drawdown checks | deterministic |
| `alerts` | assemble and deliver explanations with evidence | deterministic |
| `cli` / `report` | show state, emit the daily briefing | console |

**No AI in Phase 1.** If it can be arithmetic, it must be arithmetic. (AI interpretation is
Phase 2, one advisory brain, and it never decides.)

## 3. Data model (draft)

```
Position(name, ticker, shares, cost_basis_total, opened_date, tag)
   tag ∈ {core, growth, speculative}      # drives which limits apply
Ledger(cash, positions[], transactions[])
Transaction(date, ticker, side, shares, price, fee, note)
PriceBar(date, ticker, close, volume, adj_close)   # split/dividend adjusted
```

Everything is append-only; state is reconstructable from `transactions` (the standard that
caught the Apex accounting bug).

## 4. Signals (each cites its evidence)

| Signal | Default | Notes |
|---|---|---|
| Unusual gain | position ≥ +25% from cost within 20 days | awareness, not action |
| Extraordinary gain | ≥ +100% from cost | "review now" |
| Unusual volume | volume ≥ 5× 20-day average | corroborates a move |
| Gain erosion | gave back ≥ 30% of peak gain **since a big run-up** | advisory only |
| Allocation drift | holding drifts ≥ 5 pp beyond target | rebalancing prompt |
| Drawdown | portfolio ≥ X% off its peak | warning (see §6) |

The **gain-erosion signal is advisory**: it alerts and explains; it does **not** recommend a
sale by default, because the evidence shows automatic trailing sales cost more than they save.
It may *suggest* review for positions tagged **speculative** only.

## 5. Position-risk controls

- **Concentration limit** — max % per single name. **Initial setting: 10%, to be evaluated.**
  Honest basis (from the audit): the cap roughly halves drawdown *and* return; risk-adjusted
  return is unchanged. It is a **drawdown-control dial**, not a proven performance edge.
- **Speculative limit** — total % in `speculative`-tagged names (initial 15%).
- **Drawdown warning** — portfolio drawdown thresholds (e.g. −15% warn, −25% escalate).
- **Cash** — a legitimate position; no requirement to be fully invested.

## 6. Alerts and explanations (the evidence rule)

Every alert states: **what happened** (with numbers), **why it matters**, **what evidence**
(source + date), and **what would change the view**. No alert without a source. This is the
same contract used in `Apex_oracle_bot`, which kept its research honest.

**Delivery:** console + a written daily/weekly briefing file. (Channel TBD — open question.)

## 7. Milestones

| # | Milestone | Acceptance test |
|---|---|---|
| M1 | Ledger + reconciliation | Reconstruct a portfolio from transactions; cash and P&L match by hand; **tests** for cost-basis and fee math |
| M2 | Price ingestion + cache | Fetch and store daily bars; split/dividend adjustment verified on a known case (e.g. DFNS 1:8 and 1:125) |
| M3 | Monitor signals | Unit tests per signal; a synthetic series triggers each exactly once |
| M4 | Risk controls | Concentration/speculative/drawdown checks with boundary tests |
| M5 | Alerts + briefing | Reproduce the **DFNS replay** as a live-style alert: "extraordinary gain on 2024-12-17" with evidence |
| M6 | Replay harness | Run M1–M5 over the operator's real holdings (DXCM/INUV/DFNS) and the S&P panel; confirm outputs |

**M5 is the key demo:** the system must have *noticed* DFNS — the original problem — as an
alert, without pretending it could have known the top.

## 8. Validation standards (inherited, mandatory)

From `INVESTMENT_POLICY.md`: rolling start dates, bear-market survival, correct cash accounting,
mark-to-market risk, fair benchmarks, out-of-sample. Any rule promoted beyond "advisory" must
pass them. The trailing sale failed them; that is why it is not in Phase 1.

## 9. Non-goals and guardrails

- **No automatic selling.** Ever, in Phase 1.
- **No broker credentials.** Simulation and local data only.
- **No AI decisions.** Deterministic rules and arithmetic.
- **No selection.** Phase 1 does not choose what to buy.
- Fail closed: when uncertain, report "no action warranted" and say so.

## 10. Operator decisions (locked 2026-11-03)

| Open question | Decision |
|---|---|
| Alert channel | **Console + a daily report file** |
| Individual-stock limit | **10%, configurable** |
| Speculative allocation limit | **15%, configurable** |
| Speculative tagging | **Operator assigns the tag**; the system may *suggest* changes for approval |
| Automatic selling | **Never in Phase 1** |

**Ledger seeding (important):** the old **DXCM, INUV and DFNS/NUKK holdings are kept as
historical TEST fixtures only** — they must **not** enter the new portfolio's starting capital.
The live ledger starts **clean**. Old losses and purchases do not carry over.

**Implementation order:** M1 (ledger + reconciliation) → M2 (price ingestion + verified
split/dividend adjustment) → M3 (monitoring signals + tests).

**DFNS replay = acceptance test, not a tuning target.** The software is not fitted to DFNS; it
must simply *notice* the 2024-12-17 move when replayed, as one case among many.
