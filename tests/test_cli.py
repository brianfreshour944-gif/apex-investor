"""CLI (scripts/daily_report.py) integration tests.

These run the REAL main() code path (offline, no network) against a temp portfolio and cache,
covering the end-to-end wiring the operator asked about: a missing quote must produce a final
BLOCKED status, a malformed known-actions file must fail closed, and every unverified/unchecked
ticker must be visible in the console output.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import daily_report  # noqa: E402


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _cache_bars(path: Path, symbol: str, bars: list[dict]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{symbol}.json").write_text(json.dumps({"symbol": symbol, "bars": bars}))


def _bar(date: str, close: float) -> dict:
    return {"date": date, "close": close, "adj_close": close, "volume": 1000.0}


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """Point the CLI at a temp portfolio/cache and force offline mode."""
    today = _today()
    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text(json.dumps({
        "starting_cash": 1000.0,
        "holdings": [
            {"ticker": "GOOD", "shares": 1, "cost_basis_ps": 100.0, "tag": "core"},
            {"ticker": "MISSING", "shares": 1, "cost_basis_ps": 100.0, "tag": "core"},
        ],
    }))
    cache = tmp_path / "cache"
    # GOOD has a fresh, checkable quote (a normal move, so sanity = OK)
    _cache_bars(cache, "GOOD", [_bar(yesterday, 100.0), _bar(today, 101.0)])
    # MISSING has NO cache at all -> gather_prices reports it missing

    monkeypatch.setattr(daily_report, "KNOWN_ACTIONS_FILE", tmp_path / "known_actions.json")
    monkeypatch.setattr(daily_report, "LIVE_PORTFOLIO", portfolio)
    monkeypatch.setattr(daily_report, "fetch_bars",
                        lambda tk, **k: _load_cache(cache, tk))
    monkeypatch.setattr(daily_report, "write_report", lambda report, **k: tmp_path / "out.md")
    return {"tmp": tmp_path, "portfolio": portfolio, "cache": cache, "today": today}


def _load_cache(cache: Path, symbol: str):
    from apex_investor.prices import Bar
    f = cache / f"{symbol}.json"
    if not f.exists():
        raise RuntimeError(f"no cache for {symbol}")
    return [Bar(**b) for b in json.loads(f.read_text())["bars"]]


def _run(argv, capsys):
    monkeypatch_argv = ["daily_report.py", *argv]
    import sys as _sys
    old = _sys.argv
    _sys.argv = monkeypatch_argv
    try:
        rc = daily_report.main()
    finally:
        _sys.argv = old
    out = capsys.readouterr()
    return rc, out.out + out.err


# ---- finding 1: a missing quote must yield a final BLOCKED status -------------

def test_cli_missing_quote_blocks_status(workspace, capsys):
    rc, out = _run(["--portfolio", str(workspace["portfolio"]), "--offline"], capsys)
    assert rc == 0
    assert "Status: BLOCKED" in out            # section 2, derived from the final risk state
    assert "no fresh quote for MISSING" in out
    assert "report is BLOCKED, not OK" in out


def test_cli_missing_quote_blocks_via_api(workspace):
    """Same wiring, checked on the report object rather than console text."""
    led, tags = daily_report.load_portfolio(workspace["portfolio"])
    prices, dates, missing, prev = daily_report.gather_prices(sorted(led.positions),
                                                              offline=True)
    assert missing == ["MISSING"]
    as_of = max(dates.values()) if dates else None
    from apex_investor.prices import Bar
    latest = {tk: Bar(dates[tk], prices[tk], prices[tk], 0.0) for tk in prices}
    checks = daily_report.sanity_checks(sorted(led.positions), latest, prev,
                                        limits=daily_report.PriceSanityLimits(),
                                        known_actions=[])
    from apex_investor.report import build_report
    r = build_report(workspace["today"], led, prices, tags=tags, price_as_of=as_of,
                     price_dates=dates, quote_checks=checks, missing_tickers=missing)
    assert r.status == "BLOCKED"
    assert r.risk.trustworthy is False


# ---- finding 3: malformed known-actions file must fail closed -----------------

def test_load_known_actions_absent_is_empty():
    actions, problems = daily_report.load_known_actions(Path("/nonexistent/actions.json"))
    assert actions == [] and problems == []


def test_load_known_actions_valid(tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps([{"ticker": "X", "date": "2026-11-05", "ratio": 8.0}]))
    actions, problems = daily_report.load_known_actions(p)
    assert len(actions) == 1 and problems == []


def test_load_known_actions_malformed_reports_problem(tmp_path):
    p = tmp_path / "a.json"
    p.write_text("{not valid json")
    actions, problems = daily_report.load_known_actions(p)
    assert actions == [] and len(problems) == 1
    assert "unreadable" in problems[0]


def test_load_known_actions_not_a_list_reports_problem(tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"ticker": "X"}))
    actions, problems = daily_report.load_known_actions(p)
    assert actions == [] and len(problems) == 1
    assert "not a JSON list" in problems[0]


def test_cli_malformed_known_actions_blocks(workspace, capsys):
    workspace["tmp"].joinpath("known_actions.json").write_text("{broken")
    rc, out = _run(["--portfolio", str(workspace["portfolio"]), "--offline"], capsys)
    assert rc == 0
    assert "Status: BLOCKED" in out
    assert "unreadable" in out                 # config problem surfaced


# ---- finding 4: unverified/unchecked tickers visible on the console -----------

def test_cli_unchecked_ticker_visible_and_warns(workspace, capsys):
    """A held ticker with a quote but no previous close -> UNVERIFIABLE, console must say so."""
    today = workspace["today"]
    # OVERWRITE GOOD's cache so it has a single bar -> no previous close -> UNVERIFIABLE
    _cache_bars(workspace["cache"], "GOOD", [_bar(today, 101.0)])
    # give MISSING a single fresh bar too, so nothing is "missing" and the WARN path is isolated
    _cache_bars(workspace["cache"], "MISSING", [_bar(today, 100.0)])
    rc, out = _run(["--portfolio", str(workspace["portfolio"]), "--offline"], capsys)
    assert rc == 0
    assert "could not be price-checked" in out
    assert "valuation NOT fully verified" in out
    assert "Status: WARN" in out               # not BLOCKED, but not OK either


def test_cli_suspect_ticker_visible(workspace, capsys):
    today = workspace["today"]
    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    # GOOD jumps +90% -> SUSPECT; MISSING is given a normal fresh bar so it is not the blocker
    _cache_bars(workspace["cache"], "GOOD", [_bar(yesterday, 100.0), _bar(today, 190.0)])
    _cache_bars(workspace["cache"], "MISSING", [_bar(yesterday, 100.0), _bar(today, 100.0)])
    rc, out = _run(["--portfolio", str(workspace["portfolio"]), "--offline"], capsys)
    assert rc == 0
    assert "suspicious quote(s) for ['GOOD']" in out
    assert "Unverified price(s): GOOD" in out
    assert "Valuation is UNVERIFIED for GOOD" in out
