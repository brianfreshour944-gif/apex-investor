"""Monitoring signals: awareness only. These NEVER place orders or auto-sell.

Each signal returns `Alert` objects carrying their own evidence (the numbers that triggered
them), so an alert can always be explained. Signals are pure functions over price series and
portfolio state — no network, no side effects.

Signal set (Phase 1):
  unusual_gain        position up >= 25% from cost within a short window
  extraordinary_gain  position up >= 100% from cost  (a "review now" event)
  unusual_volume      volume >= 5x its 20-day average
  gain_erosion        gave back >= 30% of peak gain after a big run-up  (ADVISORY)
  drift               an allocation drifted >= 5pp beyond its target
  drawdown            portfolio >= X% off its peak equity

The gain_erosion signal is advisory by design: the evidence audit showed automatic trailing
sales cost more than they save, so it explains and suggests review, and sells nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --- defaults (operator-configurable) --------------------------------------
UNUSUAL_GAIN = 0.25
EXTRAORDINARY_GAIN = 1.00
VOLUME_WINDOW = 20
VOLUME_MULT = 5.0
EROSION_FRACTION = 0.30      # give back >= 30% of the peak gain
DRIFT_BAND = 0.05            # 5 percentage points
DRAWDOWN_WARN = -0.15
DRAWDOWN_CRITICAL = -0.25


@dataclass(frozen=True)
class Alert:
    date: str
    ticker: str
    kind: str
    severity: str            # info | warn | critical
    message: str
    evidence: dict = field(default_factory=dict)

    def render(self) -> str:
        ev = ", ".join(f"{k}={v}" for k, v in self.evidence.items())
        return f"[{self.severity.upper()}] {self.date} {self.ticker} {self.kind}: {self.message} ({ev})"


def gain_alerts(bars, ticker: str, cost_basis_ps: float, *,
                unusual: float = UNUSUAL_GAIN, extraordinary: float = EXTRAORDINARY_GAIN,
                window: int | None = None) -> list[Alert]:
    """Emit an alert the first time each gain threshold is crossed.

    `cost_basis_ps` must be on the SAME basis as `bars` (as-printed, split-adjusted close).
    """
    if cost_basis_ps <= 0:
        raise ValueError("cost_basis_ps must be > 0")
    alerts: list[Alert] = []
    seen_unusual = seen_extra = False
    for b in bars:
        gain = b.close / cost_basis_ps - 1.0
        if not seen_extra and gain >= extraordinary:
            alerts.append(Alert(b.date, ticker, "extraordinary_gain", "critical",
                                f"position up {gain:+.0%} from cost — review now",
                                {"gain": f"{gain:+.1%}", "price": round(b.close, 2),
                                 "cost": round(cost_basis_ps, 2)}))
            seen_extra = seen_unusual = True
        elif not seen_unusual and gain >= unusual:
            alerts.append(Alert(b.date, ticker, "unusual_gain", "warn",
                                f"position up {gain:+.0%} from cost",
                                {"gain": f"{gain:+.1%}", "price": round(b.close, 2),
                                 "cost": round(cost_basis_ps, 2)}))
            seen_unusual = True
    return alerts


def volume_alerts(bars, ticker: str, *, window: int = VOLUME_WINDOW,
                  mult: float = VOLUME_MULT) -> list[Alert]:
    """Emit when volume >= mult x its trailing average."""
    alerts: list[Alert] = []
    vols = [b.volume for b in bars]
    for i in range(window, len(bars)):
        avg = sum(vols[i - window:i]) / window
        if avg > 0 and vols[i] >= mult * avg:
            alerts.append(Alert(bars[i].date, ticker, "unusual_volume", "warn",
                                f"volume {vols[i] / avg:.1f}x the {window}-day average",
                                {"volume": int(vols[i]), "avg": int(avg),
                                 "multiple": round(vols[i] / avg, 1)}))
    return alerts


def erosion_alerts(bars, ticker: str, cost_basis_ps: float, *,
                   arm: float = EXTRAORDINARY_GAIN, give_back: float = EROSION_FRACTION,
                   ) -> list[Alert]:
    """ADVISORY: after a big run-up, alert when price gives back >= `give_back` of peak gain.

    Sells nothing. Provided so the operator is *aware*; the evidence says acting on it
    automatically costs more than it saves.
    """
    alerts: list[Alert] = []
    peak = cost_basis_ps
    armed = False
    for b in bars:
        p = b.close
        if p > peak:
            peak = p
        if not armed and p / cost_basis_ps - 1 >= arm:
            armed = True
        if armed and peak > cost_basis_ps:
            peak_gain = peak - cost_basis_ps
            given_back = (peak - p) / peak_gain if peak_gain > 0 else 0.0
            if given_back >= give_back:
                alerts.append(Alert(b.date, ticker, "gain_erosion", "warn",
                                    f"gave back {given_back:.0%} of peak gain "
                                    f"({peak / cost_basis_ps - 1:+.0%} -> {p / cost_basis_ps - 1:+.0%})",
                                    {"peak_gain": f"{peak / cost_basis_ps - 1:+.1%}",
                                     "now": f"{p / cost_basis_ps - 1:+.1%}",
                                     "given_back": f"{given_back:.0%}"}))
                break
    return alerts


def drift_alerts(weights: dict[str, float], targets: dict[str, float], *,
                 band: float = DRIFT_BAND, date: str = "") -> list[Alert]:
    """Emit when |current - target| >= band (fraction of portfolio)."""
    alerts: list[Alert] = []
    for tk, target in targets.items():
        w = weights.get(tk, 0.0)
        if abs(w - target) >= band:
            alerts.append(Alert(date, tk, "drift", "info",
                                f"allocation {w:.1%} vs target {target:.1%} (band {band:.0%})",
                                {"current": f"{w:.1%}", "target": f"{target:.1%}",
                                 "delta": f"{w - target:+.1%}"}))
    return alerts


def drawdown_alerts(equity_series: list[tuple[str, float]], *, ticker: str = "PORTFOLIO",
                    warn: float = DRAWDOWN_WARN, critical: float = DRAWDOWN_CRITICAL) -> list[Alert]:
    """Emit the first time equity drawdown breaches warn, then critical."""
    alerts: list[Alert] = []
    peak = float("-inf")
    seen_warn = seen_crit = False
    for date, eq in equity_series:
        peak = max(peak, eq)
        dd = eq / peak - 1.0 if peak > 0 else 0.0
        if not seen_crit and dd <= critical:
            alerts.append(Alert(date, ticker, "drawdown", "critical",
                                f"portfolio {dd:.1%} off peak",
                                {"drawdown": f"{dd:.1%}", "peak": round(peak, 2), "equity": round(eq, 2)}))
            seen_crit = seen_warn = True
        elif not seen_warn and dd <= warn:
            alerts.append(Alert(date, ticker, "drawdown", "warn",
                                f"portfolio {dd:.1%} off peak",
                                {"drawdown": f"{dd:.1%}", "peak": round(peak, 2), "equity": round(eq, 2)}))
            seen_warn = True
    return alerts


def dedupe(alerts: list[Alert]) -> list[Alert]:
    """Drop exact duplicate alerts, keeping first occurrence, ordered by (date, ticker, kind)."""
    seen: set[tuple[str, str, str]] = set()
    out: list[Alert] = []
    for a in sorted(alerts, key=lambda x: (x.date, x.ticker, x.kind)):
        key = (a.date, a.ticker, a.kind)
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


def position_alerts(bars, ticker: str, cost_basis_ps: float, *,
                    unusual: float = UNUSUAL_GAIN, extraordinary: float = EXTRAORDINARY_GAIN,
                    volume_window: int = VOLUME_WINDOW, volume_mult: float = VOLUME_MULT,
                    erosion: float = EROSION_FRACTION) -> list[Alert]:
    """All price/volume signals for one position, deduplicated."""
    return dedupe(
        gain_alerts(bars, ticker, cost_basis_ps, unusual=unusual, extraordinary=extraordinary)
        + volume_alerts(bars, ticker, window=volume_window, mult=volume_mult)
        + erosion_alerts(bars, ticker, cost_basis_ps, arm=extraordinary, give_back=erosion))


def portfolio_signals(holdings: list[dict], **kwargs) -> list[Alert]:
    """Run every signal across all holdings.

    Each holding is a dict: {"ticker": str, "bars": list[Bar], "cost_basis_ps": float}.
    `cost_basis_ps` must be on the same basis as the bars: the as-printed, split-adjusted close.
    Alerts appear once, each carrying its own evidence.
    """
    out: list[Alert] = []
    for h in holdings:
        out += position_alerts(h["bars"], h["ticker"], h["cost_basis_ps"], **kwargs)
    return dedupe(out)
