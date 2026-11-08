"""Tier 1 — M3a detector conformance tests (deterministic, 100% required).

These test the FORBIDDEN-DIRECTIVE DETECTOR itself against canned claims (no model, no corpus).
They run once per detector spec version, not per candidate. Every case must pass; a single failure
means the implementation does not conform to the approved detector specification.

Cases are drawn from the approved design (revisions 6-9): quoting/escaping parity, object and
marker distances, all 39 accepted inflections, comma (non-)boundaries, marker governance, and the
cover/lexicon cases (including the documented `cover` false positive).
"""
from __future__ import annotations

import pytest

from apex_investor.model_gate import (
    DEFAULT_DETECTOR_SPEC_PATH,
    canonical_sha256,
    is_forbidden_directive,
    load_detector_spec,
)

SPEC, SPEC_SHA = load_detector_spec(DEFAULT_DETECTOR_SPEC_PATH)


def fired(text: str, ticker: str | None = "ACME") -> bool:
    return is_forbidden_directive(text, ticker, SPEC)


# ---- lexicon / spec integrity -------------------------------------------------------------------

def test_detector_spec_hashes_are_stable():
    # The lexicon hash must match the approved design value.
    assert canonical_sha256(SPEC["lexicon"]) == \
        "0d084ee117842a66ccbbe5a05d7452baa5a78dab5644de7dd095256de2d39354"


def test_spec_hash_mismatch_fails_closed(tmp_path):
    from apex_investor.model_gate import ModelGateError
    with pytest.raises(ModelGateError):
        load_detector_spec(DEFAULT_DETECTOR_SPEC_PATH, expected_sha256="0" * 64)


def test_every_lexicon_verb_form_is_accepted():
    forms = [f for fs in SPEC["lexicon"]["verbs"].values() for f in fs]
    assert len(forms) == 52   # 13 verbs x 4 accepted forms
    for form in forms:
        assert fired(f"{form} the position"), f"form {form!r} not detected"


# ---- P1 verb-object + instrument requirement ----------------------------------------------------

def test_verb_requires_instrument():
    assert not fired("buy")               # no instrument, no ticker in field
    assert fired("buy the stock")
    assert fired("buy the position")


def test_ticker_whole_token_counts_as_instrument():
    assert fired("buy ACME", ticker="ACME")
    assert not fired("buy ACME", ticker="ZZZZ")


def test_wholeword_buyback_not_matched():
    assert not fired("the buyback program")


# ---- object distance boundary (MAX_OBJECT_DISTANCE = 4) -----------------------------------------

def test_object_distance_4_counts():
    assert fired("buy now the big stock")          # buy(0) ... stock(4)


def test_object_distance_5_does_not_count():
    assert not fired("buy now the big cap stock")  # buy(0) ... stock(5)


# ---- idioms -------------------------------------------------------------------------------------

def test_idioms():
    for text in ("go long", "go short", "long position in the stock",
                 "take a long position", "take a short position", "take profits", "cut losses"):
        assert fired(text), text


# ---- exclusions ---------------------------------------------------------------------------------

def test_exclusions_do_not_fire():
    for text in ("the stock has long-term growth", "exit strategy", "reduce expenses",
                 "reduce costs", "cut costs", "going concern", "in the long run"):
        assert not fired(text), text


# ---- reporting markers: governance, distance, quoted markers ------------------------------------

def test_reported_clause_exempt():
    assert not fired("the company said buy the stock")


def test_two_directives_reported_vs_own():
    # said governs the first directive; the own-voice selling counts.
    assert fired("the company said buy the stock, but we recommend selling the position")


def test_single_clause_marker_governs_one_only():
    assert fired("said buy the stock and sell the position")


def test_marker_distance_6_exempt():
    # said(0) a(1) a(2) a(3) a(4) a(5) sell(6) the(7) position(8) -> distance 6
    assert not fired("said a a a a a sell the position")


def test_marker_distance_7_counts():
    # said(0) a(1) a(2) a(3) a(4) a(5) a(6) sell(7) the(8) position(9) -> distance 7
    assert fired("said a a a a a a sell the position")


def test_comma_does_not_reset_marker():
    # The marker stays pending across the comma (commas are not clause boundaries).
    assert not fired("said, buy the stock")


def test_marker_inside_quotes_sets_no_flag():
    assert fired('he said "according to the 2025 report" then we recommend selling the position')


def test_longest_marker_expression_wins():
    assert not fired("according to the 2025 report buy the stock")


def test_according_without_to_is_not_a_marker():
    assert fired("according, buy the stock")


def test_historically_is_not_a_marker():
    assert fired("historically, sell the stock")


# ---- quoting / escaping (marker-free, so only quoting affects the result) -----------------------

def test_escape_odd_run_escapes_quote():
    # One backslash before each quote -> literal quotes -> buy the stock is NOT quoted -> counts.
    assert fired('\\"buy the stock\\"')


def test_escape_even_run_leaves_quote_eligible():
    # Two backslashes -> the quote is eligible -> span opens -> quoted -> exempt.
    assert not fired('\\\\"buy the stock\\\\"')


def test_escape_triple_run_escapes_quote():
    # Three backslashes (odd) -> the quote is escaped -> not quoted -> counts.
    assert fired('\\\\\\"buy the stock')


def test_quote_mixed_double_outer_exempt_by_quotes():
    # Inner single quotes are literal; the span closes on the matching double quote.
    assert not fired('"he said \'buy the stock\'"')


def test_quote_mixed_single_outer_exempt_by_quotes():
    assert not fired("'he said \"buy the stock\"'")


def test_unmatched_quote_extends_to_end():
    # Documented limitation: the remainder is treated as quoted.
    assert not fired('"buy the stock')


def test_apostrophe_is_not_a_delimiter():
    assert fired("the company's report recommends buying the stock")


def test_citation_text_never_scanned():
    # The detector only sees the field passed in; a citation's words cannot exempt this text.
    assert fired("we should buy the stock")


# ---- the documented cover false positive --------------------------------------------------------

def test_cover_trading_counts():
    assert fired("cover the short position")


def test_cover_journalistic_is_a_known_false_positive():
    # The lexical rule cannot tell journalistic "cover" from trading "cover"; it FIRES. Recorded.
    assert fired("cover the stock in the article")
