# Phase 1 — Monitoring Spec (SUPERSEDED)

> **SUPERSEDED 2026-11-03 by `PHASE1_BUILD_PLAN_2026-11-03.md`.** The automatic trailing sale
> proposed below was validated and rejected as a general rule; the revised plan replaces it with
> alerts + concentration discipline + no automatic selling.

# Phase 1 — Monitoring Spec (original, design; no code)

> **REVISION (2026-11-03) after validation:** the trailing-gain rule was tested
> (`TRAILING_RULE_VALIDATION_2026-11-03.md`). It passes on DFNS but **fails generally** — it sold
> winners too early in 86% of 37 panel firings, including NVDA at +891% instead of +39,668%.
> **Change: keep the alerts; demote the automatic trailing sale to advisory-only (speculative
> positions only); promote position-size / concentration caps to the primary safeguard.**
> The counterfactual: capping DFNS at 10% of the account would have cut the −44% loss to ~−3%,
> without truncating any winner.

**Goal:** catch what you missed — a holding that surged while you were at work, then gave the
gain back — without predicting the market and without selling on every dip.

**Scope:** monitoring, alerting, and gain protection only. No stock selection. No orders.
**Validation case:** the real DFNS/NUKK December 2024 episode.

---

## 1. The problem, stated precisely

You held Nukkleus (NUKK, later T3 Defense/DFNS). It ran from about **$1.39 (Dec 16, 2024)** to
a closing high around **$52 (Dec 19, 2024)** on news, then collapsed over the following year to
about **$3.49** — a −99% round trip on the position. You were working and not watching.

The system's job: notice the extraordinary move, tell you, and apply a protection rule that
locks in *some* gain — without selling the whole thing on the first pullback.

## 2. Components (deterministic first)

| Component | Job | Type |
|---|---|---|
| **Ledger** | positions, cost basis, cash, fees, running P&L | deterministic |
| **Price feed** | daily (and intraday where available) prices + volume | data |
| **Monitor** | detect unusual moves and drift | deterministic |
| **Protection engine** | trailing-gain rule per position | deterministic |
| **Reporter** | explain what happened and why, with evidence | deterministic (Phase 1) |

No AI in Phase 1. If a rule can be written as arithmetic, it must be.

## 3. Signals to detect (each must cite its evidence)

1. **Unusual gain** — position up more than X% from cost (default: +25%) within Y days
   (default: 20).
2. **Extraordinary gain** — up more than Z% (default: +100%) — a "review now" event.
3. **Gain erosion** — a position that ran up sharply has given back more than W% (default: 30%)
   of its **peak unrealized gain** from that peak.
4. **Unusual volume** — volume above N× its 20-day average (default: 5×).
5. **Drift** — an allocation has drifted beyond its band (default: ±5 percentage points).

## 4. Trailing-gain protection rule (the DFNS rule)

For a position whose unrealized gain has exceeded the **arm threshold** (default +100%):

- Track the **peak price** reached since arming.
- **Alert** when price falls more than the **trail** (default 25%) from that peak.
- **Recommend** (never execute) reducing a defined fraction of the position (default: enough to
  recover the original cost basis, i.e. "take the principal off the table").

Design intent: **do not sell on a normal pullback.** A temporary dip is not a reversal. The rule
only fires after a large gain and a meaningful give-back.

## 5. The validation test — would it have caught DFNS?

Replay the real price series (Dec 2024 → 2026) and answer, with numbers:

| Question | Required answer |
|---|---|
| When did the monitor first flag an unusual gain? | date + price |
| When did it flag "extraordinary" (+100%)? | date + price |
| When would the protection rule have alerted? | date + price |
| How much of the peak gain would it have preserved? | $ and % |
| Would it have sold too early (before the peak)? | yes/no, with the date |
| What if the gain had kept running? | counterfactual |

**This test must pass before anything else is built.** If the rule sells too early or misses the
move, it is wrong, and we fix the rule — not the test.

## 6. Out of scope for Phase 1

- Stock selection / "what to buy" (Phase 3).
- Any AI interpretation (Phase 2).
- Broker connection, real money, live orders (not authorized).
- Tax optimization.

## 7. Open questions for the operator

1. **Trail width:** 25% is a starting point. Too tight sells on noise; too loose gives back more.
   We will pick it from the DFNS replay, not by guessing.
2. **Sell fraction:** recover cost basis, or sell a fixed fraction (e.g. one-third)?
3. **Alert channel:** how do you want to be told (email, file, console)?
4. **Universe for the ledger:** start with the holdings you actually had (DXCM, INUV, DFNS, and
   the others once we can read the statement cleanly).
