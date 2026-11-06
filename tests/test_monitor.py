"""M3 tests: monitoring signals. Each signal fires exactly once on a synthetic series."""
from __future__ import annotations

import pytest

from apex_investor.monitor import (
    drawdown_alerts,
    drift_alerts,
    erosion_alerts,
    gain_alerts,
    volume_alerts,
)
from apex_investor.prices import Bar


def bars_from(closes, vols=None):
    vols = vols or [1000] * len(closes)
    return [Bar(f"2024-01-{i + 1:02d}", c, c, v) for i, (c, v) in enumerate(zip(closes, vols, strict=False))]


def test_unusual_gain_fires_once():
    bars = bars_from([100, 110, 130, 140, 128])
    a = gain_alerts(bars, "X", cost_basis_ps=100.0)
    assert len(a) == 1
    assert a[0].kind == "unusual_gain" and a[0].severity == "warn"
    assert a[0].date == "2024-01-03"        # first close >= +25%


def test_gain_thresholds_fire_once_each_in_order():
    # crosses +25% on day 2, +100% on day 3 -> one alert per threshold, chronologically
    bars = bars_from([100, 130, 210, 180])
    a = gain_alerts(bars, "X", cost_basis_ps=100.0)
    assert [x.kind for x in a] == ["unusual_gain", "extraordinary_gain"]
    assert a[0].date == "2024-01-02" and a[1].date == "2024-01-03"


def test_extraordinary_only_when_jump_skips_unusual():
    # first close already >= +100% -> only the extraordinary alert
    bars = bars_from([100, 250])
    a = gain_alerts(bars, "X", cost_basis_ps=100.0)
    assert len(a) == 1 and a[0].kind == "extraordinary_gain"


def test_no_gain_alert_when_below_threshold():
    assert gain_alerts(bars_from([100, 101, 102]), "X", 100.0) == []


def test_gain_alert_requires_positive_cost():
    with pytest.raises(ValueError):
        gain_alerts(bars_from([1, 2]), "X", 0.0)


def test_volume_alert_fires_on_spike():
    closes = [100] * 25
    vols = [1000] * 24 + [9000]          # last day 9x
    a = volume_alerts(bars_from(closes, vols), "X", window=20, mult=5.0)
    assert len(a) == 1 and a[0].kind == "unusual_volume"
    assert a[0].evidence["multiple"] == pytest.approx(9.0, rel=0.05)


def test_volume_no_alert_when_normal():
    a = volume_alerts(bars_from([100] * 25, [1000] * 25), "X", window=20)
    assert a == []


def test_erosion_alert_is_advisory_and_fires_once():
    # runs to +100% (arm), then gives back >30% of the peak gain
    bars = bars_from([100, 150, 200, 210, 190, 160, 150])
    a = erosion_alerts(bars, "X", cost_basis_ps=100.0, arm=1.00, give_back=0.30)
    assert len(a) == 1
    assert a[0].kind == "gain_erosion" and a[0].severity == "warn"
    assert "gave back" in a[0].message


def test_erosion_does_not_fire_without_a_big_runup():
    bars = bars_from([100, 110, 105, 100, 90])
    assert erosion_alerts(bars, "X", 100.0) == []


def test_drift_alert():
    a = drift_alerts({"AAPL": 0.30, "SPY": 0.60}, {"AAPL": 0.20, "SPY": 0.60},
                     band=0.05, date="2024-06-01")
    assert len(a) == 1 and a[0].ticker == "AAPL" and a[0].kind == "drift"


def test_drawdown_warn_then_critical():
    eq = [("d1", 100), ("d2", 90), ("d3", 84), ("d4", 74), ("d5", 80)]
    a = drawdown_alerts(eq, warn=-0.15, critical=-0.25)
    assert [x.severity for x in a] == ["warn", "critical"]
    assert a[0].date == "d3" and a[1].date == "d4"


def test_alerts_render_with_evidence():
    a = gain_alerts(bars_from([100, 130]), "X", 100.0)[0]
    s = a.render()
    assert "unusual_gain" in s and "gain=" in s and "cost=" in s
