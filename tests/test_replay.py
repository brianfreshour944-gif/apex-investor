"""M6 tests: replay harness classification + live-quote helper. No network (injected fetcher)."""
from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

from apex_investor.prices import Bar, latest_quote

# import the harness script as a module (it lives outside the package)
_spec = importlib.util.spec_from_file_location(
    "replay", Path(__file__).resolve().parent.parent / "scripts" / "replay.py")
replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay)


def bars(n, start="2024-01-02", close=100.0, vol=1000.0):
    d0 = date.fromisoformat(start)
    return [Bar((d0 + timedelta(days=i)).isoformat(), close, close, vol) for i in range(n)]


def test_replay_ok_returns_alerts():
    def fake(ticker, start=None, end=None, use_cache=True, **k):
        return bars(60, close=100.0)  # flat -> no gain alerts
    r = replay.replay_one("TEST", 100.0, "2024-01-01", "2024-12-31", offline=True, fetch=fake)
    assert r["outcome"] == "OK" and r["bars"] == 60


def test_replay_detects_gain_when_price_rises():
    def fake(ticker, start=None, end=None, use_cache=True, **k):
        series = [100.0] * 40 + [260.0] * 20
        return [Bar(f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}", c, c, 1000.0)
                for i, c in enumerate(series)]
    r = replay.replay_one("TEST", 100.0, "2024-01-01", "2024-12-31", offline=True, fetch=fake)
    assert r["outcome"] == "OK"
    assert any(a.kind == "extraordinary_gain" for a in r["alerts"])


def test_replay_fetch_failure_is_data_limitation_not_system_failure():
    def fake(*a, **k):
        raise OSError("network down")
    r = replay.replay_one("TEST", 100.0, "2024-01-01", "2024-12-31", offline=True, fetch=fake)
    assert r["outcome"] == "DATA_LIMITATION"
    assert "network down" in r["detail"]


def test_replay_too_few_bars_is_data_limitation():
    def fake(ticker, start=None, end=None, use_cache=True, **k):
        return bars(5)
    r = replay.replay_one("TEST", 100.0, "2024-01-01", "2024-12-31", offline=True, fetch=fake)
    assert r["outcome"] == "DATA_LIMITATION"
    assert "bars" in r["detail"]


def test_replay_rule_exception_is_system_failure(monkeypatch):
    def fake(ticker, start=None, end=None, use_cache=True, **k):
        return bars(60)
    def boom(*a, **k):
        raise ValueError("rule bug")
    monkeypatch.setattr(replay, "portfolio_signals", boom)
    r = replay.replay_one("TEST", 100.0, "2024-01-01", "2024-12-31", offline=True, fetch=fake)
    assert r["outcome"] == "SYSTEM_FAILURE"


def test_replay_panel_uses_first_close_as_cost_basis():
    # a flat series must produce NO extraordinary_gain when cost == first close
    def fake(ticker, start=None, end=None, use_cache=True, **k):
        return bars(60, close=100.0)
    r = replay.replay_one("TEST", None, "2024-01-01", "2024-12-31", offline=True, fetch=fake)
    assert not any(a.kind == "extraordinary_gain" for a in r["alerts"])


def test_latest_quote_returns_none_on_failure(monkeypatch, tmp_path):
    import apex_investor.prices as prices
    def boom(*a, **k):
        raise RuntimeError("no network")
    monkeypatch.setattr(prices, "fetch_bars", boom)
    assert latest_quote("NOPE", cache_dir=tmp_path) is None


def test_latest_quote_returns_last_bar(monkeypatch, tmp_path):
    import apex_investor.prices as prices
    def fake(symbol, *a, **k):
        return [Bar("2024-01-02", 10.0, 10.0, 1), Bar("2024-01-03", 11.0, 11.0, 1)]
    monkeypatch.setattr(prices, "fetch_bars", fake)
    q = latest_quote("X", cache_dir=tmp_path, today="2024-01-04")
    assert q is not None and q.date == "2024-01-03" and q.close == pytest.approx(11.0)


# ---- REGRESSION: a stale cache entry must not masquerade as a live quote ----

def test_latest_quote_rejects_stale_bar(monkeypatch, tmp_path):
    import apex_investor.prices as prices
    def fake(symbol, *a, **k):
        return [Bar("2024-12-30", 5000.0, 5000.0, 1)]      # 2 years old
    monkeypatch.setattr(prices, "fetch_bars", fake)
    assert latest_quote("DFNS", cache_dir=tmp_path, today="2026-11-06") is None


def test_latest_quote_accepts_recent_bar(monkeypatch, tmp_path):
    import apex_investor.prices as prices
    def fake(symbol, *a, **k):
        return [Bar("2026-11-05", 2.29, 2.29, 1)]
    monkeypatch.setattr(prices, "fetch_bars", fake)
    q = latest_quote("DFNS", cache_dir=tmp_path, today="2026-11-06")
    assert q is not None and q.close == pytest.approx(2.29)


def test_latest_quote_rejects_unparseable_date(monkeypatch, tmp_path):
    import apex_investor.prices as prices
    def fake(symbol, *a, **k):
        return [Bar("garbage", 1.0, 1.0, 1)]
    monkeypatch.setattr(prices, "fetch_bars", fake)
    assert latest_quote("X", cache_dir=tmp_path, today="2026-11-06") is None


# ---- REGRESSION: one fresh ticker must not mask a stale price for another ----

def test_per_ticker_staleness_blocks_report():
    from apex_investor.ledger import Ledger, Side, Tag, Transaction
    from apex_investor.report import build_report
    led = Ledger.from_transactions(
        [Transaction("2000-01-01", "VTI", Side.BUY, 1, 250.0),
         Transaction("2000-01-01", "DFNS", Side.BUY, 100, 3.0)],
        starting_cash=4300.0, tags={"VTI": Tag.CORE, "DFNS": Tag.SPECULATIVE})
    r = build_report("2026-11-06", led, {"VTI": 360.52, "DFNS": 5000.0},
                     tags={"VTI": Tag.CORE, "DFNS": Tag.SPECULATIVE},
                     price_as_of="2026-11-05",                 # overall looks fresh
                     price_dates={"VTI": "2026-11-05", "DFNS": "2024-12-30"})  # DFNS is stale
    assert r.status == "BLOCKED"
    assert any("stale price for DFNS" in b for b in r.risk.blocked)


# ---- REGRESSION: valuation and gain alerts must use ONE price basis ----

def test_gain_alert_and_valuation_agree_on_basis():
    """A dividend payer's close != adj_close; the report must not show two different gains."""
    from apex_investor.ledger import Ledger, Side, Tag, Transaction
    from apex_investor.monitor import portfolio_signals
    from apex_investor.report import build_report
    # cost 100; latest close 200 (raw), adj_close 190 (dividends stripped)
    b = [Bar("2024-01-02", 100.0, 100.0, 1000), Bar("2024-06-01", 200.0, 190.0, 1000)]
    sig = portfolio_signals([{"ticker": "X", "bars": b, "cost_basis_ps": 100.0}])
    gain_alert = next(a for a in sig if a.kind == "extraordinary_gain")
    led = Ledger.from_transactions([Transaction("2024-01-02", "X", Side.BUY, 10, 100.0)],
                                   starting_cash=1000.0, tags={"X": Tag.GROWTH})
    r = build_report("2024-06-01", led, {"X": 200.0}, tags={"X": Tag.GROWTH},
                     price_as_of="2024-06-01", alerts=sig)
    # Both must use the CLOSE basis: unrealized = 10*(200-100) = 1000 (not 900 from adj_close),
    # and the alert gain = +100% (not +90%).
    assert r.holdings[0]["unrealized"] == pytest.approx(1000.0)
    assert gain_alert.evidence["gain"] == "+100.0%"


# ---- REGRESSION: expected availability errors vs unexpected defects ----------

def test_replay_expected_oserror_is_data_limitation():
    def fake(*a, **k):
        raise OSError("dns failure")
    r = replay.replay_one("TEST", 100.0, "2024-01-01", "2024-12-31", offline=True, fetch=fake)
    assert r["outcome"] == "DATA_LIMITATION"
    assert "OSError" in r["detail"]


def test_replay_expected_valueerror_is_system_failure():
    """A bare ValueError is no longer classified as data availability — a defect must surface."""
    def fake(*a, **k):
        raise ValueError("no usable bars in payload")
    r = replay.replay_one("TEST", 100.0, "2024-01-01", "2024-12-31", offline=True, fetch=fake)
    assert r["outcome"] == "SYSTEM_FAILURE"


def test_replay_price_fetch_error_is_data_limitation():
    """The dedicated fetch exception is the ONLY provider-failure signal the harness trusts."""
    from apex_investor.prices import PriceFetchError
    def fake(*a, **k):
        raise PriceFetchError("fetch failed for TEST: OSError('offline')")
    r = replay.replay_one("TEST", 100.0, "2024-01-01", "2024-12-31", offline=True, fetch=fake)
    assert r["outcome"] == "DATA_LIMITATION"
    assert "PriceFetchError" in r["detail"]


def test_replay_unexpected_fetch_error_is_system_failure():
    """A programming defect inside the fetch layer must NOT be hidden as missing data."""
    def fake(*a, **k):
        raise TypeError("fetch layer bug")
    r = replay.replay_one("TEST", 100.0, "2024-01-01", "2024-12-31", offline=True, fetch=fake)
    assert r["outcome"] == "SYSTEM_FAILURE"
    assert "TypeError" in r["detail"]


# ---- REGRESSION: replay_dfns must not crash on empty/short/absent data --------

_spec_d = importlib.util.spec_from_file_location(
    "replay_dfns", Path(__file__).resolve().parent.parent / "scripts" / "replay_dfns.py")
replay_dfns = importlib.util.module_from_spec(_spec_d)
_spec_d.loader.exec_module(replay_dfns)


def test_replay_dfns_empty_data_is_data_limited(monkeypatch, capsys):
    monkeypatch.setattr(replay_dfns, "fetch_bars", lambda *a, **k: [])
    rc = replay_dfns.main()
    out = capsys.readouterr().out
    assert rc == 2
    assert "DATA-LIMITED" in out


def test_replay_dfns_short_data_is_data_limited(monkeypatch, capsys):
    monkeypatch.setattr(replay_dfns, "fetch_bars", lambda *a, **k: bars(5))
    rc = replay_dfns.main()
    out = capsys.readouterr().out
    assert rc == 2
    assert "insufficient history" in out


def test_replay_dfns_fetch_failure_is_data_limited(monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(replay_dfns, "fetch_bars", boom)
    rc = replay_dfns.main()
    out = capsys.readouterr().out
    assert rc == 2
    assert "DATA-LIMITED" in out


def test_replay_dfns_acceptance_passes_with_fixture(monkeypatch, capsys):
    series = [100.0] * 40
    series[16] = 1200.0                      # 2024-12-17 -> +109% vs cost 572.03
    fixture = [Bar((date(2024, 12, 1) + timedelta(days=i)).isoformat(), c, c, 1000.0)
               for i, c in enumerate(series)]
    monkeypatch.setattr(replay_dfns, "fetch_bars", lambda *a, **k: fixture)
    rc = replay_dfns.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "extraordinary_gain alert on 2024-12-17: PASS" in out
