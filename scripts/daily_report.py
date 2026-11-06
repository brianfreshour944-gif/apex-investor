"""Generate a daily report (M5/M6): live prices via the M2 cache layer + console & Markdown.

No broker, no orders, no AI. Nothing here trades.

Usage:
  uv run python scripts/daily_report.py [--date YYYY-MM-DD] [--offline]

Portfolio source: a JSON fixture (see data/portfolio.example.json). The LIVE ledger stays empty
until the operator supplies a real holdings-and-cash snapshot and approves it.
"""
import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apex_investor.ledger import Ledger, Side, Tag, Transaction  # noqa: E402
from apex_investor.prices import Bar, fetch_bars  # noqa: E402
from apex_investor.report import MAX_PRICE_AGE_DAYS, build_report, write_report  # noqa: E402
from apex_investor.sanity import PriceSanityLimits, check_quote  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "data" / "portfolio.example.json"
# The REAL portfolio must be supplied explicitly. There is deliberately NO fallback to the
# example file, so a demo portfolio can never silently become the live ledger.
LIVE_PORTFOLIO = ROOT / "data" / "portfolio.json"
# Optional operator-supplied corporate actions that explain a large move, so the guard flags
# rather than blocks. `ratio` is the PRICE FACTOR (new/old), matching the observed move:
#   - a 1:8 reverse split makes the price rise, so ratio = 8.0  (100 -> 800)
#   - a 4:1 forward split makes the price fall, so ratio = 0.25 (100 -> 25)
# A ratio that does not match the observed factor does NOT explain the move (it stays flagged).
# Format: [{"ticker": "X", "date": "2026-11-05", "ratio": 8.0, "label": "1:8 reverse split"}]
KNOWN_ACTIONS_FILE = ROOT / "data" / "known_actions.json"
# Persisted daily equity so the report's "Daily change" is a real day-over-day number instead of
# a permanent n/a. Example runs use a SEPARATE history so demo data can never mix with live data.
EQUITY_HISTORY_LIVE = ROOT / "data" / "equity_history.json"
EQUITY_HISTORY_EXAMPLE = ROOT / "data" / "equity_history.example.json"


def load_equity_history(path: Path) -> list[dict]:
    """Read the equity history file. Absent or malformed -> [] (fail closed, first-run behaviour)."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return []
    return data if isinstance(data, list) else []


def previous_equity_from_history(history: list, report_date: str) -> float | None:
    """Most recent recorded equity STRICTLY BEFORE `report_date`, else None."""
    best: tuple[str, float] | None = None
    for entry in history:
        if not isinstance(entry, dict):
            continue
        d, eq = entry.get("date"), entry.get("equity")
        if not isinstance(d, str) or not isinstance(eq, (int, float)) or d >= report_date:
            continue
        if best is None or d > best[0]:
            best = (d, float(eq))
    return best[1] if best else None


def append_equity_history(path: Path, report_date: str, equity: float) -> None:
    """Record today's equity, replacing any same-date entry (re-runs refine, never duplicate)."""
    history = [e for e in load_equity_history(path)
               if isinstance(e, dict) and e.get("date") != report_date]
    history.append({"date": report_date, "equity": round(float(equity), 2)})
    history.sort(key=lambda e: e.get("date", ""))
    path.write_text(json.dumps(history, indent=2))



def load_known_actions(path: Path) -> tuple[list[dict], list[str]]:
    """Load operator-supplied corporate actions. Returns (actions, config_problems).

    Distinguishes three cases so a deliberate action is never silently dropped:
      - file absent            -> ([], [])            no actions configured
      - file present and valid -> (actions, [])       actions loaded
      - file present, malformed/unreadable/not a list -> ([], [problem])  fail closed
    """
    if not path.exists():
        return [], []
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError) as e:
        return [], [f"known actions file {path.name} is unreadable ({e.__class__.__name__}); "
                    "treating corporate actions as UNKNOWN (a split may be unexplained)"]
    if not isinstance(data, list):
        return [], [f"known actions file {path.name} is not a JSON list; treating corporate "
                    "actions as UNKNOWN"]
    return data, []


def load_portfolio(path: Path) -> tuple[Ledger, dict]:
    """Read a portfolio fixture: {starting_cash, holdings:[{ticker,shares,cost_basis_ps,tag}]}.

    The snapshot is represented as ONE synthetic BUY per holding dated "2000-01-01", and cash is
    raised to `starting_cash + total cost` so those buys are affordable. Consequence: realised P&L
    and reconciliation are correct FROM THIS OPENING SNAPSHOT FORWARD only. They cannot reconstruct
    gains or losses realised before the snapshot — the snapshot's cost basis is the starting point,
    not a trade history. A real transaction history would need to be supplied instead.
    """
    spec = json.loads(path.read_text())
    tags, txs, cost = {}, [], 0.0
    for h in spec["holdings"]:
        tk = h["ticker"]
        txs.append(Transaction("2000-01-01", tk, Side.BUY, h["shares"], h["cost_basis_ps"]))
        cost += h["shares"] * h["cost_basis_ps"]
        tags[tk] = Tag(h["tag"])
    led = Ledger.from_transactions(txs, starting_cash=spec["starting_cash"] + cost, tags=tags)
    return led, tags


def _fresh_latest(bars: list[Bar]) -> Bar | None:
    """Newest bar if it is within MAX_PRICE_AGE_DAYS of today AND not future-dated, else None."""
    if not bars:
        return None
    bar = bars[-1]
    try:
        age = (datetime.now(UTC).date() - date.fromisoformat(bar.date)).days
    except ValueError:
        return None
    return bar if 0 <= age <= MAX_PRICE_AGE_DAYS else None


def gather_prices(tickers, *, offline: bool) -> tuple[dict, dict, list[str], dict]:
    """Latest quote per ticker. Returns (prices, price_dates, missing, prev_bars). Fail closed.

    One refresh-aware fetch per ticker: a stale cache is refreshed (unless `offline`), so a stale
    cache does not block a report a fresh fetch could satisfy. Live runs ALWAYS attempt a network
    refresh (max_cache_age_days=0), so prices track the market daily instead of riding a
    <=5-day-old cache; on fetch failure the cache layer falls back to cached bars and the
    MAX_PRICE_AGE_DAYS gate decides whether they are still usable. A bar still older than
    MAX_PRICE_AGE_DAYS after that is treated as MISSING, so it cannot pass as a live quote.
    `price_dates` carries each ticker's own date; `prev_bars` the previous valid bar (sanity guard).
    """
    prices: dict[str, float] = {}
    dates: dict[str, str] = {}
    prev_bars: dict = {}
    missing: list[str] = []
    for tk in tickers:
        try:
            # 0 = only a same-day cache is reused without a network call; otherwise refetch.
            # On fetch failure fetch_bars returns the cached bars and the age gate still applies.
            bars = fetch_bars(tk, use_cache=True,
                              max_cache_age_days=None if offline else 0)
        except Exception:  # noqa: BLE001 - a fetch failure means "no fresh quote", not a crash
            missing.append(tk)
            continue
        bar = _fresh_latest(bars)
        if bar is None:
            missing.append(tk)
            continue
        prices[tk] = bar.close
        dates[tk] = bar.date
        prev = [b for b in bars if b.date < bar.date]
        if prev:
            prev_bars[tk] = prev[-1]
    return prices, dates, missing, prev_bars


def sanity_checks(tickers, latest_by_ticker, prev_bars, *, limits, known_actions):
    """Run the price-sanity guard for each ticker that has a quote."""
    return [check_quote(tk, latest_by_ticker.get(tk), prev_bars.get(tk),
                        limits=limits, known_actions=known_actions)
            for tk in tickers if tk in latest_by_ticker]


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate the daily report.")
    ap.add_argument("--date", default=datetime.now(UTC).date().isoformat())
    ap.add_argument("--portfolio", type=Path, default=LIVE_PORTFOLIO,
                    help="path to the portfolio snapshot (default: data/portfolio.json)")
    ap.add_argument("--example", action="store_true",
                    help="use the bundled EXAMPLE portfolio (demo only, never the live ledger)")
    ap.add_argument("--offline", action="store_true", help="use cache only (no network)")
    args = ap.parse_args()

    if args.example:
        portfolio = EXAMPLE
        print(f"NOTE: using EXAMPLE portfolio {portfolio} (demo only).", file=sys.stderr)
    else:
        portfolio = args.portfolio

    if not portfolio.exists():
        print(f"no portfolio snapshot at {portfolio}; nothing to report.\n"
              f"Supply the real holdings with --portfolio, or use --example for a demo.",
              file=sys.stderr)
        return 2
    led, tags = load_portfolio(portfolio)
    history_path = EQUITY_HISTORY_EXAMPLE if args.example else EQUITY_HISTORY_LIVE
    previous_equity = previous_equity_from_history(load_equity_history(history_path), args.date)
    prices, dates, missing, prev_bars = gather_prices(sorted(led.positions),
                                                      offline=args.offline)
    as_of = max(dates.values()) if dates else None

    known_actions, config_problems = load_known_actions(KNOWN_ACTIONS_FILE)
    latest_bars = {tk: Bar(dates[tk], prices[tk], prices[tk], 0.0) for tk in prices}
    checks = sanity_checks(sorted(led.positions), latest_bars, prev_bars,
                           limits=PriceSanityLimits(),
                           known_actions=known_actions)

    # Missing quotes and config problems are passed INTO build_report so the report's status,
    # alerts and blocked list are all derived from one consistent final state (no post-hoc edits).
    report = build_report(args.date, led, prices, tags=tags, price_as_of=as_of,
                          price_dates=dates, quote_checks=checks,
                          missing_tickers=missing, config_problems=config_problems,
                          previous_equity=previous_equity,
                          price_source="Yahoo Finance (cached)")

    print(report.to_console())
    print("\n" + "=" * 70)
    path = write_report(report, regenerate=True)
    print(f"written: {path}")
    if report.preserved_original:
        print(f"regenerated: preserved original as {report.preserved_original}")
    if report.equity is not None:
        try:
            append_equity_history(history_path, args.date, report.equity)
        except OSError as e:
            print(f"WARNING: could not update equity history {history_path.name}: {e}",
                  file=sys.stderr)
    if missing:
        print(f"WARNING: no fresh quote for {missing} — report is BLOCKED, not OK")
    blocked = [c.ticker for c in checks if c.blocks]
    if blocked:
        print(f"WARNING: unvalidatable quote(s) for {blocked} — valuation BLOCKED")
    suspect = [c.ticker for c in checks if c.status == "SUSPECT"]
    if suspect:
        print(f"NOTE: suspicious quote(s) for {suspect} — flagged for verification")
    unchecked = [c.ticker for c in checks if c.status == "UNVERIFIABLE"]
    if unchecked:
        print(f"NOTE: quote(s) could not be price-checked for {unchecked} (no usable previous "
              "close) — valuation NOT fully verified")
    for problem in config_problems:
        print(f"WARNING: {problem}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
