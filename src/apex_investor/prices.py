"""Price ingestion: fetch daily bars and store them with verified split/dividend adjustment.

Design rules:
  - One fetch path (Yahoo chart API), cached to disk as JSON. No network in tests.
  - Both RAW close and ADJUSTED close (total return) are stored, so split effects are auditable.
  - `verify_adjustment` proves the adjustment on a known corporate action (e.g. DFNS 1:8).
  - Pure parsing logic is separated from I/O so it can be unit-tested offline.

PRICE BASIS (single, enforced):
  Yahoo's `close` is ALREADY split-adjusted, so `close` is the basis used for ALL valuation and
  gain/erosion maths (the monitor signals read `bar.close` directly). `adj_close` differs only by
  dividend reinvestment, so mixing it in would make the report's snapshot and its gain alerts
  disagree for dividend payers. `adj_close` is retained for audit and split/dividend verification,
  never for valuation.

FAIL-CLOSED:
  `latest_quote` returns None when a fetch fails OR when the newest bar is older than
  `max_age_days`, so a stale cache entry can never masquerade as a live price.
"""
from __future__ import annotations

import json
import math
import time
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

DEFAULT_CACHE = Path("data/cache")
UA = {"User-Agent": "Mozilla/5.0 apex-investor"}


class MalformedPayloadError(ValueError):
    """The provider returned data that cannot be parsed into bars.

    Raised DELIBERATELY by `parse_chart` (and only there) so a malformed or unavailable provider
    payload is distinguishable from a programming defect. Subclasses ValueError for callers that
    expect a ValueError, but it is the *type* — not a bare ValueError — that the retry loop trusts.
    """


class PriceFetchError(Exception):
    """Daily bars could not be obtained after retries.

    Deliberately distinct from an unexpected programming error so callers can classify a genuine
    provider/network/data-availability failure WITHOUT guessing (e.g. the replay harness).
    """


# Provider/network/parse failures worth retrying. Both are DATA-AVAILABILITY problems, never
# programming defects: `OSError` is the network/IO layer, and `MalformedPayloadError` is raised
# DELIBERATELY by `parse_chart`. A bare KeyError/ValueError/TypeError from a defect is NOT in this
# tuple, so the retry loop below lets it propagate instead of masking it as a data error.
RETRYABLE_FETCH_ERRORS = (OSError, MalformedPayloadError)


@dataclass(frozen=True)
class Bar:
    date: str          # ISO date (UTC)
    close: float       # as-printed close
    adj_close: float   # total-return adjusted close
    volume: float

    def to_dict(self) -> dict:
        return asdict(self)


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), UTC).date().isoformat()


def _raw_series(payload: dict) -> tuple:
    """Extract and validate the raw provider arrays from a chart payload.

    Only CONTAINER ACCESS and SHAPE validation on the untrusted payload happen here, so only
    payload-SHAPE errors (a missing key, a non-sequence, a wrong type) are translated to
    MalformedPayloadError. The arrays are confirmed to be lists before they are returned, so a
    payload like {"timestamp": None} or {"timestamp": 123} fails HERE as a provider problem rather
    than surfacing later as an apparent defect in enumerate()/len(). No value coercion and no
    object construction occur inside, so a defect in internal code cannot be mislabelled.
    """
    try:
        result = payload["chart"]["result"][0]
        indicators = result["indicators"]
        quote = indicators["quote"][0]
        timestamps = result["timestamp"]
        closes = quote["close"]
        volumes = quote["volume"]
        adj_blocks = indicators.get("adjclose") or []
        adj = adj_blocks[0]["adjclose"] if adj_blocks else None
    except (KeyError, IndexError, TypeError) as e:
        raise MalformedPayloadError(f"malformed chart payload shape: {e!r}") from e
    # A JSON array decodes to a list; anything else (None, a number, a dict) is a malformed shape.
    if not isinstance(timestamps, list) or not isinstance(closes, list):
        raise MalformedPayloadError(
            f"provider arrays are not lists: timestamp={type(timestamps).__name__}, "
            f"close={type(closes).__name__}")
    if volumes is not None and not isinstance(volumes, list):
        raise MalformedPayloadError(f"volume array is not a list: {type(volumes).__name__}")
    if adj is not None and not isinstance(adj, list):
        raise MalformedPayloadError(f"adjclose array is not a list: {type(adj).__name__}")
    return timestamps, closes, adj, volumes


def _provider_number(value, *, what: str) -> float:
    """Coerce a PROVIDER-SUPPLIED value to a finite float, or raise MalformedPayloadError.

    Attribution is explicit: only a non-numeric, non-convertible or non-finite value coming from
    the payload is translated. The type check runs before the narrow `float()` guard, which catches
    both ValueError and OverflowError (an oversized integer), and finiteness is enforced afterwards
    so a non-finite provider value (which would otherwise poison valuation) is caught here. An
    internal defect elsewhere can never surface as this exception.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise MalformedPayloadError(f"{what} is not numeric: {value!r}")
    try:
        number = float(value)
    except (ValueError, OverflowError) as e:
        raise MalformedPayloadError(f"{what} is not numeric: {value!r}") from e
    if not math.isfinite(number):
        raise MalformedPayloadError(f"{what} is not finite: {value!r}")
    return number


def parse_chart(payload: dict) -> list[Bar]:
    """Parse a Yahoo chart payload into Bars.

    Raises `MalformedPayloadError` (a ValueError) for a malformed or unavailable PROVIDER payload:
    a bad shape (via `_raw_series`) or a non-numeric provider value (via `_provider_number`). Only
    those two provider-attributable cases are translated. All internal transformations — date
    formatting (`_iso`) and `Bar` construction — happen OUTSIDE any guard, so a defect in this
    module propagates as itself instead of being misread as a data problem.
    """
    timestamps, closes, adj, volumes = _raw_series(payload)
    bars: list[Bar] = []
    for i, t in enumerate(timestamps):
        close = closes[i] if i < len(closes) else None
        adj_close = adj[i] if (adj is not None and i < len(adj)) else None
        if adj_close is None:
            adj_close = close
        if close is None or adj_close is None:
            continue
        raw_vol = volumes[i] if i < len(volumes) else 0
        # provider values are validated here; the Bar is built from already-coerced values
        bars.append(Bar(_iso(int(_provider_number(t, what="timestamp"))),
                        _provider_number(close, what="close"),
                        _provider_number(adj_close, what="adj_close"),
                        _provider_number(0 if raw_vol is None else raw_vol, what="volume")))
    if not bars:
        raise MalformedPayloadError("no usable bars in payload")
    return bars


def fetch_bars(symbol: str, start: str = "2015-01-01", end: str | None = None,
               cache_dir: Path | str = DEFAULT_CACHE, use_cache: bool = True,
               max_cache_age_days: int | None = None) -> list[Bar]:
    """Fetch daily bars, caching to `cache_dir/<symbol>.json` (see `_fetch_bars_impl`)."""
    return _fetch_bars_impl(symbol, start=start, end=end, cache_dir=cache_dir,
                            use_cache=use_cache, max_cache_age_days=max_cache_age_days)[0]


def fetch_bars_sourced(symbol: str, start: str = "2015-01-01", end: str | None = None,
                       cache_dir: Path | str = DEFAULT_CACHE, use_cache: bool = True,
                       max_cache_age_days: int | None = None) -> tuple[list[Bar], str]:
    """`fetch_bars` plus provenance: 'network' | 'cache' | 'cache-fallback'.

    The report must state where each quote ACTUALLY came from rather than guessing from
    configuration, so the live path records retrieval provenance per ticker (review F4).
    """
    return _fetch_bars_impl(symbol, start=start, end=end, cache_dir=cache_dir,
                            use_cache=use_cache, max_cache_age_days=max_cache_age_days)


def _fetch_bars_impl(symbol: str, start: str = "2015-01-01", end: str | None = None,
                     cache_dir: Path | str = DEFAULT_CACHE, use_cache: bool = True,
                     max_cache_age_days: int | None = None) -> tuple[list[Bar], str]:
    """Fetch daily bars, caching to `cache_dir/<symbol>.json`. Returns (bars, provenance).

    `use_cache` reads an existing cache file. `max_cache_age_days`, when set, treats a cache whose
    newest bar is older than that as a MISS and refetches from the network (the live path uses this
    so a stale cache does not block a report that a fresh fetch could satisfy). Historical replay
    leaves it None, so replay reads its own isolated cache and never hits the network by surprise.
    If a refresh is attempted and fails, the cached bars are returned as-is (the caller's staleness
    check then decides whether they are usable).
    """
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    f = cache / f"{symbol}.json"
    cached: list[Bar] | None = None
    if use_cache and f.exists():
        try:
            cached = [Bar(**b) for b in json.loads(f.read_text())["bars"]]
        except Exception:  # noqa: BLE001 - a corrupt cache is treated as a miss
            cached = None
        if cached and max_cache_age_days is not None:
            ref = datetime.now(UTC).date()
            try:
                newest = date.fromisoformat(cached[-1].date)
            except ValueError:
                newest = None
            age = (ref - newest).days if newest is not None else None
            # fresh only if within the limit AND not future-dated (a future bar is not trustworthy)
            if age is not None and 0 <= age <= max_cache_age_days:
                return cached, "cache"
            # stale or future-dated -> fall through and try a fresh fetch
        elif cached:
            return cached, "cache"

    end = end or datetime.now(UTC).date().isoformat()
    p1 = int(datetime.fromisoformat(start).replace(tzinfo=UTC).timestamp())
    p2 = int(datetime.fromisoformat(end).replace(tzinfo=UTC).timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?period1={p1}&period2={p2}&interval=1d&events=split|div")
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=25).read()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as e:
                # the provider returned something that is not JSON -> a provider-data problem
                raise MalformedPayloadError(f"response is not JSON: {e!r}") from e
            bars = parse_chart(payload)
            f.write_text(json.dumps({"symbol": symbol, "bars": [b.to_dict() for b in bars]}))
            return bars, "network"
        except RETRYABLE_FETCH_ERRORS as e:
            # expected availability problem (network / non-JSON / malformed payload): retry
            last_err = e
            time.sleep(1.5 * (attempt + 1))
        # anything else (e.g. TypeError/AttributeError from a defect) propagates immediately and is
        # never retried, never re-wrapped as a data-availability failure
    if cached is not None:            # refresh failed; hand back what we have (staleness decides)
        return cached, "cache-fallback"
    raise PriceFetchError(f"fetch failed for {symbol}: {last_err!r}") from last_err


def latest_quote(symbol: str, *, cache_dir: Path | str = DEFAULT_CACHE,
                 use_cache: bool = True, max_age_days: int | None = 7,
                 today: str | None = None) -> Bar | None:
    """Most recent daily bar for `symbol` (split-adjusted close basis, same as fetch_bars).

    Returns None on failure so callers can fail closed — a missing quote must never be read as
    a valid price. Uses the same cache layer as historical bars, so both share one basis.

    `max_age_days` rejects a STALE bar (e.g. a leftover backtest cache for a ticker whose real
    history is years old). Without this, a stale cache entry can masquerade as a live quote.

    Refresh policy: the live path asks the cache layer to refresh a stale cache (same age limit),
    so a stale cache does not block a report that a fresh fetch could satisfy. Historical replay
    keeps its own cache dir and does not refresh.
    """
    try:
        bars = fetch_bars(symbol, cache_dir=cache_dir, use_cache=use_cache,
                          max_cache_age_days=max_age_days)
    except Exception:  # noqa: BLE001 - fail closed by returning None
        return None
    if not bars:
        return None
    bar = bars[-1]
    if max_age_days is not None:
        ref = date.fromisoformat(today) if today else datetime.now(UTC).date()
        try:
            age = (ref - date.fromisoformat(bar.date)).days
        except ValueError:
            return None
        if age > max_age_days or age < 0:      # too old, or future-dated (not trustworthy)
            return None
    return bar


def verify_adjustment(bars: list[Bar], split_date: str, expected_ratio: float) -> dict:
    """Verify how a corporate action is represented in this data source.

    IMPORTANT data-source behaviour (verified empirically 2026-11-03): Yahoo's `close` is
    **already split-adjusted**; only `adjclose` additionally applies dividends. So a split does
    NOT appear as a raw jump — for a non-dividend payer, `close == adjclose` and the split is
    invisible in both series. This function therefore verifies *continuity* (the split has been
    applied, so no artificial jump remains) rather than expecting a jump.

    Returns the day-over-day ratios and whether the series is continuous across `split_date`.
    """
    idx = next((i for i, b in enumerate(bars) if b.date == split_date), None)
    if idx is None or idx == 0:
        raise ValueError(f"date {split_date} not in series (or is first bar)")
    prev, cur = bars[idx - 1], bars[idx]
    raw_ratio = cur.close / prev.close
    adj_ratio = cur.adj_close / prev.adj_close
    return {
        "date": split_date,
        "raw_ratio": round(raw_ratio, 4),
        "adj_ratio": round(adj_ratio, 4),
        "expected_split": expected_ratio,
        # this source pre-applies splits, so the raw series should be continuous (no jump)
        "raw_is_continuous": abs(raw_ratio - 1.0) < 0.25,
        "adjusted_is_continuous": abs(adj_ratio - 1.0) < 0.25,
        # a raw jump equal to the split would indicate an UNADJUSTED source
        "raw_shows_unadjusted_split": abs(raw_ratio - expected_ratio) / expected_ratio < 0.25,
    }
