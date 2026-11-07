"""Tests for the equity-history helpers in scripts/daily_report.py (F3 + hardening).

These cover the operator-review requirements: loading (missing -> empty; corrupt/non-list ->
EquityHistoryError so the file is preserved), prior-date selection for the "Daily change"
calculation, value/date validation, refusal to rewrite a file containing malformed records,
atomic writes, and same-date replacement so re-runs refine rather than duplicate history.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import daily_report  # noqa: E402

# --- load_equity_history -----------------------------------------------------

def test_load_missing_file_returns_empty(tmp_path):
    assert daily_report.load_equity_history(tmp_path / "absent.json") == []


def test_load_malformed_json_raises_and_preserves(tmp_path):
    f = tmp_path / "equity_history.json"
    f.write_text("{not json")
    with pytest.raises(daily_report.EquityHistoryError):
        daily_report.load_equity_history(f)
    assert f.read_text() == "{not json"          # file preserved, not emptied


def test_load_non_list_json_raises(tmp_path):
    f = tmp_path / "equity_history.json"
    f.write_text(json.dumps({"date": "2026-01-01", "equity": 1.0}))
    with pytest.raises(daily_report.EquityHistoryError):
        daily_report.load_equity_history(f)


def test_load_valid_list_round_trips(tmp_path):
    f = tmp_path / "equity_history.json"
    entries = [{"date": "2026-01-01", "equity": 100.0}]
    f.write_text(json.dumps(entries))
    assert daily_report.load_equity_history(f) == entries


# --- previous_equity_from_history --------------------------------------------

def test_previous_equity_empty_history_is_none():
    assert daily_report.previous_equity_from_history([], "2026-01-02") is None


def test_previous_equity_selects_latest_prior_date():
    history = [
        {"date": "2026-01-01", "equity": 100.0},
        {"date": "2026-01-02", "equity": 110.0},
        {"date": "2026-01-03", "equity": 120.0},
    ]
    assert daily_report.previous_equity_from_history(history, "2026-01-04") == 120.0


def test_previous_equity_ignores_same_and_future_dates():
    history = [
        {"date": "2026-01-03", "equity": 120.0},
        {"date": "2026-01-04", "equity": 130.0},
    ]
    # report for 2026-01-04 must compare against the 01-03 close, never itself
    assert daily_report.previous_equity_from_history(history, "2026-01-04") == 120.0
    assert daily_report.previous_equity_from_history(history, "2026-01-02") is None


def test_previous_equity_skips_malformed_entries():
    """Read-only path: malformed entries are skipped, not fatal."""
    history = [
        "garbage",
        {"date": "2026-01-01", "equity": "not-a-number"},
        {"date": "2026-01-02", "equity": 110.0},
    ]
    assert daily_report.previous_equity_from_history(history, "2026-01-03") == 110.0


# --- append_equity_history ---------------------------------------------------

def test_append_creates_sorted_history(tmp_path):
    f = tmp_path / "equity_history.json"
    daily_report.append_equity_history(f, "2026-01-02", 110.0)
    daily_report.append_equity_history(f, "2026-01-01", 100.0)
    data = json.loads(f.read_text())
    assert data == [
        {"date": "2026-01-01", "equity": 100.0},
        {"date": "2026-01-02", "equity": 110.0},
    ]


def test_append_same_date_replaces_never_duplicates(tmp_path):
    f = tmp_path / "equity_history.json"
    daily_report.append_equity_history(f, "2026-01-01", 100.0)
    daily_report.append_equity_history(f, "2026-01-01", 101.5)
    data = json.loads(f.read_text())
    assert len(data) == 1
    assert data[0] == {"date": "2026-01-01", "equity": 101.5}


def test_append_refuses_corrupt_history_and_preserves_it(tmp_path):
    f = tmp_path / "equity_history.json"
    f.write_text('[{"date": "2026-01-01", "equity": 100.0}')     # truncated
    before = f.read_text()
    with pytest.raises(daily_report.EquityHistoryError):
        daily_report.append_equity_history(f, "2026-01-02", 110.0)
    assert f.read_text() == before                # corrupt file NOT overwritten


@pytest.mark.parametrize("bad_entry", [
    "garbage",                                   # non-dict entry
    {"date": "01/01/2026", "equity": 100.0},     # malformed date
    {"date": "2026-01-01", "equity": "nope"},    # malformed equity
    {"date": "2026-01-01", "equity": -5.0},      # negative equity
    {"date": "2026-01-01", "equity": True},      # bool equity
    {"date": "2026-01-01"},                      # missing equity
])
def test_append_refuses_malformed_entry_and_preserves_file(tmp_path, bad_entry):
    """A parseable list containing a malformed record must NOT be silently rewritten."""
    f = tmp_path / "equity_history.json"
    f.write_text(json.dumps([{"date": "2026-01-01", "equity": 100.0}, bad_entry]))
    before = f.read_text()
    with pytest.raises(daily_report.EquityHistoryError):
        daily_report.append_equity_history(f, "2026-01-02", 110.0)
    assert f.read_text() == before                # byte-for-byte preserved


def test_append_valid_history_still_writes(tmp_path):
    """Sanity: a fully well-formed file is still updated normally."""
    f = tmp_path / "equity_history.json"
    f.write_text(json.dumps([{"date": "2026-01-01", "equity": 100.0}]))
    daily_report.append_equity_history(f, "2026-01-02", 110.0)
    assert json.loads(f.read_text()) == [
        {"date": "2026-01-01", "equity": 100.0},
        {"date": "2026-01-02", "equity": 110.0},
    ]


# --- hardening: numeric / date validation ------------------------------------

@pytest.mark.parametrize("bad", [True, False, float("nan"), float("inf"),
                                 float("-inf"), -1.0, "100", None])
def test_previous_equity_rejects_invalid_values(bad):
    history = [{"date": "2026-01-01", "equity": bad}]
    assert daily_report.previous_equity_from_history(history, "2026-01-02") is None


def test_previous_equity_rejects_malformed_date():
    history = [{"date": "01/01/2026", "equity": 100.0}]
    assert daily_report.previous_equity_from_history(history, "2026-01-02") is None


@pytest.mark.parametrize("bad", ["20260102", "2026-W01-5", "2026-1-2", "2026/01/02"])
def test_previous_equity_rejects_non_canonical_dates(bad):
    """fromisoformat accepts compact/week forms; only exact YYYY-MM-DD is allowed."""
    history = [{"date": bad, "equity": 100.0}]
    assert daily_report.previous_equity_from_history(history, "2026-01-31") is None


def test_previous_equity_accepts_zero():
    history = [{"date": "2026-01-01", "equity": 0.0}]
    assert daily_report.previous_equity_from_history(history, "2026-01-02") == 0.0


@pytest.mark.parametrize("bad_date", ["", "01/01/2026", "2026-13-01", "2026-02-30", None,
                                      "20260102", "2026-W01-5", "2026-1-2"])
def test_append_rejects_invalid_report_date(tmp_path, bad_date):
    with pytest.raises(ValueError):
        daily_report.append_equity_history(tmp_path / "h.json", bad_date, 100.0)


@pytest.mark.parametrize("bad_equity", [True, float("nan"), float("inf"), -1.0, "100", None])
def test_append_rejects_invalid_equity(tmp_path, bad_equity):
    with pytest.raises(ValueError):
        daily_report.append_equity_history(tmp_path / "h.json", "2026-01-01", bad_equity)


# --- hardening: atomic write -------------------------------------------------

def test_append_is_atomic_leaves_no_temp_files(tmp_path):
    f = tmp_path / "equity_history.json"
    daily_report.append_equity_history(f, "2026-01-01", 100.0)
    daily_report.append_equity_history(f, "2026-01-02", 110.0)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "equity_history.json"]
    assert leftovers == []                          # temp file was replaced, not left behind
    assert len(json.loads(f.read_text())) == 2


def test_append_write_failure_preserves_original(tmp_path, monkeypatch):
    f = tmp_path / "equity_history.json"
    daily_report.append_equity_history(f, "2026-01-01", 100.0)
    before = f.read_text()

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError):
        daily_report.append_equity_history(f, "2026-01-02", 110.0)
    assert f.read_text() == before                  # original intact, not truncated
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "equity_history.json"]
    assert leftovers == []                          # temp file cleaned up on failure


# --- hardening: main() must not mask unrelated errors ------------------------

def _minimal_main(tmp_path, monkeypatch, *, history_text=None):
    """Wire main() to run offline against a temp portfolio, returning the history path."""
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text(json.dumps({"starting_cash": 1000.0, "holdings": []}))
    hp = tmp_path / "equity_history.json"
    if history_text is not None:
        hp.write_text(history_text)
    monkeypatch.setattr(daily_report, "LIVE_PORTFOLIO", portfolio)
    monkeypatch.setattr(daily_report, "EQUITY_HISTORY_LIVE", hp)
    monkeypatch.setattr(daily_report, "gather_prices",
                        lambda tickers, *, offline: ({}, {}, [], {}, {}))
    monkeypatch.setattr(daily_report, "write_report",
                        lambda report, **k: tmp_path / "out.md")
    monkeypatch.setattr(daily_report, "load_known_actions", lambda p: ([], []))
    monkeypatch.setattr(daily_report, "build_report",
                        lambda *a, **k: type("R", (), {
                            "equity": 22504.25, "preserved_original": None,
                            "to_console": lambda self: "", "date": "2026-01-02"})())
    monkeypatch.setattr(sys, "argv", ["daily_report.py", "--date", "2026-01-02"])
    return hp


def test_main_continues_and_warns_on_corrupt_history(tmp_path, monkeypatch, capsys):
    """A corrupt history file must not fail the run, and must be preserved."""
    hp = _minimal_main(tmp_path, monkeypatch, history_text="{not json")
    assert daily_report.main() == 0
    err = capsys.readouterr().err
    assert "corrupt equity history" in err and "preserved" in err
    assert hp.read_text() == "{not json"           # untouched


def test_main_continues_on_oserror_writing_history(tmp_path, monkeypatch, capsys):
    """A filesystem failure updating history warns but does not fail the report."""
    _minimal_main(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(daily_report, "append_equity_history", boom)
    assert daily_report.main() == 0
    assert "could not update equity history" in capsys.readouterr().err


@pytest.mark.parametrize("exc", [KeyError("bug"), TypeError("bug"), AttributeError("bug"),
                                 ValueError("bug")])
def test_main_propagates_unrelated_append_errors(tmp_path, monkeypatch, exc):
    """Only EquityHistoryError/OSError are handled; any other exception must propagate."""
    _minimal_main(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(daily_report, "append_equity_history", boom)
    with pytest.raises(type(exc)):
        daily_report.main()
