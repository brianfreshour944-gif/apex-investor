"""M2 tests: price parsing and split/dividend adjustment. No network (fixtures only)."""
from __future__ import annotations

import pytest

from apex_investor.prices import Bar, parse_chart, verify_adjustment


def _payload(dates, closes, adj, vols=None):
    vols = vols or [1000] * len(dates)
    return {"chart": {"result": [{
        "timestamp": dates,
        "indicators": {
            "quote": [{"close": closes, "volume": vols}],
            "adjclose": [{"adjclose": adj}],
        },
    }]}}


def test_parse_chart_basic():
    p = _payload([1704067200], [100.0], [100.0], [500])
    bars = parse_chart(p)
    assert len(bars) == 1
    assert bars[0].close == 100.0 and bars[0].volume == 500
    assert bars[0].date == "2024-01-01"


def test_parse_chart_skips_null_closes():
    p = _payload([1704067200, 1704153600], [100.0, None], [100.0, None])
    bars = parse_chart(p)
    assert len(bars) == 1


def test_parse_chart_rejects_empty():
    with pytest.raises(ValueError, match="no usable bars"):
        parse_chart(_payload([1704067200], [None], [None]))


def test_parse_chart_falls_back_to_close_when_no_adjclose():
    p = {"chart": {"result": [{"timestamp": [1704067200],
                               "indicators": {"quote": [{"close": [50.0], "volume": [1]}]}}]}}
    bars = parse_chart(p)
    assert bars[0].adj_close == 50.0


def test_verify_adjustment_source_preapplies_splits():
    # This data source pre-applies splits: close is continuous across the split date.
    bars = [
        Bar("2024-10-23", 40.0, 40.0, 1000),
        Bar("2024-10-24", 40.5, 40.5, 1000),   # no raw jump
        Bar("2024-10-25", 41.8, 41.8, 1000),
    ]
    v = verify_adjustment(bars, "2024-10-24", expected_ratio=8.0)
    assert v["raw_is_continuous"] is True
    assert v["adjusted_is_continuous"] is True
    assert v["raw_shows_unadjusted_split"] is False


def test_verify_adjustment_flags_unadjusted_source():
    # an UNADJUSTED source would show a raw jump equal to the split ratio
    bars = [
        Bar("2024-10-23", 40.0, 40.0, 1000),
        Bar("2024-10-24", 320.0, 40.5, 1000),   # raw x8
        Bar("2024-10-25", 330.0, 41.8, 1000),
    ]
    v = verify_adjustment(bars, "2024-10-24", expected_ratio=8.0)
    assert v["raw_shows_unadjusted_split"] is True
    assert v["raw_is_continuous"] is False


def test_verify_adjustment_missing_date_raises():
    with pytest.raises(ValueError):
        verify_adjustment([Bar("2024-01-01", 1.0, 1.0, 1)], "2024-06-06", 8.0)


# ---- improvement 5: live path refreshes a stale cache; replay does not --------

def _write_cache(tmp_path, symbol, bars):
    import json
    (tmp_path / f"{symbol}.json").write_text(
        json.dumps({"symbol": symbol, "bars": [b.to_dict() for b in bars]}))


def _epoch(iso_date: str) -> int:
    """Unix timestamp (UTC) for an ISO date, so a payload fixture cannot drift from its label."""
    from datetime import UTC, datetime
    return int(datetime.fromisoformat(iso_date).replace(tzinfo=UTC).timestamp())


def test_fetch_bars_returns_cache_without_network_when_fresh(monkeypatch, tmp_path):
    import urllib.request
    from datetime import UTC, datetime

    from apex_investor.prices import fetch_bars
    today = datetime.now(UTC).date().isoformat()
    _write_cache(tmp_path, "X", [Bar(today, 10.0, 10.0, 1)])

    def boom(*a, **k):
        raise AssertionError("network must not be used for a fresh cache")
    monkeypatch.setattr(urllib.request, "urlopen", boom)

    bars = fetch_bars("X", cache_dir=tmp_path, max_cache_age_days=7)
    assert bars[-1].close == 10.0


def test_fetch_bars_refreshes_stale_cache(monkeypatch, tmp_path):
    """A stale cache with a refresh policy must attempt a network fetch."""
    import json
    import urllib.request

    from apex_investor.prices import fetch_bars
    _write_cache(tmp_path, "X", [Bar("2024-12-30", 5000.0, 5000.0, 1)])   # stale

    fresh = Bar("2026-11-05", 2.29, 2.29, 1)
    payload = json.dumps({"chart": {"result": [{
        "timestamp": [_epoch(fresh.date)], "indicators": {
            "quote": [{"close": [fresh.close], "volume": [1]}],
            "adjclose": [{"adjclose": [fresh.adj_close]}]}}]}}).encode()

    class _Resp:
        def read(self):
            return payload
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())

    bars = fetch_bars("X", cache_dir=tmp_path, max_cache_age_days=7)
    assert bars[-1].close == pytest.approx(2.29)          # refreshed, not the stale 5000
    assert bars[-1].date == fresh.date                    # and the date matches the label


def test_fetch_bars_keeps_stale_cache_when_refresh_fails(monkeypatch, tmp_path):
    import urllib.request

    from apex_investor.prices import fetch_bars
    _write_cache(tmp_path, "X", [Bar("2024-12-30", 5000.0, 5000.0, 1)])

    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr("apex_investor.prices.time.sleep", lambda *_: None)

    bars = fetch_bars("X", cache_dir=tmp_path, max_cache_age_days=7)
    assert bars[-1].close == 5000.0        # falls back to cache; caller's staleness check decides


def test_fetch_bars_does_not_refresh_without_policy(monkeypatch, tmp_path):
    """Replay semantics: no refresh policy -> stale cache is returned as-is (no network)."""
    import urllib.request

    from apex_investor.prices import fetch_bars
    _write_cache(tmp_path, "X", [Bar("2024-12-30", 5000.0, 5000.0, 1)])

    def boom(*a, **k):
        raise AssertionError("replay must not hit the network")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    bars = fetch_bars("X", cache_dir=tmp_path)     # max_cache_age_days default None
    assert bars[-1].close == 5000.0


# ---- round 4, finding 3: a future-dated bar is not "fresh" ---------------------

def test_latest_quote_rejects_future_dated_bar(monkeypatch, tmp_path):
    import apex_investor.prices as prices
    from apex_investor.prices import latest_quote
    def fake(symbol, *a, **k):
        return [Bar("2027-01-01", 100.0, 100.0, 1)]     # in the future
    monkeypatch.setattr(prices, "fetch_bars", fake)
    assert latest_quote("X", cache_dir=tmp_path, today="2026-11-06") is None


def test_fetch_bars_refreshes_future_dated_cache(monkeypatch, tmp_path):
    """A future-dated cache must not be accepted as fresh; a fetch is attempted."""
    import json
    import urllib.request

    from apex_investor.prices import fetch_bars
    _write_cache(tmp_path, "X", [Bar("2027-01-01", 999.0, 999.0, 1)])   # future

    fresh_date = "2026-11-05"
    payload = json.dumps({"chart": {"result": [{
        "timestamp": [_epoch(fresh_date)], "indicators": {
            "quote": [{"close": [2.29], "volume": [1]}],
            "adjclose": [{"adjclose": [2.29]}]}}]}}).encode()

    class _Resp:
        def read(self):
            return payload
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())

    bars = fetch_bars("X", cache_dir=tmp_path, max_cache_age_days=7)
    assert bars[-1].close == pytest.approx(2.29)      # refreshed, not the future 999
    assert bars[-1].date == fresh_date


# ---- round 7: fetch errors are classified, not guessed -------------------------

def test_fetch_bars_wraps_retryable_error_as_price_fetch_error(monkeypatch, tmp_path):
    """A genuine provider/network failure (no cache) raises the dedicated PriceFetchError."""
    import urllib.request

    from apex_investor.prices import PriceFetchError, fetch_bars

    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr("apex_investor.prices.time.sleep", lambda *_: None)

    with pytest.raises(PriceFetchError):
        fetch_bars("X", cache_dir=tmp_path)


def test_fetch_bars_does_not_retry_or_wrap_unexpected_defect(monkeypatch, tmp_path):
    """A programming defect must propagate UNWRAPPED — never retried, never masked as a data error."""
    import urllib.request

    from apex_investor.prices import PriceFetchError, fetch_bars

    calls = {"n": 0}

    def defect(*a, **k):
        calls["n"] += 1
        raise TypeError("programming defect in fetch layer")
    monkeypatch.setattr(urllib.request, "urlopen", defect)
    monkeypatch.setattr("apex_investor.prices.time.sleep", lambda *_: None)

    with pytest.raises(TypeError):
        fetch_bars("X", cache_dir=tmp_path)
    assert calls["n"] == 1                    # not retried
    # and it must not be the dedicated (data-limitation) exception
    assert not isinstance(TypeError(), PriceFetchError)


# ---- round 8: parser signals malformed provider data deliberately ---------------

def test_parse_chart_malformed_shape_raises_malformed_payload():
    from apex_investor.prices import MalformedPayloadError
    with pytest.raises(MalformedPayloadError):
        parse_chart({"chart": {}})              # missing result -> provider-data problem


def test_parse_chart_no_usable_bars_raises_malformed_payload():
    from apex_investor.prices import MalformedPayloadError
    with pytest.raises(MalformedPayloadError):
        parse_chart(_payload([1704067200], [None], [None]))


def test_fetch_bars_wraps_malformed_payload_as_price_fetch_error(monkeypatch, tmp_path):
    """A malformed provider payload (no cache) is a data-availability failure, not a defect."""
    import urllib.request

    from apex_investor.prices import PriceFetchError, fetch_bars

    class _Resp:
        def read(self):
            return b'{"chart": {}}'                 # valid JSON, malformed chart
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr("apex_investor.prices.time.sleep", lambda *_: None)

    with pytest.raises(PriceFetchError):
        fetch_bars("X", cache_dir=tmp_path)


def test_fetch_bars_non_json_response_is_retryable_data_error(monkeypatch, tmp_path):
    import urllib.request

    from apex_investor.prices import PriceFetchError, fetch_bars

    calls = {"n": 0}

    class _Resp:
        def read(self):
            calls["n"] += 1
            return b"<html>provider error page</html>"   # not JSON
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr("apex_investor.prices.time.sleep", lambda *_: None)

    with pytest.raises(PriceFetchError):
        fetch_bars("X", cache_dir=tmp_path)
    assert calls["n"] == 3                          # retried 3 times (a transient provider problem)


# ---- round 9: the provider-error boundary is narrow ----------------------------

def test_internal_defect_in_iso_is_not_mislabelled_as_provider_error(monkeypatch):
    """A bug in an internal helper must NOT become MalformedPayloadError (provider data)."""
    import apex_investor.prices as prices
    from apex_investor.prices import MalformedPayloadError, parse_chart

    def buggy_iso(ts):
        raise TypeError("internal defect in _iso")
    monkeypatch.setattr(prices, "_iso", buggy_iso)

    payload = _payload([1704067200], [100.0], [100.0])
    with pytest.raises(TypeError) as ei:
        parse_chart(payload)
    assert not isinstance(ei.value, MalformedPayloadError)


def test_internal_defect_in_bar_construction_is_not_mislabelled(monkeypatch):
    """A bug in Bar construction must NOT become MalformedPayloadError."""
    import apex_investor.prices as prices
    from apex_investor.prices import MalformedPayloadError, parse_chart

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("internal defect building Bar")
    monkeypatch.setattr(prices, "Bar", Boom)

    with pytest.raises(RuntimeError) as ei:
        parse_chart(_payload([1704067200], [100.0], [100.0]))
    assert not isinstance(ei.value, MalformedPayloadError)


def test_non_finite_provider_value_is_malformed_payload():
    from apex_investor.prices import MalformedPayloadError, parse_chart
    with pytest.raises(MalformedPayloadError):
        parse_chart(_payload([1704067200], [float("inf")], [float("inf")]))


def test_non_numeric_provider_close_is_malformed_payload():
    from apex_investor.prices import MalformedPayloadError, parse_chart
    with pytest.raises(MalformedPayloadError):
        parse_chart(_payload([1704067200], ["not-a-number"], ["not-a-number"]))


def test_bad_shape_provider_payload_is_malformed_payload():
    from apex_investor.prices import MalformedPayloadError, parse_chart
    with pytest.raises(MalformedPayloadError):
        parse_chart({"chart": {"result": [{"timestamp": [1]}]}})   # no indicators


# ---- round 10: validate provider arrays and provider overflow -------------------

def test_null_timestamp_array_is_malformed_payload():
    """{"timestamp": None} must fail as a PROVIDER problem, not escape as a TypeError defect."""
    from apex_investor.prices import MalformedPayloadError, parse_chart
    payload = {"chart": {"result": [{
        "timestamp": None,
        "indicators": {"quote": [{"close": [100.0], "volume": [1]}],
                       "adjclose": [{"adjclose": [100.0]}]}}]}}
    with pytest.raises(MalformedPayloadError):
        parse_chart(payload)


def test_scalar_timestamp_array_is_malformed_payload():
    from apex_investor.prices import MalformedPayloadError, parse_chart
    payload = {"chart": {"result": [{
        "timestamp": 123,
        "indicators": {"quote": [{"close": [100.0], "volume": [1]}],
                       "adjclose": [{"adjclose": [100.0]}]}}]}}
    with pytest.raises(MalformedPayloadError):
        parse_chart(payload)


def test_non_sequence_close_array_is_malformed_payload():
    from apex_investor.prices import MalformedPayloadError, parse_chart
    with pytest.raises(MalformedPayloadError):
        parse_chart(_payload([1704067200], None, [100.0]))


def test_non_list_volume_array_is_malformed_payload():
    from apex_investor.prices import MalformedPayloadError, parse_chart
    with pytest.raises(MalformedPayloadError):
        parse_chart(_payload([1704067200], [100.0], [100.0], vols=999))


def test_oversized_integer_provider_value_is_malformed_payload():
    """float() raises OverflowError for an enormous int; it must be a provider-data problem."""
    from apex_investor.prices import MalformedPayloadError, parse_chart
    huge = 10 ** 400          # far beyond float range -> OverflowError on float()
    with pytest.raises(MalformedPayloadError):
        parse_chart(_payload([1704067200], [huge], [huge]))


def test_oversized_int_helper_raises_malformed_payload():
    from apex_investor.prices import MalformedPayloadError, _provider_number
    with pytest.raises(MalformedPayloadError):
        _provider_number(10 ** 400, what="close")
