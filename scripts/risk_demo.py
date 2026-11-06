"""M4 demo: risk controls on a cash-heavy portfolio + a fail-closed case.

Run:  uv run python scripts/risk_demo.py
Alerts only — nothing here trades.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apex_investor.ledger import Ledger, Side, Tag, Transaction  # noqa: E402
from apex_investor.risk import assess  # noqa: E402


def build(holdings, cash, tags):
    txs, cost = [], 0.0
    for tk, (sh, px) in holdings.items():
        txs.append(Transaction("2024-01-02", tk, Side.BUY, sh, px))
        cost += sh * px
    return Ledger.from_transactions(txs, starting_cash=cash + cost, tags=tags)


def main() -> int:
    # Cash-heavy portfolio: $9,000 cash + $1,000 across two names (5% each of $10k equity).
    led = build({"VTI": (5, 100.0), "JUNK": (5, 100.0)}, cash=9000.0,
                tags={"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE})
    prices = {"VTI": 100.0, "JUNK": 100.0}
    a = assess(led, prices, date="2024-06-01")
    print("CASH-HEAVY PORTFOLIO (the case that misleads naive tools):")
    print(a.render())
    print(f"  VTI weight of TOTAL equity: {a.weights['VTI']:.1%}  (not 50% of invested value)")
    print(f"  cash weight: {a.cash_weight:.1%}\n")

    # Same holdings, but JUNK marked up to 25% of equity -> speculative + concentration alerts,
    # and a peak of 13,500 so equity (10,000) is ~-26% off peak -> drawdown critical.
    led2 = build({"VTI": (5, 100.0), "JUNK": (25, 100.0)}, cash=7000.0,
                 tags={"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE})
    a2 = assess(led2, {"VTI": 100.0, "JUNK": 100.0}, peak_equity=13500.0, date="2024-06-01")
    print("CONCENTRATED + SPECULATIVE + DRAWDOWN:")
    print(a2.render(), "\n")

    # Fail closed: missing price must NOT read as a clean assessment.
    a3 = assess(led2, {"VTI": 100.0}, date="2024-06-01")
    print("FAIL-CLOSED (missing JUNK price):")
    print(a3.render())
    print(f"  trustworthy={a3.trustworthy}  clean={a3.clean}\n")

    # Fail closed: untagged position.
    led4 = build({"VTI": (5, 100.0)}, cash=9500.0, tags={})
    a4 = assess(led4, {"VTI": 100.0}, date="2024-06-01")
    print("FAIL-CLOSED (untagged position):")
    print(f"  trustworthy={a4.trustworthy}  blocked={a4.blocked}")

    ok = (a.clean and not a2.clean and not a3.trustworthy and not a4.trustworthy)
    print(f"\nDEMO: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
