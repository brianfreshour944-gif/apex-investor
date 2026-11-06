"""Tests for the equity-history helpers in scripts/daily_report.py (F3).

These cover the operator-review requirements: loading (missing / malformed / non-list files fail
closed to empty), prior-date selection for the "Daily change" calculation, and same-date
replacement so re-runs refine rather than duplicate history entries.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import daily_report  # noqa: E402


# --- load_equity_history -----------------------------------------------------

def test_load_missing_file_returns_empty(tmp_path):
    assert daily_report.load_equity_history(tmp_path / "absent.json") == []


def test_load_malformed_json_fails_closed(tmp_path):
    f = tmp_path / "equity_history.json"
    f.write_text("{not json")
    assert daily_report.load_equity_history(f) == []


def test_load_non_list_json_fails_closed(tmp_path):
    f = tmp_path / "equity_history.json"
    f.write_text(json.dumps({"date": "2026-01-01", "equity": 1.0}))
    assert daily_report.load_equity_history(f) == []


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


def test_append_drops_malformed_existing_entries(tmp_path):
    f = tmp_path / "equity_history.json"
    f.write_text(json.dumps(["garbage", {"date": "2026-01-01", "equity": 100.0}]))
    daily_report.append_equity_history(f, "2026-01-02", 110.0)
    data = json.loads(f.read_text())
    assert data == [
        {"date": "2026-01-01", "equity": 100.0},
        {"date": "2026-01-02", "equity": 110.0},
    ]
