"""Portfolio ledger: append-only transactions -> positions, cash, cost basis, P&L.

Design rules:
  - State is ALWAYS reconstructable from the transaction list (never mutate history).
  - Buys capitalise fees into cost basis; sells deduct fees from proceeds.
  - Cost basis uses average cost. Realised P&L is booked on each sell.
  - Money is tracked in float but rounded to cents for reporting; tests assert on rounded values.

This is pure accounting: no prices, no strategy, no network.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date as _date
from enum import StrEnum

CENTS = 2


def _r(x: float) -> float:
    return round(x + 0.0, CENTS)


def _finite(x) -> bool:
    """A usable number: a real finite float, never a bool. Rejects None, str, NaN and +/-inf."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Tag(StrEnum):
    CORE = "core"
    GROWTH = "growth"
    SPECULATIVE = "speculative"


@dataclass(frozen=True)
class Transaction:
    date: str            # ISO date, e.g. "2024-12-16"
    ticker: str
    side: Side
    shares: float
    price: float
    fee: float = 0.0
    note: str = ""

    def __post_init__(self):
        if not _finite(self.shares):
            raise ValueError(f"shares must be a finite number, got {self.shares!r}")
        if not _finite(self.price):
            raise ValueError(f"price must be a finite number, got {self.price!r}")
        if not _finite(self.fee):
            raise ValueError(f"fee must be a finite number, got {self.fee!r}")
        if self.shares <= 0:
            raise ValueError(f"shares must be > 0, got {self.shares}")
        if self.price < 0:
            raise ValueError(f"price must be >= 0, got {self.price}")
        if self.fee < 0:
            raise ValueError(f"fee must be >= 0, got {self.fee}")
        _date.fromisoformat(self.date)  # validates format

    @property
    def gross(self) -> float:
        return self.shares * self.price


@dataclass
class Position:
    ticker: str
    shares: float = 0.0
    cost_basis: float = 0.0     # total cost of shares currently held (incl. fees)
    tag: Tag = Tag.GROWTH

    @property
    def avg_cost(self) -> float:
        return self.cost_basis / self.shares if self.shares > 0 else 0.0

    def market_value(self, price: float) -> float:
        return self.shares * price

    def unrealized(self, price: float) -> float:
        return self.market_value(price) - self.cost_basis


@dataclass
class Ledger:
    """Append-only portfolio. Start with cash; apply transactions in order."""
    starting_cash: float = 0.0
    transactions: list[Transaction] = field(default_factory=list)
    tags: dict[str, Tag] = field(default_factory=dict)

    def __post_init__(self):
        if not _finite(self.starting_cash):
            raise ValueError(f"starting_cash must be a finite number, got {self.starting_cash!r}")
        if self.starting_cash < 0:
            raise ValueError(f"starting_cash must be >= 0, got {self.starting_cash}")

    # ---- construction -------------------------------------------------
    @classmethod
    def from_transactions(cls, transactions, starting_cash: float = 0.0, tags=None):
        led = cls(starting_cash=starting_cash, tags=dict(tags or {}))
        for t in transactions:
            led.record(t)
        return led

    def record(self, t: Transaction) -> None:
        """Append a transaction. Validates it cannot create negative cash/shares."""
        # simulate on a copy of current state
        cash = self.cash
        pos = dict(self.positions)
        p = pos.get(t.ticker, Position(t.ticker))
        if t.side == Side.BUY:
            cash -= t.gross + t.fee
            p.shares += t.shares
            p.cost_basis += t.gross + t.fee
        else:
            if t.shares > p.shares + 1e-9:
                raise ValueError(
                    f"sell {t.shares} {t.ticker} exceeds held {p.shares:.6f}")
            avg = p.avg_cost
            cash += t.gross - t.fee
            p.shares -= t.shares
            p.cost_basis -= avg * t.shares
            if p.shares <= 1e-9:
                p.shares = 0.0
                p.cost_basis = 0.0
        if cash < -1e-9:
            raise ValueError(f"insufficient cash: need {t.gross + t.fee:.2f}, have {self.cash:.2f}")
        self.transactions.append(t)
        if t.ticker in self.tags:
            p.tag = self.tags[t.ticker]

    # ---- derived state ------------------------------------------------
    @property
    def cash(self) -> float:
        c = self.starting_cash
        for t in self.transactions:
            if t.side == Side.BUY:
                c -= t.gross + t.fee
            else:
                c += t.gross - t.fee
        return c

    @property
    def positions(self) -> dict[str, Position]:
        pos: dict[str, Position] = {}
        for t in self.transactions:
            p = pos.setdefault(t.ticker, Position(t.ticker, tag=self.tags.get(t.ticker, Tag.GROWTH)))
            if t.side == Side.BUY:
                p.shares += t.shares
                p.cost_basis += t.gross + t.fee
            else:
                avg = p.cost_basis / p.shares if p.shares > 0 else 0.0
                p.shares -= t.shares
                p.cost_basis -= avg * t.shares
                if p.shares <= 1e-9:
                    p.shares, p.cost_basis = 0.0, 0.0
        return {k: v for k, v in pos.items() if v.shares > 1e-9}

    @property
    def realized_pnl(self) -> float:
        """Total realised P&L across all closed/reduced lots (average cost)."""
        pos: dict[str, Position] = {}
        realized = 0.0
        for t in self.transactions:
            p = pos.setdefault(t.ticker, Position(t.ticker))
            if t.side == Side.BUY:
                p.shares += t.shares
                p.cost_basis += t.gross + t.fee
            else:
                avg = p.cost_basis / p.shares if p.shares > 0 else 0.0
                realized += (t.gross - t.fee) - avg * t.shares
                p.shares -= t.shares
                p.cost_basis -= avg * t.shares
                if p.shares <= 1e-9:
                    p.shares, p.cost_basis = 0.0, 0.0
        return realized

    @property
    def fees_paid(self) -> float:
        return sum(t.fee for t in self.transactions)

    @property
    def invested_cost(self) -> float:
        return sum(p.cost_basis for p in self.positions.values())

    def market_value(self, prices: dict[str, float]) -> float:
        return sum(p.market_value(prices.get(tk, 0.0)) for tk, p in self.positions.items())

    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        return sum(p.unrealized(prices.get(tk, 0.0)) for tk, p in self.positions.items())

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    # ---- reconciliation ----------------------------------------------
    def reconcile(self, prices: dict[str, float] | None = None) -> dict:
        """Cash-flow identity check. Returns a dict; `ok` must be True."""
        prices = prices or {}
        flows = 0.0
        for t in self.transactions:
            flows += -(t.gross + t.fee) if t.side == Side.BUY else (t.gross - t.fee)
        expected_cash = self.starting_cash + flows
        ok_cash = abs(expected_cash - self.cash) < 1e-6
        # equity = cash + holdings; and equity == starting_cash + realized + unrealized - fees?
        # Cleanest invariant: equity = starting_cash + realized_pnl + unrealized_pnl
        eq = self.equity(prices)
        identity = self.starting_cash + self.realized_pnl + self.unrealized_pnl(prices)
        ok_pnl = abs(eq - identity) < 1e-6
        return {"ok": ok_cash and ok_pnl, "cash": _r(self.cash),
                "equity": _r(eq), "realized_pnl": _r(self.realized_pnl),
                "unrealized_pnl": _r(self.unrealized_pnl(prices)),
                "fees_paid": _r(self.fees_paid),
                "cash_ok": ok_cash, "pnl_ok": ok_pnl}

    # ---- tag management (operator-assigned) ---------------------------
    def set_tag(self, ticker: str, tag: Tag) -> None:
        self.tags[ticker] = tag
