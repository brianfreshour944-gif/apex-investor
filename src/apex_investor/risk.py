"""Portfolio risk controls (M4): concentration, speculative allocation, drawdown.

Guiding rules:
  - Limits are measured against TOTAL PORTFOLIO EQUITY (cash + holdings), never against
    invested value alone. A cash-heavy portfolio must not read as concentrated.
  - FAIL CLOSED. Missing prices, unknown/absent tags, invalid values, or an unreconciled ledger
    must NOT produce a clean assessment. Any such condition sets `trustworthy=False` and raises
    a blocking alert, so an untrustworthy check is never mistaken for a passing one.
  - ALERTS ONLY. This module never trades, never sizes an order, and never mutates the ledger.

Thresholds are strict, with two deliberate conventions:
  - LIMITS (concentration, speculative): a value *at* the limit is compliant; only *above* it
    raises an alert. (A float epsilon protects the "exactly at" case from rounding error.)
  - WARNING THRESHOLDS (drawdown): *reaching* the threshold raises the alert, so -15% warns and
    -25% escalates.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .ledger import Ledger, Tag
from .monitor import Alert

# Tolerance so a value that is exactly at a limit is not flagged by float rounding error.
_EPS = 1e-9

# Defaults (operator-configurable; see PHASE1_BUILD_PLAN §10).
MAX_POSITION_WEIGHT = 0.10      # 10% per single stock
MAX_SPECULATIVE_WEIGHT = 0.15   # 15% combined speculative sleeve
DRAWDOWN_WARN = -0.15
DRAWDOWN_CRITICAL = -0.25


@dataclass(frozen=True)
class RiskLimits:
    max_position_weight: float = MAX_POSITION_WEIGHT
    max_speculative_weight: float = MAX_SPECULATIVE_WEIGHT
    drawdown_warn: float = DRAWDOWN_WARN
    drawdown_critical: float = DRAWDOWN_CRITICAL


@dataclass
class RiskAssessment:
    date: str
    trustworthy: bool                 # False => do NOT treat as a clean assessment
    equity: float | None
    weights: dict[str, float]         # ticker -> weight of TOTAL equity
    cash_weight: float | None
    speculative_weight: float | None
    drawdown: float | None            # vs provided peak, if any
    alerts: list[Alert] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """A clean assessment requires trust AND no alerts. Untrustworthy is never clean."""
        return self.trustworthy and not self.alerts

    def render(self) -> str:
        lines = [f"Risk assessment {self.date}: trustworthy={self.trustworthy} "
                 f"equity={self.equity} clean={self.clean}"]
        if self.blocked:
            lines.append("  BLOCKED: " + "; ".join(self.blocked))
        for a in self.alerts:
            lines.append("  " + a.render())
        return "\n".join(lines)


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _resolve_tag(ledger: Ledger, ticker: str, tags: dict | None) -> Tag | None:
    """Operator-assigned tag lookup. Returns None when the position is untagged (fail closed)."""
    src = dict(tags or {})
    if ticker in src:
        return src[ticker]
    if ticker in ledger.tags:
        return ledger.tags[ticker]
    return None


def assess(ledger: Ledger, prices: dict[str, float], *, limits: RiskLimits | None = None,
           date: str = "", peak_equity: float | None = None,
           tags: dict | None = None) -> RiskAssessment:
    """Assess concentration, speculative allocation and drawdown. Fail closed."""
    limits = limits or RiskLimits()
    blocked: list[str] = []

    # --- validate the limits themselves BEFORE any comparison (invalid config must not crash
    #     or pass silently; a non-numeric value must not reach the `<=` below) ---
    for name, val in (("max_position_weight", limits.max_position_weight),
                      ("max_speculative_weight", limits.max_speculative_weight),
                      ("drawdown_warn", limits.drawdown_warn),
                      ("drawdown_critical", limits.drawdown_critical)):
        if not _finite(val):
            blocked.append(f"invalid limit {name}={val!r} (must be a finite number)")
    for name, val, lo, hi in (
            ("max_position_weight", limits.max_position_weight, 0.0, 1.0),
            ("max_speculative_weight", limits.max_speculative_weight, 0.0, 1.0),
            ("drawdown_warn", limits.drawdown_warn, -1.0, 0.0),
            ("drawdown_critical", limits.drawdown_critical, -1.0, 0.0)):
        if _finite(val) and not (lo <= val <= hi):
            blocked.append(f"limit {name}={val!r} out of range [{lo}, {hi}]")
    # Only compare the two drawdown thresholds when BOTH are valid numbers.
    if _finite(limits.drawdown_warn) and _finite(limits.drawdown_critical):
        if limits.drawdown_warn <= limits.drawdown_critical:
            blocked.append("drawdown_warn must be above (less negative than) drawdown_critical")

    positions = ledger.positions

    # --- validate prices for every HELD ticker (missing price must not read as 0) ---
    for tk in positions:
        if tk not in prices:
            blocked.append(f"missing price for {tk}")
        elif not _finite(prices[tk]) or prices[tk] < 0:
            blocked.append(f"invalid price for {tk}: {prices[tk]!r}")

    # --- validate tags (operator-assigned; absent/unknown is a blocker) ---
    resolved: dict[str, Tag] = {}
    for tk in positions:
        tag = _resolve_tag(ledger, tk, tags)
        if tag is None:
            blocked.append(f"untagged position {tk}")
        elif not isinstance(tag, Tag):
            blocked.append(f"unknown tag for {tk}: {tag!r}")
        else:
            resolved[tk] = tag

    # --- reconcile the ledger (an unreconciled ledger is a blocker) ---
    rec = ledger.reconcile(prices)
    if not rec.get("ok", False):
        blocked.append("ledger does not reconcile")

    # --- compute equity on VALID prices only; anything missing blocks a clean verdict ---
    usable = {tk: p for tk, p in prices.items() if _finite(p) and p >= 0}
    equity = ledger.cash + sum(pos.market_value(usable.get(tk, 0.0))
                               for tk, pos in positions.items())
    if not _finite(equity) or equity <= 0:
        blocked.append(f"non-positive or invalid equity: {equity!r}")

    if blocked:
        return RiskAssessment(date, False, None, {}, None, None, None,
                              [Alert(date, "PORTFOLIO", "risk_check_incomplete", "critical",
                                     "risk check could not be trusted; no clean verdict issued",
                                     {"blocked": len(blocked), "reasons": "; ".join(blocked)})],
                              blocked)

    # --- trustworthy from here: compute weights against TOTAL equity ---
    weights = {tk: pos.market_value(usable[tk]) / equity for tk, pos in positions.items()}
    cash_weight = ledger.cash / equity
    speculative_weight = sum(w for tk, w in weights.items() if resolved[tk] is Tag.SPECULATIVE)

    alerts: list[Alert] = []
    for tk, w in sorted(weights.items(), key=lambda kv: -kv[1]):
        if w > limits.max_position_weight + _EPS:
            alerts.append(Alert(date, tk, "concentration", "warn",
                                f"{tk} is {w:.1%} of portfolio (limit {limits.max_position_weight:.0%})",
                                {"weight": f"{w:.1%}", "limit": f"{limits.max_position_weight:.0%}",
                                 "value": round(positions[tk].market_value(usable[tk]), 2),
                                 "equity": round(equity, 2)}))
    if speculative_weight > limits.max_speculative_weight + _EPS:
        alerts.append(Alert(date, "PORTFOLIO", "speculative_allocation", "warn",
                            f"speculative sleeve is {speculative_weight:.1%} "
                            f"(limit {limits.max_speculative_weight:.0%})",
                            {"weight": f"{speculative_weight:.1%}",
                             "limit": f"{limits.max_speculative_weight:.0%}"}))

    drawdown = None
    if peak_equity is not None:
        if not _finite(peak_equity) or peak_equity <= 0:
            return RiskAssessment(date, False, None, {}, None, None, None,
                                  [Alert(date, "PORTFOLIO", "risk_check_incomplete", "critical",
                                         "invalid peak_equity; no clean verdict issued",
                                         {"peak_equity": repr(peak_equity)})],
                                  ["invalid peak_equity"])
        drawdown = equity / peak_equity - 1.0
        if drawdown <= limits.drawdown_critical:
            alerts.append(Alert(date, "PORTFOLIO", "drawdown", "critical",
                                f"portfolio {drawdown:.1%} off peak",
                                {"drawdown": f"{drawdown:.1%}", "peak": round(peak_equity, 2),
                                 "equity": round(equity, 2)}))
        elif drawdown <= limits.drawdown_warn:
            alerts.append(Alert(date, "PORTFOLIO", "drawdown", "warn",
                                f"portfolio {drawdown:.1%} off peak",
                                {"drawdown": f"{drawdown:.1%}", "peak": round(peak_equity, 2),
                                 "equity": round(equity, 2)}))

    return RiskAssessment(date, True, equity, weights, cash_weight,
                          speculative_weight, drawdown, alerts, blocked)
