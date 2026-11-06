"""M1 tests: ledger accounting, cost basis, fees, realised P&L, reconciliation invariants."""
from __future__ import annotations

import pytest

from apex_investor.ledger import Ledger, Side, Tag, Transaction


def tx(d, tk, side, sh, px, fee=0.0, note=""):
    return Transaction(date=d, ticker=tk, side=Side[side], shares=sh, price=px, fee=fee, note=note)


def test_buy_reduces_cash_including_fee():
    led = Ledger.from_transactions([tx("2024-01-02", "AAPL", "BUY", 10, 100.0, fee=1.0)],
                                   starting_cash=2000.0)
    assert led.cash == pytest.approx(2000.0 - 1000.0 - 1.0)
    assert led.positions["AAPL"].shares == 10
    # fee is capitalised into cost basis
    assert led.positions["AAPL"].cost_basis == pytest.approx(1001.0)
    assert led.positions["AAPL"].avg_cost == pytest.approx(100.1)


def test_sell_books_realized_pnl_and_deducts_fee():
    led = Ledger.from_transactions([
        tx("2024-01-02", "AAPL", "BUY", 10, 100.0, fee=1.0),   # basis 1001
        tx("2024-02-02", "AAPL", "SELL", 10, 110.0, fee=1.0),  # proceeds 1099
    ], starting_cash=2000.0)
    assert led.positions == {}                       # fully closed
    assert led.realized_pnl == pytest.approx(1099.0 - 1001.0)  # +98
    assert led.cash == pytest.approx(2000.0 - 1001.0 + 1099.0)


def test_partial_sell_keeps_average_cost():
    led = Ledger.from_transactions([
        tx("2024-01-02", "X", "BUY", 10, 100.0),
        tx("2024-01-03", "X", "BUY", 10, 120.0),   # avg 110, basis 2200
        tx("2024-02-02", "X", "SELL", 5, 130.0),   # realised 5*(130-110)=100
    ], starting_cash=5000.0)
    p = led.positions["X"]
    assert p.shares == 15
    assert p.avg_cost == pytest.approx(110.0)
    assert led.realized_pnl == pytest.approx(100.0)


def test_reconcile_identity_holds_with_prices():
    led = Ledger.from_transactions([
        tx("2024-01-02", "AAPL", "BUY", 10, 100.0, fee=1.0),
        tx("2024-02-02", "AAPL", "SELL", 4, 110.0, fee=0.5),
    ], starting_cash=2000.0)
    r = led.reconcile({"AAPL": 105.0})
    assert r["ok"] is True, r
    assert r["cash_ok"] and r["pnl_ok"]
    # equity == starting_cash + realized + unrealized
    assert r["equity"] == pytest.approx(2000.0 + led.realized_pnl + led.unrealized_pnl({"AAPL": 105.0}))


def test_insufficient_cash_rejected():
    with pytest.raises(ValueError, match="insufficient cash"):
        Ledger.from_transactions([tx("2024-01-02", "A", "BUY", 100, 100.0)], starting_cash=50.0)


def test_oversell_rejected():
    with pytest.raises(ValueError, match="exceeds held"):
        Ledger.from_transactions([
            tx("2024-01-02", "A", "BUY", 5, 100.0),
            tx("2024-01-03", "A", "SELL", 6, 100.0),
        ], starting_cash=1000.0)


def test_negative_inputs_rejected():
    with pytest.raises(ValueError):
        tx("2024-01-02", "A", "BUY", -1, 100.0)
    with pytest.raises(ValueError):
        tx("2024-01-02", "A", "BUY", 1, -5.0)
    with pytest.raises(ValueError):
        tx("2024-01-02", "A", "BUY", 1, 5.0, fee=-1.0)
    with pytest.raises(ValueError):
        tx("2024-13-99", "A", "BUY", 1, 5.0)


def test_state_reconstructable_from_transactions_only():
    """The same transaction list must always yield identical state (append-only)."""
    txs = [tx("2024-01-02", "A", "BUY", 10, 100.0, fee=1.0),
           tx("2024-02-02", "A", "SELL", 3, 150.0, fee=0.5),
           tx("2024-03-02", "B", "BUY", 7, 50.0)]
    a = Ledger.from_transactions(txs, starting_cash=2000.0)
    b = Ledger.from_transactions(list(txs), starting_cash=2000.0)
    assert a.cash == b.cash
    assert a.realized_pnl == b.realized_pnl
    assert {k: (v.shares, v.cost_basis) for k, v in a.positions.items()} == \
           {k: (v.shares, v.cost_basis) for k, v in b.positions.items()}


def test_tags_operator_assigned():
    led = Ledger.from_transactions(
        [tx("2024-01-02", "DFNS", "BUY", 1, 4.58)],
        starting_cash=1000.0, tags={"DFNS": Tag.SPECULATIVE})
    assert led.positions["DFNS"].tag is Tag.SPECULATIVE
    led.set_tag("DFNS", Tag.CORE)
    assert led.positions["DFNS"].tag is Tag.CORE


# ---- round 6: reject non-finite / non-numeric transaction values --------------

def test_nan_shares_rejected():
    with pytest.raises(ValueError, match="shares must be a finite number"):
        tx("2024-01-02", "A", "BUY", float("nan"), 100.0)


def test_nan_price_rejected():
    with pytest.raises(ValueError, match="price must be a finite number"):
        tx("2024-01-02", "A", "BUY", 1, float("nan"))


def test_nan_fee_rejected():
    with pytest.raises(ValueError, match="fee must be a finite number"):
        tx("2024-01-02", "A", "BUY", 1, 100.0, fee=float("nan"))


def test_infinite_values_rejected():
    for bad in (float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite"):
            tx("2024-01-02", "A", "BUY", bad, 100.0)
        with pytest.raises(ValueError, match="finite"):
            tx("2024-01-02", "A", "BUY", 1, bad)
        with pytest.raises(ValueError, match="finite"):
            tx("2024-01-02", "A", "BUY", 1, 100.0, fee=bad)


def test_boolean_values_rejected():
    """A bool is not a number for accounting purposes; True/False must not pass as 1/0."""
    with pytest.raises(ValueError, match="finite"):
        tx("2024-01-02", "A", "BUY", True, 100.0)
    with pytest.raises(ValueError, match="finite"):
        tx("2024-01-02", "A", "BUY", 1, True)
    with pytest.raises(ValueError, match="finite"):
        tx("2024-01-02", "A", "BUY", 1, 100.0, fee=False)


def test_non_numeric_values_rejected():
    for bad in ("10", None):
        with pytest.raises(ValueError, match="finite"):
            tx("2024-01-02", "A", "BUY", bad, 100.0)


def test_zero_price_is_permitted():
    """A zero price is legitimate for worthless/zero-valued shares and must not be rejected."""
    t = tx("2024-01-02", "A", "BUY", 1, 0.0)
    assert t.price == 0.0 and t.gross == 0.0


def test_nan_starting_cash_rejected():
    with pytest.raises(ValueError, match="starting_cash must be a finite number"):
        Ledger(starting_cash=float("nan"))


def test_infinite_starting_cash_rejected():
    for bad in (float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="starting_cash must be a finite number"):
            Ledger(starting_cash=bad)


def test_negative_starting_cash_rejected():
    with pytest.raises(ValueError, match="starting_cash must be >= 0"):
        Ledger(starting_cash=-1.0)


def test_boolean_starting_cash_rejected():
    with pytest.raises(ValueError, match="finite"):
        Ledger(starting_cash=True)


def test_non_finite_inputs_cannot_contaminate_equity():
    """A rejected transaction must never reach cash, cost basis or equity."""
    with pytest.raises(ValueError):
        Ledger.from_transactions([tx("2024-01-02", "A", "BUY", float("nan"), 100.0)],
                                 starting_cash=1000.0)
