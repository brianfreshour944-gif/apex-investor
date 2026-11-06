# Investment Policy

Binding rules for Apex Investor. Adapted from `Apex_oracle_bot`'s
`STRATEGY_VALIDATION_STANDARDS.md`, which were earned the hard way: every crypto strategy we
tested failed these bars. They apply here unchanged.

---

## 1. The index is the default

The **S&P 500 index fund** is the benchmark and the default holding. Any individual stock or
alternative must **beat the index after costs, out of sample, across rolling periods** before it
earns real weight. The prior (SPIVA, S&P Dow Jones Indices): ~90% of active large-cap U.S.
funds trail the S&P 500 over 15 years, and over 20 years it is worse. Assume the same applies
here until proven otherwise.

## 2. Validation standards (mandatory for any strategy claim)

A proposed approach must pass **all** of these. Failing any one is a NO-GO:

1. **Rolling start dates** — test every monthly start, run forward; report the distribution and
   the fraction of negative starts. Never one favorable window.
2. **Bear-market survival** — a prolonged downturn as its own scenario, judged separately.
3. **Correct cash accounting** — a running ledger reconciling purchases, proceeds, cost basis,
   fees, cash, and open positions.
4. **Mark-to-market risk** — unrealized losses and capital tied up; report drawdown, worst
   unrealized, time underwater. Closed-trade win rate alone is meaningless.
5. **Fair benchmarks** — compare against the index, DCA, and lump-sum on identical capital and
   fees. State plainly if it loses.
6. **Out-of-sample** — never optimize against the judging data.

Also mandatory: realistic costs (commission + spread + slippage per side), pre-registered
hypotheses, honest limitations, and prominent corrections (never silently overwrite a result).

**Core lesson:** a strategy is not proven because it made money over a historical period — it
must not depend on *when* you started.

## 3. Evidence rule

Every recommendation must state:

- **Claim** — one falsifiable sentence.
- **Evidence** — the source (filing, price data, report) with a date.
- **Reason** — why it implies the action.
- **What would change the view** — the falsifier.

No recommendation without a source. Reasoning alone is not evidence.

## 4. Risk and position rules (initial)

- **No leverage. No shorting. No options** (until separately authorized).
- **Concentration cap:** no single stock above a set fraction of the portfolio (start 10%).
- **Speculative tier:** a smaller, capped sleeve; tighter limits; mandatory gain protection.
- **Cash is a position.** Holding cash is allowed and expected when nothing warrants action.
- **Trailing-gain protection** on any position up sharply (see Phase 1 spec).
- **Kill-switch:** a portfolio drawdown breach halts new buys and requires human review.

## 5. Authority

- The system **monitors, researches, and recommends**. It does **not** place orders.
- You approve every trade.
- When uncertain, **fail closed** — do nothing and say so.
- The goal is to avoid mistakes that permanently damage the account, not to maximize next
  month's return.
