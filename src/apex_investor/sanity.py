"""Price-sanity guard: FLAG suspicious quotes for verification, do not blindly reject.

A large one-day move is not automatically an error. A legitimate earnings gap, a reverse split,
or another corporate action can all produce one. This guard therefore:

  - compares the latest quote with the previous valid close;
  - classifies the move with CONFIGURABLE thresholds (warn / block);
  - checks for a split or known corporate action that would explain the move;
  - marks a quote BLOCKED only when the move is extreme AND unexplained, so an unverifiable
    number never silently flows into valuation or risk;
  - keeps the quote and its evidence so the operator can inspect it.

Outcomes:
  OK          normal move, or a move explained by a known corporate action
  SUSPECT     unusually large but plausible (e.g. an earnings gap), or consistent with a split;
              surfaced for verification, valuation still proceeds but is marked UNVERIFIED
  UNVERIFIABLE  no quote, no previous close to compare against, or an unusable previous close;
              noted, not blocked (the staleness gate still applies separately)
  BLOCKED     an unusable latest price, invalid limits, or an extreme move with no explanation;
              valuation and risk are blocked for that quote

Split detection is a HEURISTIC, not proof. A factor near a common split ratio only means the move
COULD be a split; it is flagged for verification, never auto-accepted. An authoritative corporate
action (exchange or provider data) should be supplied via `known_actions` and takes precedence.

This module makes no network calls and never trades.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

# Common split ratios: a move whose factor is within tolerance of one of these is treated as
# "consistent with a split" and flagged for verification rather than blocked.
_COMMON_RATIOS = (2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 50, 100)


@dataclass(frozen=True)
class PriceSanityLimits:
    warn_move: float = 0.35        # |change| >= this -> SUSPECT (verify)
    block_move: float = 0.90       # |change| >= this AND unexplained -> BLOCKED
    split_tolerance: float = 0.06  # how close a factor must be to a common ratio


def _finite_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _valid_price(x) -> bool:
    """A usable price: a finite number strictly greater than zero.

    Rejects None, bool, str, NaN, +/-inf and non-positive values, so a malformed price can never
    reach the factor calculation or flow into valuation.
    """
    return _finite_number(x) and x > 0


def _valid_limits(lim: PriceSanityLimits) -> list[str]:
    """Return a list of reasons `lim` is unusable. Empty means valid.

    Invalid config must fail closed: a non-numeric or out-of-range threshold must never reach a
    comparison (`mag < lim.warn_move`) where it could raise or silently mis-classify.
    """
    problems: list[str] = []
    # warn/block must be positive (a zero threshold is a config error); split_tolerance may be 0
    # (exact-ratio matching). All must be <= 1.
    for name, val, lo, hi in (("warn_move", lim.warn_move, 0.0, 1.0),
                              ("block_move", lim.block_move, 0.0, 1.0),
                              ("split_tolerance", lim.split_tolerance, 0.0, 1.0)):
        if not _finite_number(val):
            problems.append(f"invalid sanity limit {name}={val!r} (must be a finite number)")
        elif not (lo <= val <= hi):
            problems.append(f"sanity limit {name}={val!r} out of range [{lo}, {hi}]")
        elif name != "split_tolerance" and val == 0.0:
            problems.append(f"sanity limit {name} must be greater than 0")
    if (_finite_number(lim.warn_move) and _finite_number(lim.block_move)
            and lim.warn_move >= lim.block_move):
        problems.append("warn_move must be below block_move")
    return problems


@dataclass(frozen=True)
class QuoteCheck:
    ticker: str
    latest_date: str
    latest_close: float
    previous_date: str | None
    previous_close: float | None
    change: float | None           # fraction, e.g. -0.875 for -87.5%
    status: str                    # OK | SUSPECT | UNVERIFIABLE | BLOCKED
    reason: str
    corporate_action: str | None = None
    evidence: dict = field(default_factory=dict)

    @property
    def blocks(self) -> bool:
        return self.status == "BLOCKED"

    @property
    def flagged(self) -> bool:
        return self.status in ("SUSPECT", "BLOCKED")


def _describe_ratio(factor: float, tol: float) -> str | None:
    """Describe a split that would explain `factor` (= latest/previous close).

    factor > 1 means the price multiplied: a reverse split (N shares become 1).
    factor < 1 means the price divided: a forward split (1 share becomes N).
    """
    for k in _COMMON_RATIOS:
        if factor >= 1 and abs(factor - k) <= tol * k:
            return f"1:{k} reverse split (factor {factor:.3f})"
        inv = 1.0 / k
        if factor < 1 and abs(factor - inv) <= tol * inv:
            return f"{k}:1 forward split (factor {factor:.3f})"
    return None


def _ratio_matches(factor: float, ratio, tol: float) -> bool:
    """True only if a declared corporate-action ratio actually explains `factor`.

    `ratio` is the price factor the action implies (new/old). It must be a finite positive
    number close to the observed factor; anything else (missing, non-numeric, zero, negative,
    NaN) cannot explain the move.
    """
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        return False
    if not math.isfinite(ratio) or ratio <= 0:
        return False
    return abs(factor - ratio) <= tol * ratio


def _find_action(ticker: str, latest_date: str, known_actions, window_days: int):
    """Return the closest matching action dict for ticker+date, or None.

    Requires an EXACT ticker match: an action with no ticker (or a different ticker) must never
    authorize a move for this holding. Malformed entries (non-dict, bad/absent date) are skipped.
    """
    best = None
    for act in known_actions or []:
        if not isinstance(act, dict):
            continue
        if act.get("ticker") != ticker:      # exact match only; None or another ticker does not apply
            continue
        ad = act.get("date")
        if not isinstance(ad, str) or not ad:
            continue
        try:
            gap = abs((date.fromisoformat(latest_date) - date.fromisoformat(ad)).days)
        except ValueError:
            continue
        if gap <= window_days and (best is None or gap < best[0]):
            best = (gap, act)
    return best[1] if best else None


def _action_label(act: dict) -> str:
    ratio = act.get("ratio")
    return act.get("label") or (f"ratio {ratio}" if ratio else "corporate action")


def check_quote(ticker: str, latest, previous, *,
                limits: PriceSanityLimits | None = None,
                known_actions: list[dict] | None = None,
                action_window_days: int = 5) -> QuoteCheck:
    """Classify one quote against its previous close. `latest`/`previous` are Bar-like objects
    with `.date` and `.close` (or None)."""
    lim = limits or PriceSanityLimits()
    problems = _valid_limits(lim)
    if problems:
        return QuoteCheck(ticker, "n/a", float("nan"), None, None, None, "BLOCKED",
                          "invalid price-sanity limits: " + "; ".join(problems),
                          evidence={"limits": _lim_dict(lim), "invalid_limits": problems})
    if latest is None:
        return QuoteCheck(ticker, "n/a", float("nan"), None, None, None, "UNVERIFIABLE",
                          "no quote available", evidence={"limits": _lim_dict(lim)})

    # Validate the raw prices BEFORE any arithmetic. A malformed latest price must be BLOCKED so
    # it can never be valued; a malformed previous close makes the move UNVERIFIABLE (not BLOCKED,
    # so a bad earlier bar cannot discard an otherwise-valid latest quote).
    raw_latest = latest.close
    if not _valid_price(raw_latest):
        return QuoteCheck(ticker, str(latest.date), float("nan"), None, None, None, "BLOCKED",
                          f"invalid latest price ({raw_latest!r}): must be a finite positive "
                          "number", evidence={"limits": _lim_dict(lim), "latest": repr(raw_latest)})
    if previous is not None and not _valid_price(previous.close):
        return QuoteCheck(ticker, str(latest.date), float(raw_latest), str(previous.date),
                          float("nan"), None, "UNVERIFIABLE",
                          f"invalid previous close ({previous.close!r}): cannot compare",
                          evidence={"limits": _lim_dict(lim), "latest": float(raw_latest),
                                    "previous": repr(previous.close)})

    ld, lc = latest.date, float(latest.close)
    if previous is None:
        return QuoteCheck(ticker, ld, lc, None, None, None, "UNVERIFIABLE",
                          "no previous close to compare against",
                          evidence={"latest": lc, "limits": _lim_dict(lim)})

    pd_, pc = previous.date, float(previous.close)
    base = {"latest": lc, "previous": pc, "previous_date": pd_, "limits": _lim_dict(lim)}

    change = lc / pc - 1.0
    factor = lc / pc
    base["change"] = f"{change:+.1%}"

    # A declared action explains the move ONLY if its stated ratio agrees with the observed
    # factor. A ratio that does not match is not an explanation — fall through to normal rules.
    action = _find_action(ticker, ld, known_actions, action_window_days)
    mismatch_note = ""
    if action is not None:
        ratio = action.get("ratio")
        label = _action_label(action)
        if _ratio_matches(factor, ratio, lim.split_tolerance):
            base["corporate_action"] = label
            return QuoteCheck(ticker, ld, lc, pd_, pc, change, "OK",
                              f"large move explained by {label} on {action.get('date')}",
                              corporate_action=label, evidence=base)
        mismatch_note = (f"; declared {label} (ratio {ratio!r}) does not match the "
                         f"observed factor {factor:.3f}")

    mag = abs(change)
    if mag < lim.warn_move:
        return QuoteCheck(ticker, ld, lc, pd_, pc, change, "OK",
                          f"move {change:+.1%} within normal range", evidence=base)

    split = _describe_ratio(factor, lim.split_tolerance)
    if mag >= lim.block_move:
        if split:
            return QuoteCheck(ticker, ld, lc, pd_, pc, change, "SUSPECT",
                              f"extreme move {change:+.1%} but consistent with a {split}; "
                              f"verify before trusting{mismatch_note}",
                              corporate_action=split, evidence=base)
        return QuoteCheck(ticker, ld, lc, pd_, pc, change, "BLOCKED",
                          f"extreme unexplained move {change:+.1%} vs previous close "
                          f"{pc} on {pd_}; cannot validate{mismatch_note}", evidence=base)

    note = f" and consistent with a {split}" if split else ""
    return QuoteCheck(ticker, ld, lc, pd_, pc, change, "SUSPECT",
                      f"unusually large move {change:+.1%}{note}; verify "
                      f"(earnings gap or corporate action?){mismatch_note}",
                      corporate_action=split, evidence=base)


def _lim_dict(lim: PriceSanityLimits) -> dict:
    return {"warn_move": lim.warn_move, "block_move": lim.block_move,
            "split_tolerance": lim.split_tolerance}


def worst_status(checks: list[QuoteCheck]) -> str:
    order = ["OK", "UNVERIFIABLE", "SUSPECT", "BLOCKED"]
    return max((c.status for c in checks), key=order.index, default="OK")
