"""M5 tests: daily report structure, no-silent-overwrite, blocked reports, console consistency."""
from __future__ import annotations

import pytest

from apex_investor.ledger import Ledger, Side, Tag, Transaction
from apex_investor.report import build_report, write_report


def build(holdings, cash, tags=None):
    txs, cost = [], 0.0
    for tk, (sh, px) in holdings.items():
        txs.append(Transaction("2024-01-02", tk, Side.BUY, sh, px))
        cost += sh * px
    return Ledger.from_transactions(txs, starting_cash=cash + cost, tags=tags or {})


def clean_ledger():
    return build({"VTI": (5, 100.0), "JUNK": (5, 100.0)}, cash=9000.0,
                 tags={"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE})


def test_report_has_all_six_sections():
    r = build_report("2024-06-01", clean_ledger(), {"VTI": 100.0, "JUNK": 100.0},
                     tags={"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE}, price_as_of="2024-06-01",
                     generated_at="2024-06-01T12:00:00Z")
    md = r.to_markdown()
    for heading in ["## 1. Portfolio snapshot", "## 2. Risk status", "## 3. Alerts",
                    "## 4. Evidence", "## 5. Blocked checks", "## 6. Summary"]:
        assert heading in md, heading
    assert "# Daily Report — 2024-06-01" in md
    assert "you approve every trade" in md


def test_report_snapshot_numbers():
    r = build_report("2024-06-01", clean_ledger(), {"VTI": 100.0, "JUNK": 100.0},
                     tags={"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE}, price_as_of="2024-06-01",
                     price_dates={"VTI": "2024-06-01", "JUNK": "2024-06-01"})
    assert r.equity == pytest.approx(10000.0)
    assert r.cash == pytest.approx(9000.0)
    assert r.cash_weight == pytest.approx(0.9)
    assert r.status == "OK"


def test_daily_change_reported():
    r = build_report("2024-06-01", clean_ledger(), {"VTI": 100.0, "JUNK": 100.0},
                     tags={"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE}, previous_equity=9800.0,
                     price_as_of="2024-06-01")
    delta, pct = r.daily_change
    assert delta == pytest.approx(200.0)
    assert pct == pytest.approx(200.0 / 9800.0)
    assert "$200.00" in r.to_markdown()


def test_blocked_report_states_blocked_and_lists_reasons():
    r = build_report("2024-06-01", clean_ledger(), {"VTI": 100.0},  # JUNK price missing
                     tags={"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE}, price_as_of="2024-06-01")
    assert r.status == "BLOCKED"
    md = r.to_markdown()
    assert "BLOCKED" in md
    assert "missing price for JUNK" in md


def test_alerts_rendered_with_evidence():
    led = build({"JUNK": (25, 100.0)}, cash=7500.0, tags={"JUNK": Tag.SPECULATIVE})
    r = build_report("2024-06-01", led, {"JUNK": 100.0}, tags={"JUNK": Tag.SPECULATIVE},
                     price_as_of="2024-06-01", price_dates={"JUNK": "2024-06-01"})
    md = r.to_markdown()
    assert "concentration" in md and "speculative_allocation" in md
    assert "evidence:" in md
    assert r.status == "WARN"


def test_console_matches_report():
    r = build_report("2024-06-01", clean_ledger(), {"VTI": 100.0, "JUNK": 100.0},
                     tags={"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE}, price_as_of="2024-06-01")
    assert r.to_console() == r.to_markdown().rstrip()


# ---- write behaviour ---------------------------------------------------------

def test_write_creates_predictable_path(tmp_path):
    r = build_report("2024-06-01", clean_ledger(), {"VTI": 100.0, "JUNK": 100.0},
                     tags={"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE}, price_as_of="2024-06-01")
    path = write_report(r, reports_dir=tmp_path)
    assert path == tmp_path / "2024-06-01.md"
    assert path.exists()


def test_write_refuses_to_overwrite_silently(tmp_path):
    r = build_report("2024-06-01", clean_ledger(), {"VTI": 100.0, "JUNK": 100.0},
                     tags={"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE}, price_as_of="2024-06-01",
                     generated_at="2024-06-01T12:00:00Z")
    write_report(r, reports_dir=tmp_path)
    with pytest.raises(FileExistsError):
        write_report(r, reports_dir=tmp_path)


def test_regenerate_preserves_original(tmp_path):
    tags = {"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE}
    r1 = build_report("2024-06-01", clean_ledger(), {"VTI": 100.0, "JUNK": 100.0},
                      tags=tags, price_as_of="2024-06-01", generated_at="2024-06-01T12:00:00Z")
    write_report(r1, reports_dir=tmp_path)
    original = (tmp_path / "2024-06-01.md").read_text()

    r2 = build_report("2024-06-01", clean_ledger(), {"VTI": 105.0, "JUNK": 100.0},
                      tags=tags, price_as_of="2024-06-01", generated_at="2024-06-01T18:00:00Z")
    write_report(r2, reports_dir=tmp_path, regenerate=True)

    # a preserved copy exists with the original content
    backups = list(tmp_path.glob("2024-06-01.original-*.md"))
    assert len(backups) == 1
    assert backups[0].read_text() == original
    # the new report records the regeneration
    md = (tmp_path / "2024-06-01.md").read_text()
    assert "REGENERATED 2024-06-01T18:00:00Z" in md
    assert backups[0].name in md


def test_failed_run_does_not_overwrite_previous_day(tmp_path):
    tags = {"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE}
    write_report(build_report("2024-05-31", clean_ledger(), {"VTI": 100.0, "JUNK": 100.0},
                              tags=tags, price_as_of="2024-05-31"), reports_dir=tmp_path)
    # today's run is blocked (missing price) but must still be written as an error report
    blocked = build_report("2024-06-01", clean_ledger(), {"VTI": 100.0}, tags=tags,
                           price_as_of="2024-06-01")
    write_report(blocked, reports_dir=tmp_path)
    assert (tmp_path / "2024-05-31.md").exists()
    assert "BLOCKED" in (tmp_path / "2024-06-01.md").read_text()


# ---- finding 4: regeneration must never overwrite an existing backup ----------

def test_repeated_regeneration_never_overwrites_a_backup(tmp_path):
    """REGRESSION: two regenerations with the SAME timestamp must keep both backups."""
    tags = {"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE}
    r1 = build_report("2024-06-01", clean_ledger(), {"VTI": 100.0, "JUNK": 100.0},
                      tags=tags, price_as_of="2024-06-01", generated_at="2024-06-01T12:00:00Z")
    write_report(r1, reports_dir=tmp_path)
    first = (tmp_path / "2024-06-01.md").read_text()

    # regenerate twice with the identical generated_at -> same backup stem
    r2 = build_report("2024-06-01", clean_ledger(), {"VTI": 105.0, "JUNK": 100.0},
                      tags=tags, price_as_of="2024-06-01", generated_at="2024-06-01T12:00:00Z")
    write_report(r2, reports_dir=tmp_path, regenerate=True)
    second = (tmp_path / "2024-06-01.md").read_text()

    r3 = build_report("2024-06-01", clean_ledger(), {"VTI": 110.0, "JUNK": 100.0},
                      tags=tags, price_as_of="2024-06-01", generated_at="2024-06-01T12:00:00Z")
    write_report(r3, reports_dir=tmp_path, regenerate=True)

    backups = sorted(tmp_path.glob("2024-06-01.original-*.md"))
    assert len(backups) == 2, [b.name for b in backups]     # both preserved
    contents = {b.read_text() for b in backups}
    assert first in contents and second in contents         # neither overwritten


# ---- improvement 6: every held ticker needs its own price date ----------------

def test_missing_per_ticker_date_blocks_the_report():
    """REGRESSION: a held ticker absent from price_dates must block, not pass silently."""
    r = build_report("2024-06-01", clean_ledger(), {"VTI": 100.0, "JUNK": 100.0},
                     tags={"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE},
                     price_as_of="2024-06-01",
                     price_dates={"VTI": "2024-06-01"})       # JUNK date missing
    assert r.status == "BLOCKED"
    assert any("no price date for JUNK" in b for b in r.risk.blocked)


# ---- finding (round 3): omitting price_dates entirely must not bypass freshness ----

def test_omitted_price_dates_with_positions_blocks():
    """REGRESSION: a held position with NO per-ticker dates must block, not pass silently."""
    r = build_report("2024-06-01", clean_ledger(), {"VTI": 100.0, "JUNK": 100.0},
                     tags={"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE},
                     price_as_of="2024-06-01")          # price_dates omitted
    assert r.status == "BLOCKED"
    assert any("no per-ticker price dates" in b for b in r.risk.blocked)


def test_omitted_price_dates_with_empty_ledger_is_not_blocked_for_that_reason():
    """An empty ledger holds nothing, so the per-ticker requirement is vacuous."""
    led = Ledger.from_transactions([], starting_cash=1000.0, tags={})
    r = build_report("2024-06-01", led, {}, price_as_of="2024-06-01")
    assert not any("per-ticker price dates" in b for b in r.risk.blocked)


# ---- round 4, hardening: concurrent backups must not overwrite each other -------

def test_concurrent_backups_do_not_overwrite(tmp_path):
    """REGRESSION: two writers with the same stamp must each get a distinct backup file."""
    from apex_investor.report import _write_backup_exclusive
    a = _write_backup_exclusive(tmp_path, "2024-06-01", "2024-06-01T12:00:00Z", "AAA")
    b = _write_backup_exclusive(tmp_path, "2024-06-01", "2024-06-01T12:00:00Z", "BBB")
    assert a != b
    assert {a.read_text(), b.read_text()} == {"AAA", "BBB"}     # neither overwritten


# ---- round 5, finding 1: a missing quote must block via build_report (no post-hoc edit) ----

def test_missing_ticker_blocks_the_report_and_derives_status():
    """REGRESSION: a missing quote passed into build_report must yield a final status of BLOCKED."""
    r = build_report("2024-06-01", clean_ledger(), {"VTI": 100.0}, tags={"VTI": Tag.CORE,
                     "JUNK": Tag.SPECULATIVE}, price_as_of="2024-06-01",
                     price_dates={"VTI": "2024-06-01", "JUNK": "2024-06-01"},
                     missing_tickers=["JUNK"])
    assert r.status == "BLOCKED"                      # derived from the final risk state
    assert r.risk.trustworthy is False
    assert any("no fresh quote for JUNK" in b for b in r.risk.blocked)


# ---- round 5, finding 3: a config problem must fail closed, not be ignored ----

def test_config_problem_blocks_the_report():
    """REGRESSION: a present-but-unusable config file must not pass as 'no actions'."""
    r = build_report("2024-06-01", clean_ledger(), {"VTI": 100.0, "JUNK": 100.0},
                     tags={"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE}, price_as_of="2024-06-01",
                     price_dates={"VTI": "2024-06-01", "JUNK": "2024-06-01"},
                     config_problems=["known actions file known_actions.json is unreadable"])
    assert r.status == "BLOCKED"
    assert r.risk.trustworthy is False
    assert any("config:" in b for b in r.risk.blocked)
    md = r.to_markdown()
    assert "config problem" in md                              # section 4 evidence
    assert "config:" in md                                     # section 5 blocked list
    assert "no clean verdict" in md                            # section 6 summary


def test_no_config_problem_does_not_block():
    r = build_report("2024-06-01", clean_ledger(), {"VTI": 100.0, "JUNK": 100.0},
                     tags={"VTI": Tag.CORE, "JUNK": Tag.SPECULATIVE}, price_as_of="2024-06-01",
                     price_dates={"VTI": "2024-06-01", "JUNK": "2024-06-01"})
    assert r.status == "OK"
    assert r.config_problems == []
