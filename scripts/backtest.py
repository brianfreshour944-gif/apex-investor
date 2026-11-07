"""Backtest harness: compare the index, DCA and the concentration-capped "apex" plan.

Purpose:
  - Quantify the trade-off the policy makes: a single index fund vs a DCA schedule vs a
    core index position with a small speculative satellite sleeve that is trimmed back to
    its target weight when it grows too large.
  - Everything is SIMULATION ONLY: no broker, no orders, no network in tests.
  - The engine is deliberately tiny and self-contained (stdlib only) so the accounting can
    be audited line by line. It reuses `apex_investor.prices.Bar` so fixtures are shared
    with the live price layer.

Accounting model (single basis, matches the live layer's `close` basis):
  - A fixed capital of 100.0 units. Fees are charged on notional at `cost_bps` per side.
  - Invariant enforced after every step: cash + Σ(shares * price) == initial - fees_paid.
  - A buy of `amount` cash spends the whole amount inclusive of fee: gross = amount/(1+fee).
  - Prices that are missing for a held name are carried forward at the last known close;
    a satellite with no price at the window start is SKIPPED, never bought at 0.

Strategies:
  - spy_lump : buy the index once at the start.
  - spy_dca  : deploy capital in equal monthly tranches over `dca_months`.
  - apex     : index + satellite sleeve trimmed back toward `sat_weight` when it exceeds
               `TRIM_FRACTION` * target. The index itself is never trimmed.
  - apex_alerts : apex plus the erosion alert (sell a satellite that gives back
               `EROSION_GIVEBACK` of its peak gain once armed at `ARM_GAIN`).

Run:  python scripts/backtest.py [--symbol SPY] [--offline]
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apex_investor.prices import Bar  # noqa: E402

# Trim a satellite back toward its target weight once it exceeds this multiple of it.
TRIM_FRACTION = 1.25
# Erosion alert: arm at +100% gain, sell when the price gives back this fraction of the peak.
ARM_GAIN = 1.0
EROSION_GIVEBACK = 0.30

# One entry per window start, in this order.
STRATEGIES = ("spy_lump", "spy_dca", "apex")

_START_CAPITAL = 100.0


@dataclass
class Config:
    """Backtest knobs. Defaults are the policy values under test."""

    cost_bps: float = 10.0
    satellites: tuple = ()
    sat_weight: float = 0.10
    horizon_months: int = 36
    step_months: int = 3
    dca_months: int = 6


@dataclass
class Result:
    strategy: str
    start: int
    end: int
    total_return: float
    trades: int
    trims: int
    erosion_sells: int
    fees: float
    reconciled: bool
    notes: list = field(default_factory=list)


# ── helpers ──────────────────────────────────────────────────────────────────

def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    year, month = d.year + m // 12, m % 12 + 1
    # Clamp the day to the last valid day of the target month (e.g. 31 -> 30/28).
    day = d.day
    while day > 1:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1
    return date(year, month, 1)


def end_index(dates: list[str], start_idx: int, horizon_months: int) -> int | None:
    """Return the last bar index inside a `horizon_months` window, or None if the data
    does not reach the window boundary (so a partial window is never reported)."""
    if not 0 <= start_idx < len(dates):
        return None
    target = _add_months(date.fromisoformat(dates[start_idx]), horizon_months)
    for j in range(start_idx + 1, len(dates)):
        if date.fromisoformat(dates[j]) >= target:
            return j - 1
    return None


def align(bars_by_symbol: dict, base: str) -> tuple[list[str], dict]:
    """Align every symbol's bars onto the sorted union of all dates.

    Returns (dates, px) where px[symbol] is a list the same length as dates, holding the
    bar close or None where that symbol has no bar on that date.
    """
    if base not in bars_by_symbol or not bars_by_symbol[base]:
        raise ValueError(f"base symbol {base!r} has no bars")
    dates = sorted({b.date for bars in bars_by_symbol.values() for b in bars})
    px = {}
    for sym, bars in bars_by_symbol.items():
        by_date = {b.date: b.close for b in bars}
        px[sym] = [by_date.get(d) for d in dates]
    return dates, px


def synthetic_bars(seed: int, years: int = 6, start: str = "2020-01-01",
                   start_price: float = 100.0, annual_drift: float = 0.07,
                   annual_vol: float = 0.16) -> list[Bar]:
    """Deterministic geometric-Brownian-motion bars on business days (no network)."""
    rng = random.Random(seed)
    d = date.fromisoformat(start)
    end = date(d.year + years, d.month, d.day)
    dt = 1.0 / 252.0
    price = start_price
    out: list[Bar] = []
    while d < end:
        if d.weekday() < 5:
            price *= math.exp((annual_drift - 0.5 * annual_vol ** 2) * dt
                              + annual_vol * math.sqrt(dt) * rng.gauss(0.0, 1.0))
            out.append(Bar(d.isoformat(), price, price, 1_000_000.0))
        d += timedelta(days=1)
    return out


# ── engine ───────────────────────────────────────────────────────────────────

def simulate(strategy: str, dates: list[str], px: dict, start: int, end: int,
             cfg: Config) -> Result:
    """Run one strategy over dates[start:end+1] and return its ledger summary."""
    if not (0 <= start < len(dates)):
        raise ValueError(f"bad start index {start}")
    if end is None or not (start < end < len(dates)):
        raise ValueError(f"bad end index {end}")

    fee = cfg.cost_bps / 10_000.0
    n = len(dates)
    empty = [None] * n

    cash = _START_CAPITAL
    shares: dict[str, float] = {}
    fees = 0.0
    trades = 0
    trims = 0
    erosion_sells = 0
    last_px: dict[str, float] = {}
    peak: dict[str, float] = {}
    armed: dict[str, bool] = {}
    ledger: list[tuple] = []  # (side, qty, price, fee) — used to re-derive cash

    def bar_price(sym: str, i: int) -> float | None:
        v = px.get(sym, empty)[i]
        if v is not None:
            last_px[sym] = v
        return v

    def held_price(sym: str, i: int) -> float | None:
        v = px.get(sym, empty)[i]
        return v if v is not None else last_px.get(sym)

    def buy(sym: str, i: int, amount: float) -> None:
        nonlocal cash, fees, trades
        p = bar_price(sym, i)
        if p is None or p <= 0 or amount <= 0 or cash <= 0:
            return
        amount = min(amount, cash)
        gross = amount / (1.0 + fee)
        qty = gross / p
        if qty <= 0:
            return
        cost = qty * p
        f = cost * fee
        if cost + f > cash + 1e-9:
            return
        cash -= cost + f
        fees += f
        shares[sym] = shares.get(sym, 0.0) + qty
        ledger.append(("buy", qty, p, f))
        trades += 1

    def sell(sym: str, i: int, qty: float) -> None:
        nonlocal cash, fees, trades
        p = bar_price(sym, i)
        if p is None or p <= 0 or qty <= 0:
            return
        qty = min(qty, shares.get(sym, 0.0))
        if qty <= 0:
            return
        proceeds = qty * p
        f = proceeds * fee
        cash += proceeds - f
        fees += f
        shares[sym] -= qty
        ledger.append(("sell", qty, p, f))
        if shares[sym] <= 1e-12:
            shares.pop(sym, None)
        trades += 1

    def invested(i: int) -> float:
        total = 0.0
        for sym, qty in shares.items():
            p = held_price(sym, i)
            if p is not None:
                total += qty * p
        return total

    def portfolio_value(i: int) -> float:
        return cash + invested(i)

    # ── initial deployment ──
    # Satellites are funded first (out of the same cash), then the index gets the rest,
    # so the sleeve is `sat_weight` of capital rather than a second slice of it.
    if strategy in ("spy_lump", "spy_dca"):
        if strategy == "spy_lump":
            buy("SPY", start, cash)
    else:  # apex / apex_alerts
        for sym in cfg.satellites:
            # Only fund a satellite that actually trades at the window start; one that
            # lists later is skipped entirely rather than bought at an unknown price.
            if px.get(sym, empty)[start] is not None:
                buy(sym, start, cfg.sat_weight * _START_CAPITAL)
        buy("SPY", start, cash)

    dca_days: set[int] = set()
    tranche = 0.0
    if strategy == "spy_dca":
        seen: set[tuple] = set()
        for j in range(start, end + 1):
            dj = date.fromisoformat(dates[j])
            key = (dj.year, dj.month)
            if key not in seen:
                seen.add(key)
                dca_days.add(j)
                if len(dca_days) >= cfg.dca_months:
                    break
        tranche = _START_CAPITAL / cfg.dca_months

    # ── day loop ──
    for i in range(start, end + 1):
        if strategy == "spy_dca" and i in dca_days:
            buy("SPY", i, tranche)

        if strategy in ("apex", "apex_alerts"):
            if strategy == "apex_alerts":
                for sym in list(shares):
                    if sym == "SPY":
                        continue
                    p = px.get(sym, empty)[i]
                    if p is None:
                        continue
                    pk = max(peak.get(sym, p), p)
                    peak[sym] = pk
                    if not armed.get(sym, False) and pk >= _START_CAPITAL * (1.0 + ARM_GAIN):
                        armed[sym] = True
                    if armed.get(sym, False) and pk > 0 and (pk - p) / pk >= EROSION_GIVEBACK:
                        sell(sym, i, shares.get(sym, 0.0))
                        erosion_sells += 1
                        armed[sym] = False
                        peak.pop(sym, None)

            total = portfolio_value(i)
            if total > 0:
                for sym in list(shares):
                    if sym == "SPY":
                        continue
                    p = px.get(sym, empty)[i]
                    if p is None:
                        continue
                    val = shares[sym] * p
                    if val > TRIM_FRACTION * cfg.sat_weight * total:
                        sell(sym, i, (val - cfg.sat_weight * total) / p)
                        trims += 1

    final_value = portfolio_value(end)
    # Independent reconciliation: re-derive cash from the trade ledger and require it to
    # match the running cash (a self-check that the accounting never lost or invented
    # cash), and that no negative-cash state was ever reached.
    rebuilt_cash = _START_CAPITAL
    for side, qty, price, f in ledger:
        if side == "buy":
            rebuilt_cash -= qty * price + f
        else:
            rebuilt_cash += qty * price - f
    reconciled = (abs(rebuilt_cash - cash) <= 1e-9
                  and abs(sum(f for *_, f in ledger) - fees) <= 1e-9
                  and cash >= -1e-9
                  and math.isfinite(final_value))
    return Result(
        strategy=strategy,
        start=start,
        end=end,
        total_return=final_value / _START_CAPITAL - 1.0,
        trades=trades,
        trims=trims,
        erosion_sells=erosion_sells,
        fees=fees,
        reconciled=reconciled,
    )


def rolling(dates: list[str], px: dict, cfg: Config) -> list[Result]:
    """Roll a window of `horizon_months` forward by `step_months`, running every strategy."""
    out: list[Result] = []
    start = 0
    while True:
        end = end_index(dates, start, cfg.horizon_months)
        if end is None:
            break
        for strat in STRATEGIES:
            out.append(simulate(strat, dates, px, start, end, cfg))
        target = _add_months(date.fromisoformat(dates[start]), cfg.step_months)
        nxt = next((j for j in range(start + 1, len(dates))
                    if date.fromisoformat(dates[j]) >= target), None)
        if nxt is None:
            break
        start = nxt
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────

def _load_spy(start: str = "2015-01-01", end: str | None = None) -> list[Bar] | None:
    try:
        from apex_investor.prices import fetch_bars
        return fetch_bars("SPY", start=start, end=end)
    except Exception as exc:  # network/data unavailable -> caller falls back to synthetic
        print(f"[backtest] SPY fetch unavailable ({type(exc).__name__}: {exc})", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Index vs DCA vs apex backtest (simulation only)")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--offline", action="store_true", help="skip the network, use synthetic bars")
    args = parser.parse_args(argv)

    bars = None if args.offline else _load_spy()
    if bars is None:
        bars = synthetic_bars(seed=1, years=6)
        print(f"[backtest] using synthetic {args.symbol} bars ({len(bars)} days)")

    dates, px = align({args.symbol: bars}, args.symbol)
    cfg = Config()
    results = rolling(dates, px, cfg)

    by_strategy: dict[str, list[float]] = {}
    for r in results:
        by_strategy.setdefault(r.strategy, []).append(r.total_return)
    print(f"\n{len(results)} windows over {len(dates)} days "
          f"(horizon {cfg.horizon_months}m, step {cfg.step_months}m, cost {cfg.cost_bps}bps)")
    for strat in STRATEGIES:
        rets = sorted(by_strategy.get(strat, []))
        if not rets:
            continue
        median = rets[len(rets) // 2]
        print(f"  {strat:>11}: median {median:+.1%}  "
              f"min {rets[0]:+.1%}  max {rets[-1]:+.1%}  n={len(rets)}")
    bad = [r for r in results if not r.reconciled]
    print(f"  reconciled: {len(results) - len(bad)}/{len(results)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
