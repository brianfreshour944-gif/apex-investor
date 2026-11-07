"""Backtest harness tests: accounting, benchmark parity, trim rule, window logic. No network."""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from apex_investor.prices import Bar

_spec = importlib.util.spec_from_file_location(
    "backtest", Path(__file__).resolve().parent.parent / "scripts" / "backtest.py")
bt = importlib.util.module_from_spec(_spec)
sys.modules["backtest"] = bt  # dataclasses needs the module registered
_spec.loader.exec_module(bt)




def series(values, start="2020-01-01"):
    d, out = date.fromisoformat(start), []
    for v in values:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(Bar(d.isoformat(), v, v, 1000.0))
        d += timedelta(days=1)
    return out




def build(spy, **sats):
    m = {"SPY": series(spy)}
    m.update({k: series(v) for k, v in sats.items()})
    return bt.align(m, "SPY")




def test_lump_matches_price_ratio_net_of_costs():
    dates, px = build([100.0] * 10 + [150.0] * 10)
    cfg = bt.Config(cost_bps=10.0)
    r = bt.simulate("spy_lump", dates, px, 0, len(dates) - 1, cfg)
    # one buy, no sell: buying costs 10 bps, so equity = capital/(1.001) * 1.5
    assert r.total_return == pytest.approx(1.5 / 1.001 - 1, rel=1e-6)
    assert r.trades == 1 and r.reconciled




def test_zero_cost_lump_equals_index_return():
    dates, px = build([100.0, 110.0, 121.0])
    r = bt.simulate("spy_lump", dates, px, 0, 2, bt.Config(cost_bps=0.0))
    assert r.total_return == pytest.approx(0.21)




def test_apex_without_satellites_equals_lump():
    dates, px = build([100.0 + i for i in range(60)])
    cfg = bt.Config(satellites=[])
    a = bt.simulate("apex", dates, px, 0, 59, cfg)
    b = bt.simulate("spy_lump", dates, px, 0, 59, cfg)
    assert a.total_return == pytest.approx(b.total_return, rel=1e-9)




def test_concentration_trim_fires_and_ledger_reconciles():
    # satellite triples in the first month and stays high -> must be trimmed back toward 10%
    n = 80
    dates, px = build([100.0] * n, AAA=[100.0] * 5 + [400.0] * (n - 5))
    cfg = bt.Config(satellites=["AAA"], sat_weight=0.05, cost_bps=0.0)
    r = bt.simulate("apex", dates, px, 0, n - 1, cfg)
    assert r.trims >= 1 and r.reconciled
    # trimming into a flat index at zero cost does not change equity
    assert r.total_return == pytest.approx(0.05 * 3.0, rel=1e-6)




def test_index_is_never_trimmed():
    dates, px = build([100.0] * 70)
    cfg = bt.Config(satellites=[])
    assert bt.simulate("apex", dates, px, 0, 69, cfg).trims == 0




def test_erosion_alert_sells_satellite():
    # satellite doubles+ then gives back > 30% of the peak gain
    s = [100.0] * 5 + [150.0, 250.0, 300.0, 200.0] + [200.0] * 70
    n = len(s)
    dates, px = build([100.0] * n, AAA=s)
    cfg = bt.Config(satellites=["AAA"], sat_weight=0.05, cost_bps=0.0)
    r = bt.simulate("apex_alerts", dates, px, 0, n - 1, cfg)
    assert r.erosion_sells == 1 and r.reconciled




def test_satellite_missing_at_start_is_skipped_not_zero():
    n = 70
    dates, px = build([100.0] * n)
    px["LATE"] = [None] * 30 + [50.0] * (n - 30)
    cfg = bt.Config(satellites=["LATE"], sat_weight=0.05)
    r = bt.simulate("apex", dates, px, 0, n - 1, cfg)
    assert r.reconciled and r.total_return == pytest.approx(
        bt.simulate("spy_lump", dates, px, 0, n - 1, cfg).total_return, rel=1e-9)




def test_end_index_requires_full_window():
    dates = [b.date for b in series([1.0] * 300)]
    assert bt.end_index(dates, 0, 36) is None
    j = bt.end_index(dates, 0, 6)
    assert j is not None and dates[j] < "2020-07-01" <= dates[j + 1]




def test_rolling_produces_all_strategies_per_start():
    spy = bt.synthetic_bars(1, years=6)
    dates, px = bt.align({"SPY": spy}, "SPY")
    res = bt.rolling(dates, px, bt.Config(horizon_months=24))
    starts = {r.start for r in res}
    assert len(res) == len(starts) * len(bt.STRATEGIES) and starts
    assert all(r.reconciled for r in res)




def test_dca_deploys_in_tranches():
    n = 400
    dates, px = build([100.0] * n)
    r = bt.simulate("spy_dca", dates, px, 0, n - 1, bt.Config(dca_months=6, cost_bps=0.0))
    assert r.trades == 6 and r.total_return == pytest.approx(0.0, abs=1e-9)
