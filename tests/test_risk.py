"""M4 tests: risk controls. Boundaries below/at/above every threshold, plus fail-closed cases.

Key invariant under test: limits are measured against TOTAL PORTFOLIO EQUITY (cash included).
"""
from __future__ import annotations

import pytest

from apex_investor.ledger import Ledger, Side, Tag, Transaction
from apex_investor.risk import RiskAssessment, RiskLimits, assess


def build(holdings, cash, tags=None):
    """holdings: {ticker: (shares, price_paid)}.

    `cash` is the cash REMAINING after the buys (so equity == cash + cost of holdings).
    """
    txs = []
    cost = 0.0
    for tk, (sh, px) in holdings.items():
        txs.append(Transaction("2024-01-02", tk, Side.BUY, sh, px))
        cost += sh * px
    return Ledger.from_transactions(txs, starting_cash=cash + cost, tags=tags or {})


# ---------- 1. concentration vs TOTAL equity ----------------------------------

def test_concentration_uses_total_equity_not_invested_value():
    """Cash-heavy portfolio: a position that is 10% of *invested* value is only ~5% of equity."""
    # 1000 cash + 1000 invested in AAA -> AAA is 50% of invested but 50% of equity too;
    # make it stark: 9000 cash + 1000 in AAA -> AAA is 100% of invested, 10% of equity.
    led = build({"AAA": (10, 100.0)}, cash=9000.0, tags={"AAA": Tag.CORE})
    a = assess(led, {"AAA": 100.0}, date="2024-06-01")
    assert a.trustworthy and a.equity == pytest.approx(10000.0)
    assert a.weights["AAA"] == pytest.approx(0.10)
    assert a.cash_weight == pytest.approx(0.90)
    assert a.alerts == []            # 10% of equity is AT the limit -> no alert


def test_concentration_fires_above_limit():
    led = build({"AAA": (11, 100.0)}, cash=9000.0, tags={"AAA": Tag.CORE})
    a = assess(led, {"AAA": 100.0}, date="2024-06-01")
    assert a.trustworthy
    assert [x.kind for x in a.alerts] == ["concentration"]
    assert a.alerts[0].ticker == "AAA"


def test_concentration_just_below_limit_is_clean():
    led = build({"AAA": (9, 100.0)}, cash=9000.0, tags={"AAA": Tag.CORE})
    a = assess(led, {"AAA": 100.0}, date="2024-06-01")
    assert a.clean


# ---------- 2. speculative allocation -----------------------------------------

def test_speculative_allocation_boundaries():
    # isolate the SPECULATIVE limit from the per-stock limit: split across two names, each <10%.
    # equity = 10000. At limit: 8% + 7% = 15% (clean). Above: 9% + 8% = 17% (warn).
    led_at = build({"A": (8, 100.0), "B": (7, 100.0)}, cash=8500.0,
                   tags={"A": Tag.SPECULATIVE, "B": Tag.SPECULATIVE})
    a_at = assess(led_at, {"A": 100.0, "B": 100.0}, date="2024-06-01")
    assert a_at.trustworthy and a_at.speculative_weight == pytest.approx(0.15)
    assert a_at.clean

    led_over = build({"A": (9, 100.0), "B": (8, 100.0)}, cash=8300.0,
                     tags={"A": Tag.SPECULATIVE, "B": Tag.SPECULATIVE})
    a_over = assess(led_over, {"A": 100.0, "B": 100.0}, date="2024-06-01")
    kinds = {x.kind for x in a_over.alerts}
    assert "speculative_allocation" in kinds


def test_speculative_combines_multiple_positions():
    led = build({"A": (10, 100.0), "B": (10, 100.0)}, cash=8000.0,
                tags={"A": Tag.SPECULATIVE, "B": Tag.SPECULATIVE})
    a = assess(led, {"A": 100.0, "B": 100.0}, date="2024-06-01")
    assert a.speculative_weight == pytest.approx(0.20)
    assert any(x.kind == "speculative_allocation" for x in a.alerts)


# ---------- 3. drawdown --------------------------------------------------------

def test_drawdown_boundaries():
    led = build({"AAA": (10, 100.0)}, cash=9000.0, tags={"AAA": Tag.CORE})
    # at -15% (warn), at -25% (critical), just inside each
    a = assess(led, {"AAA": 100.0}, peak_equity=10000.0 / 0.85, date="d")
    assert any(x.kind == "drawdown" and x.severity == "warn" for x in a.alerts)

    a2 = assess(led, {"AAA": 100.0}, peak_equity=10000.0 / 0.75, date="d")
    assert any(x.kind == "drawdown" and x.severity == "critical" for x in a2.alerts)

    a3 = assess(led, {"AAA": 100.0}, peak_equity=10000.0 / 0.99, date="d")
    assert not any(x.kind == "drawdown" for x in a3.alerts)


def test_drawdown_just_inside_warn_is_clean():
    led = build({"AAA": (10, 100.0)}, cash=9000.0, tags={"AAA": Tag.CORE})
    a = assess(led, {"AAA": 100.0}, peak_equity=10000.0 / 0.86, date="d")  # ~ -14%
    assert not any(x.kind == "drawdown" for x in a.alerts)


# ---------- 4. cash is part of equity -----------------------------------------

def test_cash_included_in_equity_and_weights():
    led = build({"AAA": (5, 100.0)}, cash=500.0, tags={"AAA": Tag.CORE})
    a = assess(led, {"AAA": 100.0}, date="d")
    assert a.equity == pytest.approx(1000.0)
    assert a.weights["AAA"] == pytest.approx(0.5)
    assert a.cash_weight == pytest.approx(0.5)
    assert a.weights["AAA"] + a.cash_weight == pytest.approx(1.0)


# ---------- 5. fail-closed -----------------------------------------------------

def test_missing_price_blocks_clean_verdict():
    led = build({"AAA": (10, 100.0)}, cash=9000.0, tags={"AAA": Tag.CORE})
    a = assess(led, {}, date="d")            # no price supplied
    assert a.trustworthy is False and a.clean is False
    assert any("missing price" in b for b in a.blocked)


def test_invalid_price_blocks():
    led = build({"AAA": (10, 100.0)}, cash=9000.0, tags={"AAA": Tag.CORE})
    a = assess(led, {"AAA": float("nan")}, date="d")
    assert a.trustworthy is False
    assert any("invalid price" in b for b in a.blocked)


def test_untagged_position_blocks():
    led = build({"AAA": (10, 100.0)}, cash=9000.0, tags={})   # no tag
    a = assess(led, {"AAA": 100.0}, date="d")
    assert a.trustworthy is False and a.clean is False
    assert any("untagged" in b for b in a.blocked)


def test_unknown_tag_blocks():
    led = build({"AAA": (10, 100.0)}, cash=9000.0, tags={"AAA": "moonshot"})  # not a Tag
    a = assess(led, {"AAA": 100.0}, date="d")
    assert a.trustworthy is False
    assert any("unknown tag" in b for b in a.blocked)


def test_invalid_limits_block():
    led = build({"AAA": (10, 100.0)}, cash=9000.0, tags={"AAA": Tag.CORE})
    bad = RiskLimits(max_position_weight=float("nan"))
    a = assess(led, {"AAA": 100.0}, limits=bad, date="d")
    assert a.trustworthy is False
    assert any("invalid limit" in b for b in a.blocked)


def test_inverted_drawdown_limits_block():
    led = build({"AAA": (10, 100.0)}, cash=9000.0, tags={"AAA": Tag.CORE})
    bad = RiskLimits(drawdown_warn=-0.30, drawdown_critical=-0.10)  # inverted
    a = assess(led, {"AAA": 100.0}, limits=bad, date="d")
    assert a.trustworthy is False
    assert any("drawdown_warn" in b for b in a.blocked)


def test_invalid_peak_equity_blocks():
    led = build({"AAA": (10, 100.0)}, cash=9000.0, tags={"AAA": Tag.CORE})
    a = assess(led, {"AAA": 100.0}, peak_equity=-5.0, date="d")
    assert a.trustworthy is False
    assert any("peak_equity" in b for b in a.blocked)


def test_unreconciled_ledger_blocks(monkeypatch):
    led = build({"AAA": (10, 100.0)}, cash=9000.0, tags={"AAA": Tag.CORE})
    monkeypatch.setattr(led, "reconcile", lambda prices=None: {"ok": False})
    a = assess(led, {"AAA": 100.0}, date="d")
    assert a.trustworthy is False
    assert any("reconcile" in b for b in a.blocked)


# ---------- 6. no trading ------------------------------------------------------

def test_assess_does_not_mutate_ledger():
    led = build({"AAA": (10, 100.0)}, cash=9000.0, tags={"AAA": Tag.CORE})
    before = (led.cash, {k: (v.shares, v.cost_basis) for k, v in led.positions.items()},
              len(led.transactions))
    assess(led, {"AAA": 100.0}, peak_equity=10000.0, date="d")
    after = (led.cash, {k: (v.shares, v.cost_basis) for k, v in led.positions.items()},
             len(led.transactions))
    assert before == after


def test_clean_assessment_reports_weights_and_no_alerts():
    led = build({"AAA": (5, 100.0), "BBB": (5, 100.0)}, cash=9000.0,
                tags={"AAA": Tag.CORE, "BBB": Tag.GROWTH})
    a = assess(led, {"AAA": 100.0, "BBB": 100.0}, date="2024-06-01")
    assert isinstance(a, RiskAssessment)
    assert a.clean and a.alerts == []
    assert a.equity == pytest.approx(10000.0)


# ---------- finding 3: invalid limit config must not crash ---------------------

def test_non_numeric_drawdown_limits_block_without_crashing():
    """REGRESSION: a string limit must produce a blocked verdict, not a TypeError."""
    led = build({"AAA": (10, 100.0)}, cash=9000.0, tags={"AAA": Tag.CORE})
    bad = RiskLimits(drawdown_warn="oops", drawdown_critical=-0.25)  # type: ignore[arg-type]
    a = assess(led, {"AAA": 100.0}, limits=bad, date="d")
    assert a.trustworthy is False and a.clean is False
    assert any("invalid limit drawdown_warn" in b for b in a.blocked)


def test_out_of_range_limits_block():
    led = build({"AAA": (10, 100.0)}, cash=9000.0, tags={"AAA": Tag.CORE})
    a = assess(led, {"AAA": 100.0}, limits=RiskLimits(max_position_weight=1.5), date="d")
    assert a.trustworthy is False
    assert any("out of range" in b for b in a.blocked)


def test_both_drawdown_limits_non_numeric_does_not_crash():
    led = build({"AAA": (10, 100.0)}, cash=9000.0, tags={"AAA": Tag.CORE})
    bad = RiskLimits(drawdown_warn="x", drawdown_critical=None)  # type: ignore[arg-type]
    a = assess(led, {"AAA": 100.0}, limits=bad, date="d")
    assert a.trustworthy is False
