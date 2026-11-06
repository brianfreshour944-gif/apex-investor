"""M6 replay harness: run the monitoring + risk rules across historical data.

Purpose:
  - Replay DXCM, INUV and DFNS as HISTORICAL FIXTURES through the same rules used live.
  - Run a broader panel of tickers to show the rules behave across many histories.
  - Keep DFNS as an ACCEPTANCE test (not a tuning target): thresholds are the Phase 1 defaults.

Two failure classes are reported separately, so a data gap is never mistaken for a bug:
  - DATA LIMITATION: too few bars / no data for a ticker (the rule cannot be evaluated).
  - SYSTEM FAILURE: the rule raised an exception (a real defect).

Run:  uv run python scripts/replay.py [--offline]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apex_investor.monitor import portfolio_signals  # noqa: E402
from apex_investor.prices import PriceFetchError, fetch_bars  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MIN_BARS = 30  # below this a rule cannot be meaningfully evaluated
# Replay caches historical windows in its OWN directory. Sharing the live cache would poison
# live quotes with bars that stop at the backtest end date.
REPLAY_CACHE = ROOT / "data" / "cache" / "replay"

# The fetch layer raises PriceFetchError for a genuine provider/network/data-availability failure
# (it retries only those cases and wraps them). OSError covers a cache-directory I/O problem. Any
# OTHER exception is a programming defect and must be reported as SYSTEM_FAILURE, never hidden.
EXPECTED_FETCH_ERRORS = (PriceFetchError, OSError)

# Historical fixtures. Cost bases are TEST FIXTURES (split-adjusted), not the operator's ledger.
FIXTURES = [
    {"ticker": "DFNS", "cost_basis_ps": 572.03, "start": "2024-09-01", "end": "2025-03-31",
     "expect_kind": "extraordinary_gain", "expect_date": "2024-12-17"},
    {"ticker": "DXCM", "cost_basis_ps": 120.0, "start": "2024-01-01", "end": "2024-12-31"},
    {"ticker": "INUV", "cost_basis_ps": 2.00, "start": "2024-01-01", "end": "2024-12-31"},
]

# A small, fixed panel (deterministic; not tuned). Extend via --panel-file for the full S&P set.
PANEL = ["AAPL", "MSFT", "JPM", "XOM", "JNJ", "PG", "KO", "WMT", "GE", "F"]


def load_panel(panel_file: Path | None) -> list[str]:
    if panel_file and panel_file.exists():
        data = json.loads(panel_file.read_text())
        return [x["ticker"] if isinstance(x, dict) else x for x in data]
    return PANEL


def replay_one(ticker: str, cost_basis_ps: float | None, start: str, end: str,
               *, offline: bool, fetch=fetch_bars) -> dict:
    """Run the rules for one ticker. Classifies data limitation vs system failure.

    If `cost_basis_ps` is None the first bar's close is used as the cost basis (useful for a
    panel, where there is no real entry price). `fetch` is injectable for offline tests.
    """
    try:
        bars = fetch(ticker, start=start, end=end, use_cache=not offline, cache_dir=REPLAY_CACHE)
    except EXPECTED_FETCH_ERRORS as e:
        # expected data/network availability problem -> not a defect
        return {"ticker": ticker, "outcome": "DATA_LIMITATION",
                "detail": f"fetch unavailable: {e.__class__.__name__}: {e}", "bars": 0, "alerts": []}
    except Exception as e:  # noqa: BLE001 - anything else is a real defect, not missing data
        return {"ticker": ticker, "outcome": "SYSTEM_FAILURE",
                "detail": f"unexpected fetch error {e.__class__.__name__}: {e!r}",
                "bars": 0, "alerts": []}
    if len(bars) < MIN_BARS:
        return {"ticker": ticker, "outcome": "DATA_LIMITATION",
                "detail": f"only {len(bars)} bars (min {MIN_BARS})", "bars": len(bars), "alerts": []}
    cost = bars[0].close if cost_basis_ps is None else cost_basis_ps
    try:
        alerts = portfolio_signals([{"ticker": ticker, "bars": bars, "cost_basis_ps": cost}])
    except Exception as e:  # noqa: BLE001 - a rule raising IS a system failure
        return {"ticker": ticker, "outcome": "SYSTEM_FAILURE", "detail": repr(e),
                "bars": len(bars), "alerts": []}
    return {"ticker": ticker, "outcome": "OK", "detail": "", "bars": len(bars), "alerts": alerts}


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay rules across historical data.")
    ap.add_argument("--offline", action="store_true", help="use cache only (no network)")
    ap.add_argument("--panel-file", type=Path, default=None)
    args = ap.parse_args()

    print("=" * 78)
    print("HISTORICAL FIXTURES")
    print("=" * 78)
    failures = 0
    for fx in FIXTURES:
        r = replay_one(fx["ticker"], fx["cost_basis_ps"], fx["start"], fx["end"],
                       offline=args.offline)
        print(f"\n{r['ticker']}: {r['outcome']} ({r['bars']} bars)"
              + (f" — {r['detail']}" if r["detail"] else ""))
        for a in r["alerts"]:
            print("   " + a.render())
        if r["outcome"] == "SYSTEM_FAILURE":
            failures += 1
        if fx.get("expect_kind"):
            hit = any(a.kind == fx["expect_kind"] and a.date == fx["expect_date"]
                      for a in r["alerts"])
            print(f"   ACCEPTANCE expect {fx['expect_kind']} on {fx['expect_date']}: "
                  f"{'PASS' if hit else 'FAIL'}")
            if not hit:
                failures += 1

    panel = load_panel(args.panel_file)
    print("\n" + "=" * 78)
    print(f"PANEL ({len(panel)} tickers)")
    print("=" * 78)
    ok = data_limited = 0
    for tk in panel:
        r = replay_one(tk, None, "2024-01-01", "2024-12-31", offline=args.offline)
        if r["outcome"] == "OK":
            ok += 1
            kinds = sorted({a.kind for a in r["alerts"]})
            print(f"  {tk:6s} OK    {r['bars']:4d} bars  alerts={kinds or 'none'}")
        elif r["outcome"] == "DATA_LIMITATION":
            data_limited += 1
            print(f"  {tk:6s} DATA  {r['detail']}")
        else:
            failures += 1
            print(f"  {tk:6s} FAIL  {r['detail']}")

    print("\n" + "=" * 78)
    print(f"SUMMARY: {ok} evaluated, {data_limited} data-limited, {failures} failures")
    print("DFNS is an acceptance test, not a tuning target (default thresholds, no fitting).")
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
