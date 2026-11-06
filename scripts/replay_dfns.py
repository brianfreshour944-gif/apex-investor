"""ACCEPTANCE TEST: replay DFNS/NUKK and confirm the monitor *notices* the Dec-2024 move.

This is a test, not a tuning target. The thresholds are the Phase 1 defaults; nothing here is
fitted to DFNS. The operator's real cost basis is used as a FIXTURE only — it must never enter
the live ledger (old holdings stay out of the new portfolio's starting capital).

Run:  uv run python scripts/replay_dfns.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apex_investor.monitor import erosion_alerts, gain_alerts, volume_alerts  # noqa: E402
from apex_investor.prices import PriceFetchError, fetch_bars  # noqa: E402

# Operator's broker-reported cost basis per share, split-adjusted (TEST FIXTURE ONLY).
DFNS_COST_PS = 572.03
# A meaningful acceptance replay needs a real historical window, not one stray bar.
MIN_BARS = 30


def main() -> int:
    try:
        bars = fetch_bars("DFNS", start="2024-12-01", end="2025-03-31")
    except (PriceFetchError, OSError) as e:
        # expected availability failure -> a data limitation, not a defect
        print(f"DFNS replay: fetch unavailable ({e.__class__.__name__}: {e})")
        print("ACCEPTANCE: DATA-LIMITED (cannot evaluate — no data available)")
        return 2
    # any OTHER exception propagates as a real error (a defect must not masquerade as missing data)
    if len(bars) < MIN_BARS:
        print(f"DFNS replay: only {len(bars)} bars (need >= {MIN_BARS})")
        print("ACCEPTANCE: DATA-LIMITED (cannot evaluate — insufficient history)")
        return 2

    print(f"DFNS replay: {len(bars)} bars {bars[0].date}..{bars[-1].date}")
    print(f"cost basis fixture (split-adjusted): {DFNS_COST_PS}\n")

    alerts = (gain_alerts(bars, "DFNS", DFNS_COST_PS)
              + volume_alerts(bars, "DFNS")
              + erosion_alerts(bars, "DFNS", DFNS_COST_PS))
    alerts.sort(key=lambda a: a.date)

    print("ALERTS THE SYSTEM WOULD HAVE RAISED:")
    for a in alerts:
        print("  " + a.render())

    extraordinary = [a for a in alerts if a.kind == "extraordinary_gain"]
    ok = bool(extraordinary) and extraordinary[0].date == "2024-12-17"
    print("\nACCEPTANCE:")
    print(f"  extraordinary_gain alert on 2024-12-17: {'PASS' if ok else 'FAIL'}")
    print(f"  total alerts: {len(alerts)} (awareness only — the system sells nothing)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
