"""Price-sanity guard tests. No network — pure logic on Bar-like inputs.

Design intent under test: FLAG suspicious moves for verification, do not blindly reject.
Only an EXTREME move with NO explanation blocks valuation and risk.
"""
from __future__ import annotations

from apex_investor.ledger import Ledger, Side, Tag, Transaction
from apex_investor.prices import Bar
from apex_investor.report import build_report
from apex_investor.sanity import PriceSanityLimits, QuoteCheck, check_quote, worst_status

LIM = PriceSanityLimits()


def bar(d, c):
    return Bar(d, c, c, 1000.0)


# ---------- normal and explained moves ----------------------------------------

def test_normal_move_is_ok():
    c = check_quote("X", bar("2026-11-05", 101.0), bar("2026-11-04", 100.0), limits=LIM)
    assert c.status == "OK" and not c.flagged


def test_earnings_gap_is_suspect_not_blocked():
    # +40% one-day jump: large but plausible -> flagged for verification, NOT blocked
    c = check_quote("X", bar("2026-11-05", 140.0), bar("2026-11-04", 100.0), limits=LIM)
    assert c.status == "SUSPECT"
    assert c.flagged and not c.blocks
    assert "verify" in c.reason


def test_move_explained_by_known_action_is_ok():
    # declared ratio must AGREE with the observed factor (1000/100 = 10) to explain the move
    acts = [{"ticker": "X", "date": "2026-11-05", "ratio": 10.0, "label": "1:10 reverse split"}]
    c = check_quote("X", bar("2026-11-05", 1000.0), bar("2026-11-04", 100.0),
                    limits=LIM, known_actions=acts)
    assert c.status == "OK"
    assert c.corporate_action and "reverse split" in c.corporate_action


def test_known_action_ratio_that_does_not_match_does_not_explain():
    """REGRESSION: a declared action whose ratio disagrees with the move must NOT approve it."""
    # stated ratio 0.1, but observed factor is 1000/100 = 10 -> the action does not explain it
    acts = [{"ticker": "X", "date": "2026-11-05", "ratio": 0.1, "label": "1:10 reverse split"}]
    c = check_quote("X", bar("2026-11-05", 1000.0), bar("2026-11-04", 100.0),
                    limits=LIM, known_actions=acts)
    assert c.status != "OK"
    assert "does not match" in c.reason


def test_known_action_ratio_near_factor_within_tolerance_is_ok():
    # factor 8.0 vs declared 7.8 -> within 6% tolerance -> explains the move
    acts = [{"ticker": "X", "date": "2026-11-05", "ratio": 7.8}]
    c = check_quote("X", bar("2026-11-05", 800.0), bar("2026-11-04", 100.0),
                    limits=LIM, known_actions=acts)
    assert c.status == "OK"


def test_known_action_missing_ratio_cannot_explain():
    # an action with no usable ratio cannot approve a large move
    acts = [{"ticker": "X", "date": "2026-11-05", "label": "1:10 reverse split"}]
    c = check_quote("X", bar("2026-11-05", 1000.0), bar("2026-11-04", 100.0),
                    limits=LIM, known_actions=acts)
    assert c.status != "OK"


def test_malformed_known_action_entries_are_ignored():
    # non-dict, bad date, zero/negative/NaN ratio must not crash or approve
    acts = ["nonsense", {"ticker": "X", "date": "not-a-date", "ratio": 10.0},
            {"ticker": "X", "date": "2026-11-05", "ratio": 0.0},
            {"ticker": "X", "date": "2026-11-05", "ratio": -10.0},
            {"ticker": "X", "date": "2026-11-05", "ratio": float("nan")}]
    c = check_quote("X", bar("2026-11-05", 1000.0), bar("2026-11-04", 100.0),
                    limits=LIM, known_actions=acts)
    assert c.status != "OK"


def test_known_action_for_other_ticker_does_not_apply():
    acts = [{"ticker": "Y", "date": "2026-11-05", "label": "1:10 reverse split"}]
    c = check_quote("X", bar("2026-11-05", 1000.0), bar("2026-11-04", 100.0),
                    limits=LIM, known_actions=acts)
    assert c.status != "OK"          # X is not explained by Y's action


# ---------- splits: flag for verification, do not block ------------------------

def test_forward_split_factor_is_suspect():
    # 4:1 split -> factor 0.25; extreme move but consistent with a split -> verify, not block
    c = check_quote("X", bar("2026-11-05", 25.0), bar("2026-11-04", 100.0), limits=LIM)
    assert c.status == "SUSPECT" and c.corporate_action


def test_reverse_split_factor_is_suspect():
    # 1:8 reverse split -> factor 8.0
    c = check_quote("X", bar("2026-11-05", 800.0), bar("2026-11-04", 100.0), limits=LIM)
    assert c.status == "SUSPECT"
    assert c.corporate_action and "reverse split" in c.corporate_action


# ---------- unvalidatable quotes block ----------------------------------------

def test_extreme_unexplained_move_is_blocked():
    # -91% (factor 0.09): not a common split ratio, no known action -> BLOCKED
    c = check_quote("X", bar("2026-11-05", 9.0), bar("2026-11-04", 100.0), limits=LIM)
    assert c.status == "BLOCKED" and c.blocks
    assert "cannot validate" in c.reason


def test_no_previous_close_is_unverifiable_not_blocked():
    c = check_quote("X", bar("2026-11-05", 100.0), None, limits=LIM)
    assert c.status == "UNVERIFIABLE" and not c.blocks


def test_non_positive_previous_close_is_unverifiable():
    # a zero/negative previous close cannot be compared against -> UNVERIFIABLE (not valued as OK)
    c = check_quote("X", bar("2026-11-05", 100.0), bar("2026-11-04", 0.0), limits=LIM)
    assert c.status == "UNVERIFIABLE"
    assert not c.blocks


def test_negative_previous_close_is_unverifiable():
    c = check_quote("X", bar("2026-11-05", 100.0), bar("2026-11-04", -5.0), limits=LIM)
    assert c.status == "UNVERIFIABLE"


# ---------- round 5, finding 2: validate the raw prices themselves --------------

def test_zero_latest_close_is_blocked():
    c = check_quote("X", bar("2026-11-05", 0.0), bar("2026-11-04", 100.0), limits=LIM)
    assert c.status == "BLOCKED" and c.blocks
    assert "invalid latest price" in c.reason


def test_negative_latest_close_is_blocked():
    c = check_quote("X", bar("2026-11-05", -5.0), bar("2026-11-04", 100.0), limits=LIM)
    assert c.status == "BLOCKED"


def test_nan_latest_close_is_blocked():
    c = check_quote("X", bar("2026-11-05", float("nan")), bar("2026-11-04", 100.0), limits=LIM)
    assert c.status == "BLOCKED"


def test_infinite_latest_close_is_blocked():
    for bad in (float("inf"), float("-inf")):
        c = check_quote("X", bar("2026-11-05", bad), bar("2026-11-04", 100.0), limits=LIM)
        assert c.status == "BLOCKED", bad


def test_non_numeric_latest_close_is_blocked():
    for bad in ("100", True, None):
        c = check_quote("X", bar("2026-11-05", bad), bar("2026-11-04", 100.0), limits=LIM)
        assert c.status == "BLOCKED", bad


def test_nan_previous_close_is_unverifiable():
    c = check_quote("X", bar("2026-11-05", 100.0), bar("2026-11-04", float("nan")), limits=LIM)
    assert c.status == "UNVERIFIABLE"


def test_infinite_previous_close_is_unverifiable():
    c = check_quote("X", bar("2026-11-05", 100.0), bar("2026-11-04", float("inf")), limits=LIM)
    assert c.status == "UNVERIFIABLE"


def test_non_numeric_previous_close_is_unverifiable():
    for bad in ("100", True, None):
        c = check_quote("X", bar("2026-11-05", 100.0), bar("2026-11-04", bad), limits=LIM)
        assert c.status == "UNVERIFIABLE", bad


def test_invalid_latest_price_never_reaches_valuation():
    c = check_quote("X", bar("2026-11-05", 0.0), bar("2026-11-04", 100.0), limits=LIM)
    r = build_report("2026-11-05", _ledger(), {"X": 0.0}, tags={"X": Tag.GROWTH},
                     price_as_of="2026-11-05", price_dates={"X": "2026-11-05"}, quote_checks=[c])
    assert r.status == "BLOCKED"
    assert r.holdings[0]["price"] is None       # malformed price NOT used for valuation


def test_missing_latest_is_unverifiable():
    c = check_quote("X", None, bar("2026-11-04", 100.0), limits=LIM)
    assert c.status == "UNVERIFIABLE"


def test_thresholds_are_configurable():
    tight = PriceSanityLimits(warn_move=0.05, block_move=0.20)
    c = check_quote("X", bar("2026-11-05", 110.0), bar("2026-11-04", 100.0), limits=tight)
    assert c.status == "SUSPECT"           # +10% warns under a tight threshold
    assert c.evidence["limits"]["warn_move"] == 0.05


def test_worst_status_ordering():
    ok = QuoteCheck("A", "d", 1.0, None, None, None, "OK", "")
    sus = QuoteCheck("B", "d", 1.0, None, None, None, "SUSPECT", "")
    blk = QuoteCheck("C", "d", 1.0, None, None, None, "BLOCKED", "")
    assert worst_status([ok, sus]) == "SUSPECT"
    assert worst_status([ok, sus, blk]) == "BLOCKED"
    assert worst_status([]) == "OK"


# ---------- integration: the guard gates valuation and risk --------------------

def _ledger():
    return Ledger.from_transactions(
        [Transaction("2026-01-02", "X", Side.BUY, 10, 100.0)],
        starting_cash=1000.0, tags={"X": Tag.GROWTH})


def test_blocked_quote_blocks_report_and_drops_from_valuation():
    c = check_quote("X", bar("2026-11-05", 9.0), bar("2026-11-04", 100.0), limits=LIM)
    r = build_report("2026-11-05", _ledger(), {"X": 9.0}, tags={"X": Tag.GROWTH},
                     price_as_of="2026-11-05", price_dates={"X": "2026-11-05"}, quote_checks=[c])
    assert r.status == "BLOCKED"
    assert r.risk.trustworthy is False
    assert any("price_sanity[X]" in b for b in r.risk.blocked)
    assert r.holdings[0]["price"] is None          # unvalidated price NOT used for valuation
    assert "price_sanity" in r.to_markdown()


def test_suspect_quote_is_flagged_but_report_proceeds():
    c = check_quote("X", bar("2026-11-05", 140.0), bar("2026-11-04", 100.0), limits=LIM)
    r = build_report("2026-11-05", _ledger(), {"X": 140.0}, tags={"X": Tag.GROWTH},
                     price_as_of="2026-11-05", price_dates={"X": "2026-11-05"}, quote_checks=[c])
    assert r.status != "BLOCKED"
    assert r.holdings[0]["price"] == 140.0          # plausible price still valued
    assert any(a.kind == "price_sanity" for a in r.alerts)
    md = r.to_markdown()
    assert "Preserved price-sanity checks" in md and "140.0" in md


def test_sanity_evidence_is_preserved_in_report():
    c = check_quote("X", bar("2026-11-05", 140.0), bar("2026-11-04", 100.0), limits=LIM)
    r = build_report("2026-11-05", _ledger(), {"X": 140.0}, tags={"X": Tag.GROWTH},
                     price_as_of="2026-11-05", price_dates={"X": "2026-11-05"}, quote_checks=[c])
    assert r.quote_checks[0].status == "SUSPECT"
    assert r.quote_checks[0].evidence["previous"] == 100.0


def test_suspect_quote_does_not_read_as_ok_or_critical():
    """A SUSPECT price must not produce a clean or alarming verdict that looks authoritative."""
    c = check_quote("X", bar("2026-11-05", 140.0), bar("2026-11-04", 100.0), limits=LIM)
    r = build_report("2026-11-05", _ledger(), {"X": 140.0}, tags={"X": Tag.GROWTH},
                     price_as_of="2026-11-05", price_dates={"X": "2026-11-05"}, quote_checks=[c])
    assert r.status == "WARN"                    # not OK, not CRITICAL
    assert r.unverified_tickers == ["X"]


def test_suspect_quote_marks_valuation_unverified_in_markdown():
    c = check_quote("X", bar("2026-11-05", 140.0), bar("2026-11-04", 100.0), limits=LIM)
    r = build_report("2026-11-05", _ledger(), {"X": 140.0}, tags={"X": Tag.GROWTH},
                     price_as_of="2026-11-05", price_dates={"X": "2026-11-05"}, quote_checks=[c])
    md = r.to_markdown()
    assert "Unverified price(s): X" in md          # section 1
    assert "(unverified)" in md                     # per-row marker
    assert "Valuation is UNVERIFIED for X" in md    # section 6 summary


def test_no_previous_close_is_noted_and_not_silently_ok():
    c = check_quote("X", bar("2026-11-05", 100.0), None, limits=LIM)
    r = build_report("2026-11-05", _ledger(), {"X": 100.0}, tags={"X": Tag.GROWTH},
                     price_as_of="2026-11-05", price_dates={"X": "2026-11-05"}, quote_checks=[c])
    assert r.status != "BLOCKED"
    assert r.unchecked_tickers == ["X"]
    assert "could not be price-checked" in r.to_markdown()


# ---------- finding 2: UNVERIFIABLE must also degrade the report ----------------

def test_unverifiable_quote_degrades_report_to_warn():
    """REGRESSION: a missing previous close must not leave an otherwise-clean report at OK."""
    c = check_quote("X", bar("2026-11-05", 100.0), None, limits=LIM)
    r = build_report("2026-11-05", _ledger(), {"X": 100.0}, tags={"X": Tag.GROWTH},
                     price_as_of="2026-11-05", price_dates={"X": "2026-11-05"}, quote_checks=[c])
    assert c.status == "UNVERIFIABLE"
    assert r.status == "WARN"                     # NOT OK
    assert r.risk.trustworthy is True             # still a real assessment, just not clean
    assert r.unchecked_tickers == ["X"]
    assert r.unverified_tickers == []             # distinction preserved: not SUSPECT


# ---------- finding 1 (round 3): direction + unrelated-move guards --------------

def test_reverse_split_ratio_must_be_greater_than_one():
    # 1:8 reverse split makes the price RISE -> factor 8.0; ratio 8.0 explains it
    ok = check_quote("X", bar("2026-11-05", 800.0), bar("2026-11-04", 100.0), limits=LIM,
                     known_actions=[{"ticker": "X", "date": "2026-11-05", "ratio": 8.0}])
    assert ok.status == "OK"
    # the same event declared with the INVERSE ratio must NOT explain it
    wrong = check_quote("X", bar("2026-11-05", 800.0), bar("2026-11-04", 100.0), limits=LIM,
                        known_actions=[{"ticker": "X", "date": "2026-11-05", "ratio": 0.125}])
    assert wrong.status != "OK"


def test_forward_split_ratio_must_be_less_than_one():
    # 4:1 forward split makes the price FALL -> factor 0.25; ratio 0.25 explains it
    ok = check_quote("X", bar("2026-11-05", 25.0), bar("2026-11-04", 100.0), limits=LIM,
                     known_actions=[{"ticker": "X", "date": "2026-11-05", "ratio": 0.25}])
    assert ok.status == "OK"
    wrong = check_quote("X", bar("2026-11-05", 25.0), bar("2026-11-04", 100.0), limits=LIM,
                        known_actions=[{"ticker": "X", "date": "2026-11-05", "ratio": 4.0}])
    assert wrong.status != "OK"


def test_action_ratio_far_from_factor_does_not_explain():
    # a declared 2:1 split cannot explain a +900% move (factor 10); the mismatch leaves it flagged
    c = check_quote("X", bar("2026-11-05", 1000.0), bar("2026-11-04", 100.0), limits=LIM,
                    known_actions=[{"ticker": "X", "date": "2026-11-05", "ratio": 2.0}])
    assert c.status != "OK"
    assert "does not match" in c.reason


def test_action_ratio_outside_tolerance_does_not_explain():
    # factor 1.5 vs declared 2.0 -> 25% off, well outside the 6% tolerance
    c = check_quote("X", bar("2026-11-05", 150.0), bar("2026-11-04", 100.0), limits=LIM,
                    known_actions=[{"ticker": "X", "date": "2026-11-05", "ratio": 2.0}])
    assert c.status != "OK"


# ---------- round 4, finding 1: an action with no ticker must not apply ----------

def test_action_without_ticker_does_not_explain_any_holding():
    """REGRESSION: a missing ticker must not authorize an unexplained move for any holding."""
    acts = [{"date": "2026-11-05", "ratio": 10.0, "label": "1:10 reverse split"}]  # no ticker
    c = check_quote("X", bar("2026-11-05", 1000.0), bar("2026-11-04", 100.0),
                    limits=LIM, known_actions=acts)
    assert c.status != "OK"
    assert c.status == "SUSPECT"          # consistent with a split, but not approved


def test_action_for_other_ticker_still_does_not_apply():
    acts = [{"ticker": "Y", "date": "2026-11-05", "ratio": 10.0}]
    c = check_quote("X", bar("2026-11-05", 1000.0), bar("2026-11-04", 100.0),
                    limits=LIM, known_actions=acts)
    assert c.status != "OK"


# ---------- round 4, finding 2: invalid PriceSanityLimits must fail closed ----------

def test_string_warn_move_blocks_instead_of_raising():
    bad = PriceSanityLimits(warn_move="oops")  # type: ignore[arg-type]
    c = check_quote("X", bar("2026-11-05", 101.0), bar("2026-11-04", 100.0), limits=bad)
    assert c.status == "BLOCKED"
    assert "invalid price-sanity limits" in c.reason


def test_nan_block_move_blocks():
    bad = PriceSanityLimits(block_move=float("nan"))
    c = check_quote("X", bar("2026-11-05", 101.0), bar("2026-11-04", 100.0), limits=bad)
    assert c.status == "BLOCKED"


def test_out_of_range_sanity_limit_blocks():
    bad = PriceSanityLimits(warn_move=1.5)
    c = check_quote("X", bar("2026-11-05", 101.0), bar("2026-11-04", 100.0), limits=bad)
    assert c.status == "BLOCKED"
    assert "out of range" in c.reason


def test_inverted_sanity_thresholds_block():
    bad = PriceSanityLimits(warn_move=0.9, block_move=0.3)   # warn above block
    c = check_quote("X", bar("2026-11-05", 101.0), bar("2026-11-04", 100.0), limits=bad)
    assert c.status == "BLOCKED"
    assert "warn_move must be below block_move" in c.reason


def test_valid_limits_are_unaffected():
    c = check_quote("X", bar("2026-11-05", 101.0), bar("2026-11-04", 100.0), limits=LIM)
    assert c.status == "OK"


def test_unchecked_ticker_discloses_daily_comparison_unavailable():
    """A valid latest price with no usable previous close: valuation proceeds, but the report
    must state plainly that the daily comparison is unavailable."""
    c = check_quote("X", bar("2026-11-05", 100.0), bar("2026-11-04", float("nan")), limits=LIM)
    r = build_report("2026-11-05", _ledger(), {"X": 100.0}, tags={"X": Tag.GROWTH},
                     price_as_of="2026-11-05", price_dates={"X": "2026-11-05"}, quote_checks=[c])
    md = r.to_markdown()
    assert c.status == "UNVERIFIABLE"
    assert "Unchecked price(s): X" in md
    assert "daily comparison for these is UNAVAILABLE" in md
    assert r.holdings[0]["price"] == 100.0      # the valid latest price still values the holding
