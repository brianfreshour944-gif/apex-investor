"""M6 integration tests: monitor signals wired into the report, fail-closed on stale/missing data.

These exercise the REAL code paths end to end (ledger -> risk -> monitor -> report), with no mocks.
"""
from __future__ import annotations

from datetime import date, timedelta

from apex_investor.ledger import Ledger, Side, Tag, Transaction
from apex_investor.monitor import Alert, portfolio_signals
from apex_investor.prices import Bar
from apex_investor.report import build_report, write_report


def build(holdings, cash, tags=None):
    txs, cost = [], 0.0
    for tk, (sh, px) in holdings.items():
        txs.append(Transaction("2024-01-02", tk, Side.BUY, sh, px))
        cost += sh * px
    return Ledger.from_transactions(txs, starting_cash=cash + cost, tags=tags or {})


def bars(closes, start="2024-06-01"):
    d0 = date.fromisoformat(start)
    out = []
    for i, c in enumerate(closes):
        d = (d0 + timedelta(days=i)).isoformat()
        out.append(Bar(d, c, c, 1000))
    return out


TAGS = {"VTI": Tag.CORE, "DFNS": Tag.SPECULATIVE}


# ---------- monitor wired into the report -------------------------------------

def test_monitor_alerts_flow_into_report_once_with_evidence():
    led = build({"VTI": (5, 100.0), "DFNS": (5, 100.0)}, cash=9000.0, tags=TAGS)
    # DFNS rises +150% from cost 100 -> extraordinary_gain; VTI flat.
    sig = portfolio_signals([
        {"ticker": "DFNS", "bars": bars([100, 120, 260]), "cost_basis_ps": 100.0},
        {"ticker": "VTI", "bars": bars([100, 100, 100]), "cost_basis_ps": 100.0},
    ])
    r = build_report("2024-06-10", led, {"VTI": 100.0, "DFNS": 260.0}, tags=TAGS,
                     price_as_of="2024-06-10", alerts=sig)
    kinds = [(a.ticker, a.kind) for a in r.alerts]
    assert kinds.count(("DFNS", "extraordinary_gain")) == 1     # appears exactly once
    md = r.to_markdown()
    assert "extraordinary_gain" in md and "evidence:" in md
    assert "gain=" in md                                        # supporting evidence present


def test_duplicate_signals_are_deduplicated():
    dup = [Alert("2024-06-03", "DFNS", "extraordinary_gain", "critical", "m", {"gain": "150%"}),
           Alert("2024-06-03", "DFNS", "extraordinary_gain", "critical", "m", {"gain": "150%"})]
    led = build({"DFNS": (5, 100.0)}, cash=9500.0, tags=TAGS)
    r = build_report("2024-06-10", led, {"DFNS": 260.0}, tags=TAGS,
                     price_as_of="2024-06-10", alerts=dup)
    assert [(a.ticker, a.kind) for a in r.alerts].count(("DFNS", "extraordinary_gain")) == 1


# ---------- fail closed on stale / missing data --------------------------------

def test_stale_prices_block_the_report():
    led = build({"VTI": (5, 100.0), "DFNS": (5, 100.0)}, cash=9000.0, tags=TAGS)
    # report date is 20 days after the price snapshot -> stale, must NOT be OK
    r = build_report("2024-06-20", led, {"VTI": 100.0, "DFNS": 100.0}, tags=TAGS,
                     price_as_of="2024-06-01")
    assert r.status == "BLOCKED"
    assert r.risk.trustworthy is False
    assert any("stale prices" in b for b in r.risk.blocked)
    assert "stale prices" in r.to_markdown()


def test_missing_price_as_of_blocks_the_report():
    led = build({"VTI": (5, 100.0)}, cash=9500.0, tags=TAGS)
    r = build_report("2024-06-10", led, {"VTI": 100.0}, tags=TAGS)  # no price_as_of
    assert r.status == "BLOCKED"
    assert any("no price as-of" in b for b in r.risk.blocked)


def test_future_price_date_blocks_the_report():
    led = build({"VTI": (5, 100.0)}, cash=9500.0, tags=TAGS)
    r = build_report("2024-06-10", led, {"VTI": 100.0}, tags=TAGS, price_as_of="2024-06-11")
    assert r.status == "BLOCKED"
    assert any("after report date" in b for b in r.risk.blocked)


def test_invalid_price_date_blocks_the_report():
    led = build({"VTI": (5, 100.0)}, cash=9500.0, tags=TAGS)
    r = build_report("2024-06-10", led, {"VTI": 100.0}, tags=TAGS, price_as_of="not-a-date")
    assert r.status == "BLOCKED"
    assert any("invalid price as-of" in b for b in r.risk.blocked)


def test_fresh_prices_are_ok():
    led = build({"VTI": (5, 100.0), "DFNS": (5, 100.0)}, cash=9000.0, tags=TAGS)
    r = build_report("2024-06-02", led, {"VTI": 100.0, "DFNS": 100.0}, tags=TAGS,
                     price_as_of="2024-06-01",
                     price_dates={"VTI": "2024-06-01", "DFNS": "2024-06-01"})
    assert r.status == "OK"
    assert r.risk.trustworthy is True


# ---------- blocked report is still written, never overwrites -----------------

def test_blocked_run_writes_error_report_and_preserves_previous(tmp_path):
    write_report(build_report("2024-06-09", build({"VTI": (5, 100.0)}, cash=9500.0, tags=TAGS),
                              {"VTI": 100.0}, tags=TAGS, price_as_of="2024-06-09"),
                 reports_dir=tmp_path)
    prev = (tmp_path / "2024-06-09.md").read_text()

    blocked = build_report("2024-06-10", build({"VTI": (5, 100.0)}, cash=9500.0, tags=TAGS),
                           {"VTI": 100.0}, tags=TAGS, price_as_of="2024-06-01")
    write_report(blocked, reports_dir=tmp_path)

    assert (tmp_path / "2024-06-09.md").read_text() == prev      # untouched
    assert "BLOCKED" in (tmp_path / "2024-06-10.md").read_text()  # explicit error report
