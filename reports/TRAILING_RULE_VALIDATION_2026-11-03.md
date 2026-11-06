# Trailing-Gain-Protection Rule — Validation Study (read-only)

**Status:** read-only research. No code, no broker, no orders, no project change.
**Date:** 2026-11-03
**Purpose:** decide whether a trailing-gain-protection rule is a *generally reliable* safeguard,
before building anything. Two parts, as requested: the DFNS replay, and a broad panel of real
big movers that tests whether the rule sells winners too early.

---

## 0. Bottom line

**The DFNS replay passes. The broader validation fails.** A 25% trailing rule would have
protected the DFNS gain — but across a panel of 45 real big movers it **sold winners too early
86% of the time**, and it **cannot protect losers that never doubled.** The DFNS case is
misleadingly easy: it was a total pump-and-dump. Tuning to it would have produced a false
positive — exactly the risk you flagged.

**Recommendation: keep the monitor's *alerts*, drop the automatic *trailing sale* as a general
rule, and make position sizing the primary safeguard.**

---

## 1. Pre-registered design (fixed before looking)

- **Rule:** arm when unrealized gain ≥ **+100%** from cost; track the peak close since arming;
  recommend selling when close ≤ peak × (1 − **25%**).
- **Part 1:** DFNS/NUKK, Dec 2024, split-adjusted, with the operator's actual cost basis.
- **Part 2:** panel of 45 real big movers — **both** those that later collapsed **and** those
  that kept climbing. Key metric: **"sold too early"** = after the rule fires, does the price
  later exceed the pre-fire peak by >10%?
- Costs 0.1%/side. Split-adjusted daily closes (Yahoo Finance).

---

## 2. Part 1 — DFNS replay: the rule works here

Using split-adjusted closes (raw ÷ 125 = actual price):

| | Date | Price (adj) | Actual |
|---|---|---|---|
| Cost basis (his statement) | — | 572.03 | ~$4.58 |
| Surge begins | 2024-12-17 | 1,485 | $11.88 |
| Peak | 2024-12-19 | 6,512 | **$52.10** |
| **Rule fires** | **2024-12-24** | **4,564** | **$36.51** |
| Final | 2026-11 | 2.29 | $0.02 |

- **ARM** 2024-12-17 (+160% from his cost basis)
- **PEAK** 2024-12-19 (+1,038%)
- **FIRE** 2024-12-24, −30% off peak
- **Preserved +698%** vs peak +1,038% vs **hold-to-now −100%**
- **sold_too_early = False**

**Verdict on DFNS alone: PASS.** The rule would have locked in a large gain and avoided a
total loss. This is exactly the outcome you wanted.

---

## 3. Part 2 — broad panel: the rule fails, badly

45 real big movers, same rule (arm +100%, trail 25%), entry = first available close:

| Group | n | fired | **sold too early** | median preserved | median hold |
|---|---|---|---|---|---|
| **ALL** | 45 | 37 (82%) | **32/37 (86%)** | +110% | **+639%** |
| later collapsed (B&H<+50%) | 20 | 12 (60%) | 8/12 (67%) | +90% | −63% |
| kept climbing (B&H≥+50%) | 25 | 25 (100%) | **24/25 (96%)** | +136% | +991% |

**The rule truncates the winners — which is where long-term wealth comes from.**

Where it hurt most (sold vs held):
- **NVDA:** sold +891% vs held **+39,668%**
- **SHOP:** sold +72% vs held +8,902%
- **CVNA:** sold +57% vs held +3,394%
- **AVGO:** sold +110% vs held +3,152%
- **TSLA:** sold +210% vs held +2,535%
- **AMZN:** sold +63% vs held +1,625%

Where it helped (sold vs held): PTON (+288% vs −79%), LCID (+256% vs −97%), SPCE (+118% vs
−99%), BYND (+92% vs −100%).

**Tally: helped on 15, hurt on 22.** Median outcome if the rule is followed: **+110%**; if held:
**+639%**.

### Sensitivity — it is not a tuning problem

| arm | trail | fired | sold too early |
|---|---|---|---|
| +25% | 15–50% | 37–43 | 59–91% |
| +50% | 15–50% | 34–40 | 53–82% |
| +100% | 15–50% | 31–37 | 55–89% |
| +200% | 15–50% | 27–33 | 48–82% |

**Across every arm/trail combination, 48–91% of firings are "too early."** There is no setting
that avoids it. The flaw is structural, not a parameter choice.

### The losers it cannot protect

13 panel positions were real losers (B&H < −30%). The rule did nothing for **5** of them
because they never doubled: MARA (−95%, peak only +1%), AI (−87%, +92%), RIVN (−86%, +71%),
BBBY (−66%, +11%), COIN (−54%, +28%). **A rule that arms at +100% cannot protect a position
that never gains 100%.**

### Entry-dependence

Rolling monthly entries: collapsers fired 34% (36% too early); winners fired 69% (84% too
early). The pattern holds regardless of when you started.

---

## 4. The counterfactual that matters more: position sizing

DFNS was **51% of the account** ($489.66 of $960.47 total cost). That concentration, not the
missing trailing rule, is what caused the −44% portfolio loss.

| DFNS cap | DFNS loss | Portfolio outcome |
|---|---|---|
| 51% (actual) | −$485 | −44% |
| 25% | −$238 | ~−26% better |
| **10%** | **−$95** | **~−41% better** |

**Position sizing would have prevented most of the damage — without truncating any winner.**
The trailing rule helps only *after* a big gain; sizing helps *before*.

---

## 5. Conclusion and recommendation

| Element | Verdict |
|---|---|
| **Alerts** on unusual gains | **KEEP** — awareness is the real value; an alert does not force a sale |
| **Automatic trailing sale** (general rule) | **DROP** — sells winners too early 86% of the time; cost outweighs benefit |
| **Position sizing / concentration caps** | **PROMOTE to primary safeguard** — prevents the DFNS disaster without cutting winners |
| **Never-armed losers** | Covered only by sizing and thesis review, **not** by any gain rule |

**The honest summary:** the trailing rule solves the *visible* problem (DFNS) but creates a
larger, *invisible* one (selling NVDA at +891% instead of +39,668%). That is precisely the
selection-bias trap you warned about — and the two-part test caught it.

**Revised Phase 1 scope:**
1. Deterministic ledger + monitor (keep).
2. **Alerts** on unusual gain/volume/drift, each with evidence (keep).
3. **Position-size limits** enforced and alerted (new primary safeguard).
4. Trailing-gain logic becomes **advisory only**, and only for positions explicitly tagged
   *speculative/pump-like* — never applied to quality compounders.

---

## 6. Limitations

- Panel is survivorship-influenced (today's listed names); delisted microcaps are under-
  represented. This likely *understates* the rule's value — but the too-early rate among
  survivors is already fatal.
- Entry = first available close for the panel; the rolling-entry test (Part 3c) confirms the
  pattern is not entry-specific.
- Yahoo split-adjusted closes; dead-token/shell prices near delisting are imperfect.
- The rule was tested on *gain protection only*; it says nothing about entry or selection.
