# Evidence Audit — Trailing-Rule Study (read-only)

**Status:** read-only. No code, no broker, no orders, no project change.
**Date:** 2026-11-03
**Purpose:** audit the trailing-rule study before treating its findings as proven, per the
operator's five points. Result: the *direction* holds, the *magnitude* was inflated by
selection, and one claimed benefit (the 10% cap) was overstated.

---

## 0. Summary of the audit

| Operator's point | Finding |
|---|---|
| 1. How were the 45 stocks selected (failed/delisted)? | **Valid criticism.** The panel was hindsight-selected. Rerun on a pre-specified universe: too-early rate **65%**, not 86%. |
| 2. Point-in-time information only? | **Pass.** The rule uses only trailing/current data at each date. No look-ahead. |
| 3. Dividends, splits, fees, execution? | **Pass, with a caveat.** Total-return adjusted prices (dividends + splits). Fees immaterial at these magnitudes. |
| 4. How was "sold too early" defined? | **Definition-sensitive:** 46%–74% depending on the threshold. The headline was definition-dependent. |
| 5. Was the 10% cap tested on a diversified portfolio? | **Now yes — and it is not a free lunch.** It is a tail-risk dial with a return cost. |

**Net effect on the decision: unchanged in direction, corrected in magnitude.** The automatic
trailing sale is still not justified as a general rule; the case for position-size caps is
**weaker and more honest** than the first pass implied.

---

## 1. Point 1 — selection bias (the operator was right)

The original 45-name panel was assembled from names I knew had big moves. That over-represents
spectacular winners and makes a protective rule look worse than it is.

**Fix:** reran the identical rule on a **pre-specified** universe — the **current S&P 500
constituents** (505 names, parsed from the index list) plus known delisted names — from **every
monthly entry point** (63,023 entries).

| Universe | fired | **sold too early** | median if sold | median if held |
|---|---|---|---|---|
| Original 45 (hindsight) | 82% | **86%** | +110% | +639% |
| **Systematic S&P 500 (63,023 entries)** | **47%** | **65%** | +85% | +214% |

**The rate fell from 86% to 65%** — so the original figure *was* inflated by selection, exactly
as you suspected. **But the conclusion holds:** the rule still fires on 47% of entries and gives
up the difference between +85% (sold) and +214% (held).

**Survivorship caveat (unresolved, and it matters):** the universe is *today's* index members.
Of 7 delisted/failed names I added, only 1 (SBNY) returned data — the rest are gone. **Missing
failures bias the test *in favour of* the rule** (failed names would make holding worse). So the
true too-early cost on survivors is real, and the rule's protective value is understated — but
the too-early cost among *surviving* names is already large enough to reject the general rule.

## 2. Point 2 — point-in-time information (pass)

The rule arms on the current gain vs cost and tracks the peak **since arming** — both use only
data available on the day. No future information enters a signal. The evaluation window
(hold-to-end) is used only to *score* the outcome, not to trigger it. **No look-ahead.**

## 3. Point 3 — dividends, splits, fees, execution (pass, with caveat)

- **Prices:** total-return adjusted closes, so **dividends and splits are included**.
- **Fees:** modelled 0.0%, 0.1%, and 0.2% per side. **No material change** (median rule +66% vs
  hold +92% at every fee level) — the outcome difference is far larger than transaction costs.
- **Execution caveat:** the study assumes the sale fills at the closing price on the signal day.
  Realistic execution (next open, slippage) would shave a little, but not change the direction.
- **Caveat:** the adjusted-price convention differs from the DFNS replay (which used
  split-adjustment only, appropriate for a non-dividend microcap). Both are defensible for their
  cases; the panel uses total return.

## 4. Point 4 — the definition of "sold too early" (definition-sensitive)

"Sold too early" = after firing, does the price later exceed the pre-fire peak by more than X%?

| Threshold X | "sold too early" |
|---|---|
| >0% | 74% |
| >5% | 70% |
| **>10% (used)** | **65%** |
| >20% | 60% |
| >50% | 46% |

**The rate is definition-dependent (46%–74%).** The honest statement is therefore not "86% of
the time" but: **"in the majority of firings, the price later rose above the pre-fire peak; the
share depends on how large a re-rise counts, ranging from 46% to 74%."** The economic conclusion
(median sold +85% vs held +214%) does not depend on the definition.

## 5. Point 5 — the 10% cap on a diversified portfolio (tested; not a free lunch)

The earlier claim (DFNS at 51% → cap at 10% → loss −44% becomes −3%) was **single-position
arithmetic**. I tested it properly: **3,000 random 5-name portfolios** (like your real account),
1-year holds, monthly rebalanced, random start dates, uncapped (20%/name) vs 10% capped.

| | median return | median maxDD | worst maxDD | return/DD |
|---|---|---|---|---|
| uncapped (20%) | **+16.7%** | −15.3% | −59.8% | 1.08 |
| **10% capped** | **+8.5%** | **−7.9%** | **−35.4%** | 1.07 |

**The cap is a tail-risk dial, not a free lunch.** It roughly halves the drawdown *and* roughly
halves the return; risk-adjusted return is **unchanged** (1.08 vs 1.07). It helps most in the
tail (worst case −60% → −35%) and costs in the median.

**Corrected statement:** the 10% cap reduces the severity of bad outcomes but does **not**
improve risk-adjusted return in general. Its justification is **drawdown tolerance and
concentration-limit discipline**, not a proven performance edge. **It should be an initial
policy setting to evaluate, not a scientifically established ideal.**

---

## 6. What the audit changes

| Claim from the first study | Status after audit |
|---|---|
| "Rule sells winners too early 86% of the time" | **Corrected:** 65% on a pre-specified universe; 46–74% by definition |
| "Rule hurts on average" | **Holds:** median +66% (rule) vs +92% (hold); mean +88% vs +216% |
| "Capping DFNS at 10% turns −44% into −3%" | **Corrected:** true for that single position; across portfolios the cap is risk-neutral (ret/DD unchanged) |
| "Keep alerts, drop the automatic sale" | **Holds** — the alert is awareness; the automatic sale costs more than it saves |
| "Position sizing is the primary safeguard" | **Refined:** it is a *drawdown-control* safeguard, not a return-enhancer |

**Nothing here reverses the recommendation.** It corrects two magnitudes and removes one
overstated benefit. The design that survives is: **awareness first, concentration discipline
second, no automatic selling.**

## 7. Limitations carried forward

- Universe is today's index members → survivorship bias favouring the rule (unquantified).
- Adjusted-price convention differs between the DFNS case and the panel.
- Execution modelled at the close, not next-open.
- "Too early" remains a judgement with a range, not a point estimate.
