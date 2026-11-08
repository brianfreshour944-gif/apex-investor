"""Daily report (M5): one Markdown file per day, plus matching console output.

Design:
  - One file per day at reports/daily/YYYY-MM-DD.md (predictable location).
  - Six sections: snapshot, risk status, alerts, evidence, blocked checks, summary.
  - NEVER silently overwrite an existing report. Writing to an occupied path raises unless
    `regenerate=True`, which preserves the original as YYYY-MM-DD.original-<ts>.md and records
    the regeneration timestamp in the new report.
  - Console output is generated from the same object, so the two cannot drift.
  - No email, no broker, no AI, no automated trading. This module reports; it never trades.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .advisory import AdvisorySection
from .ledger import Ledger
from .monitor import Alert, dedupe
from .risk import RiskAssessment, RiskLimits, assess
from .sanity import QuoteCheck

DEFAULT_REPORTS_DIR = Path("reports/daily")
MAX_PRICE_AGE_DAYS = 5
NO_TRADE_NOTE = "No automatic trade instructions. This report informs; you approve every trade."


def _age_days(older: str, newer: str) -> int:
    """Whole days from `older` to `newer` (both ISO dates). Raises ValueError on bad input."""
    a = datetime.fromisoformat(older).date()
    b = datetime.fromisoformat(newer).date()
    return (b - a).days


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compact(stamp: str) -> str:
    return stamp.replace("-", "").replace(":", "")


def _money(x: float | None) -> str:
    return "n/a" if x is None else f"${x:,.2f}"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.2%}"


def _render_published(status: str, published_at: str | None) -> str:
    """Render a publication date WITHOUT ever implying freshness."""
    if status == "known":
        return published_at or "unknown"
    if status == "invalid":
        return "invalid (not used for recency)"
    return "unknown"


# Markdown-active characters in untrusted advisory text (source quotes, claims, titles, URLs).
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+!|<>])")
# Other C0 controls (excluding tab, handled separately), DEL, and C1 controls.
_MD_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")


def _md_safe(value: object) -> str:
    """Neutralize untrusted advisory text for a single Markdown line.

    Advisory values (quotes, claims, reasons, URLs, titles, model metadata) originate outside the
    renderer. This collapses CR/LF/tab to spaces, removes any remaining C0/C1 control characters,
    then escapes the listed Markdown structural characters. Result: the value stays on ONE line as
    literal text and cannot inject a heading, list item, section, link, or emphasis.
    """
    text = "" if value is None else str(value)
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = _MD_CONTROL.sub("", text)
    return _MD_SPECIAL.sub(r"\\\1", text)


@dataclass
class DailyReport:
    date: str
    generated_at: str
    equity: float | None
    cash: float | None
    cash_weight: float | None
    previous_equity: float | None
    holdings: list[dict]
    risk: RiskAssessment
    alerts: list[Alert]
    price_source: str
    price_as_of: str | None
    volume_baseline_days: int
    risk_limits: RiskLimits = field(default_factory=RiskLimits)
    quote_checks: list = field(default_factory=list)
    price_sources: dict = field(default_factory=dict)  # ticker -> network|cache|cache-fallback
    regenerated_at: str | None = None
    preserved_original: str | None = None
    extra_evidence: dict = field(default_factory=dict)
    config_problems: list[str] = field(default_factory=list)
    advisory: AdvisorySection | None = None

    # ---- derived -----------------------------------------------------
    @property
    def daily_change(self) -> tuple[float, float] | None:
        if self.equity is None or self.previous_equity in (None, 0):
            return None
        delta = self.equity - self.previous_equity
        return delta, delta / self.previous_equity

    @property
    def status(self) -> str:
        if not self.risk.trustworthy:
            return "BLOCKED"
        # A SUSPECT quote is valued for continuity, but that value is NOT verified, so it must not
        # read as CRITICAL/OK. An UNVERIFIABLE quote could not be checked at all. Either way the
        # report degrades to at most WARN and says so — a price we could not fully trust must never
        # present as a clean OK.
        if any(getattr(c, "status", "") in ("SUSPECT", "UNVERIFIABLE") for c in self.quote_checks):
            return "WARN"
        if any(a.severity == "critical" for a in self.alerts):
            return "CRITICAL"
        if self.alerts:
            return "WARN"
        return "OK"

    @property
    def unverified_tickers(self) -> list[str]:
        return [c.ticker for c in self.quote_checks if getattr(c, "status", "") == "SUSPECT"]

    @property
    def unchecked_tickers(self) -> list[str]:
        return [c.ticker for c in self.quote_checks if getattr(c, "status", "") == "UNVERIFIABLE"]

    # ---- rendering ---------------------------------------------------
    def _header(self) -> list[str]:
        lines = [f"# Daily Report — {self.date}", ""]
        meta = f"_Generated {self.generated_at} · prices: {self.price_source}"
        if self.price_as_of:
            meta += f" · as of {self.price_as_of}"
        meta += "_"
        lines += [meta, ""]
        if self.regenerated_at:
            lines += [f"> **REGENERATED {self.regenerated_at}.** Original preserved as "
                      f"`{self.preserved_original}`.", ""]
        return lines

    def _snapshot(self) -> list[str]:
        lines = ["## 1. Portfolio snapshot", ""]
        lines.append(f"- Total equity: {_money(self.equity)}")
        cw = "" if self.cash_weight is None else f" ({self.cash_weight:.1%})"
        lines.append(f"- Cash: {_money(self.cash)}{cw}")
        dc = self.daily_change
        lines.append(f"- Daily change: {_money(dc[0])} ({_pct(dc[1])})" if dc else "- Daily change: n/a")
        unverified = self.unverified_tickers
        if unverified:
            lines.append(f"- **Unverified price(s): {', '.join(unverified)}** — valued for continuity "
                         "only; confirm before trusting this equity or any weight.")
        unchecked = self.unchecked_tickers
        if unchecked:
            lines.append(f"- **Unchecked price(s): {', '.join(unchecked)}** — no usable previous "
                         "close; the daily comparison for these is UNAVAILABLE (their latest price "
                         "is still used for valuation).")
        lines.append("")
        if self.holdings:
            lines += ["| Ticker | Shares | Avg cost | Price | Market value | Weight | Unrealized |",
                      "|---|---|---|---|---|---|---|"]
            for h in self.holdings:
                weight = "n/a" if h["weight"] is None else f"{h['weight']:.1%}"
                unreal = "n/a" if h["unrealized"] is None else _money(h["unrealized"])
                price = "n/a" if h["price"] is None else _money(h["price"])
                if h["ticker"] in unverified and h["price"] is not None:
                    price += " (unverified)"
                lines.append(
                    f"| {h['ticker']} | {h['shares']:,.4g} | {_money(h['avg_cost'])} | "
                    f"{price} | {_money(h['market_value'])} | {weight} | {unreal} |")
        lines.append("")
        return lines

    def _risk(self) -> list[str]:
        lines = ["## 2. Risk status", ""]
        if not self.risk.trustworthy:
            lines += [f"- **Status: BLOCKED** — {len(self.risk.blocked)} check(s) could not be "
                      "trusted. See section 5.", ""]
            return lines
        lines.append(f"- Status: {self.status}")
        if self.risk.weights:
            tk, w = max(self.risk.weights.items(), key=lambda kv: kv[1])
            lines.append(f"- Largest position: {tk} {w:.1%} (limit "
                         f"{self.risk_limits.max_position_weight:.0%})")
        if self.risk.speculative_weight is not None:
            lines.append(f"- Speculative sleeve: {self.risk.speculative_weight:.1%} (limit "
                         f"{self.risk_limits.max_speculative_weight:.0%})")
        if self.risk.drawdown is not None:
            lines.append(f"- Drawdown: {self.risk.drawdown:.1%} (warn "
                         f"{self.risk_limits.drawdown_warn:.0%} / escalate "
                         f"{self.risk_limits.drawdown_critical:.0%})")
        lines.append("")
        return lines

    def _alerts(self) -> list[str]:
        lines = ["## 3. Alerts", ""]
        if not self.alerts:
            lines += ["_None._", ""]
            return lines
        for a in self.alerts:
            ev = ", ".join(f"{k}={v}" for k, v in a.evidence.items())
            lines.append(f"- **[{a.severity.upper()}] {a.ticker} — {a.kind}**: {a.message}")
            lines.append(f"  - why: {a.kind}; evidence: {ev}")
        lines.append("")
        return lines

    def _evidence(self) -> list[str]:
        lines = ["## 4. Evidence", "",
                 f"- Price source: {self.price_source}",
                 f"- Price as of: {self.price_as_of or 'n/a'}",
                 f"- Volume baseline: {self.volume_baseline_days}-day average"]
        if self.price_sources:
            lines.append("- Price retrieval: " + ", ".join(
                f"{tk}={src}" for tk, src in sorted(self.price_sources.items())))
        for k, v in self.extra_evidence.items():
            lines.append(f"- {k}: {v}")
        for problem in self.config_problems:
            lines.append(f"- **config problem:** {problem}")
        flagged = [c for c in self.quote_checks if c.flagged]
        if flagged:
            lines += ["", "### Preserved price-sanity checks", ""]
            for c in self.quote_checks:
                ev = ", ".join(f"{k}={v}" for k, v in c.evidence.items())
                lines.append(f"- {c.ticker}: **{c.status}** — {c.reason}")
                if ev:
                    lines.append(f"  - evidence: {ev}")
        lines.append("")
        return lines

    def _blocked(self) -> list[str]:
        lines = ["## 5. Blocked checks", ""]
        if not self.risk.blocked:
            lines += ["_None._", ""]
            return lines
        lines += [f"- {b}" for b in self.risk.blocked]
        lines.append("")
        return lines

    def _summary(self) -> list[str]:
        lines = ["## 6. Summary", ""]
        if not self.risk.trustworthy:
            lines.append("- **Risk checks are blocked; no clean verdict.** Resolve the blocked "
                         "items in section 5 before acting on anything.")
        else:
            unverified = self.unverified_tickers
            unchecked = self.unchecked_tickers
            if unverified:
                lines.append(f"- **Valuation is UNVERIFIED for {', '.join(unverified)}** — a "
                             "suspicious price was used for continuity. Confirm the price before "
                             "trusting equity, weights, or gains.")
            if unchecked:
                lines.append(f"- {', '.join(unchecked)} could not be price-checked (no usable "
                             "previous close); the daily comparison for these is UNAVAILABLE. "
                             "Their latest price is still used for valuation. Freshness still "
                             "applies.")
            if self.config_problems:
                lines.append(f"- **Configuration problem(s):** {'; '.join(self.config_problems)}")
            if self.alerts:
                lines.append(f"- {len(self.alerts)} alert(s) deserve attention (section 3).")
            if not unverified and not unchecked and not self.alerts and not self.config_problems:
                lines.append("- Nothing requires attention today.")
        lines.append(f"- {NO_TRADE_NOTE}")
        lines.append("")
        return lines

    def to_markdown(self) -> str:
        lines = self._header()
        for section in (self._snapshot, self._risk, self._alerts,
                        self._evidence, self._blocked, self._summary):
            lines += section()
        lines += self._advisory()
        return "\n".join(lines).rstrip() + "\n"

    def to_console(self) -> str:
        return self.to_markdown().rstrip()

    # ---- advisory (Phase 2 / M5) -------------------------------------
    def _advisory(self) -> list[str]:
        """Render the advisory section, or [] when no advisory was requested.

        Returns [] when `advisory is None`, so the Phase 1 report is byte-identical. Rendering is
        total and guarded: any ordinary error (never BaseException) degrades to the fallback text
        rather than breaking the daily report.
        """
        if self.advisory is None:
            return []
        try:
            return self._render_advisory()
        except Exception:  # noqa: BLE001 - deliberately catch-all so Phase 1 always renders
            return ["## 7. Interpretation (advisory)", "", "No interpretation available.", ""]

    def _render_advisory(self) -> list[str]:
        lines = ["## 7. Interpretation (advisory)", "",
                 f"_Advisory only — not a trade instruction. {NO_TRADE_NOTE}_", ""]
        section = self.advisory
        if not section.available:
            lines += ["No interpretation available.", ""]
            return lines
        if not section.items:
            lines += ["No findings.", ""]
            return lines
        for item in section.items:
            lines.append(f"### {_md_safe(item.ticker)} — finding {_md_safe(item.finding_id)}")
            lines.append(f"- Claim: {_md_safe(item.claim)}")
            lines.append(f"- Reason: {_md_safe(item.reason)}")
            lines.append(f"- Falsifier: {_md_safe(item.falsifier)}")
            lines.append(f"- Confidence: {item.confidence:.2f}")
            lines.append(f"- Model: {_md_safe(item.model)} (prompt {_md_safe(item.prompt_version)})")
            if item.recency == "unknown":
                lines.append("- Recency: unknown (a supporting document's publication date is "
                             "unknown or invalid; freshness not asserted)")
            else:
                age = "n/a" if item.evidence_age_days is None else f"{item.evidence_age_days} days old"
                lines.append(f"- Recency: {item.recency} (oldest supporting evidence {age})")
            lines.append("- Evidence:")
            for ev in item.evidence:
                lines.append(f'  - "{_md_safe(ev.quote)}"')
                title = f" ({_md_safe(ev.title)})" if ev.title else ""
                lines.append(f"    - source: {_md_safe(ev.source_url)}{title} (doc {_md_safe(ev.doc_id)})")
                lines.append(f"    - published: {_render_published(ev.published_at_status, ev.published_at)}"
                             f" · retrieved: {_md_safe(ev.retrieved_at)}")
            lines.append("")
        return lines


def build_report(date: str, ledger: Ledger, prices: dict[str, float], *,
                 tags: dict | None = None, limits: RiskLimits | None = None,
                 previous_equity: float | None = None, peak_equity: float | None = None,
                 price_source: str = "Yahoo Finance", price_as_of: str | None = None,
                 volume_baseline_days: int = 20, alerts: list[Alert] | None = None,
                 generated_at: str | None = None,
                 max_price_age_days: int = MAX_PRICE_AGE_DAYS,
                 price_dates: dict[str, str] | None = None,
                 quote_checks: list[QuoteCheck] | None = None,
                 missing_tickers: list[str] | None = None,
                 config_problems: list[str] | None = None,
                 price_sources: dict[str, str] | None = None,
                 advisory: AdvisorySection | None = None) -> DailyReport:
    """Assemble a DailyReport from the ledger, prices and alerts. Deterministic.

    FAIL CLOSED on stale, missing or unvalidatable price data:
      - the overall `price_as_of` must be within `max_price_age_days` of `date`;
      - if `price_dates` is given, EVERY ticker's own date is checked too, so one fresh ticker
        cannot mask a stale price for another;
      - if `quote_checks` is given, any BLOCKED quote blocks valuation and risk. SUSPECT quotes
        are surfaced as alerts, their evidence is preserved, and their valuation is marked
        UNVERIFIED (the report can never read as a clean OK for them).
    """
    limits = limits or RiskLimits()
    checks = list(quote_checks or [])

    # A blocked quote must not be valued at face value: drop it so valuation/risk cannot use an
    # unvalidated number, and block the report instead.
    blocked_tickers = {c.ticker for c in checks if c.blocks}
    valued_prices = {tk: px for tk, px in prices.items() if tk not in blocked_tickers}
    risk = assess(ledger, valued_prices, limits=limits, date=date, peak_equity=peak_equity,
                  tags=tags)

    # --- staleness gate (fail closed) ---
    stale_reasons: list[str] = []
    for tk in (missing_tickers or []):
        stale_reasons.append(f"no fresh quote for {tk}")
    if price_as_of is None:
        stale_reasons.append("no price as-of date supplied")
    else:
        try:
            age = _age_days(price_as_of, date)
        except ValueError:
            stale_reasons.append(f"invalid price as-of date: {price_as_of!r}")
        else:
            if age > max_price_age_days:
                stale_reasons.append(
                    f"stale prices: as of {price_as_of}, {age} days before {date} "
                    f"(max {max_price_age_days})")
            elif age < 0:
                stale_reasons.append(f"price as-of {price_as_of} is after report date {date}")

    for tk, pd_date in (price_dates or {}).items():
        try:
            age = _age_days(pd_date, date)
        except ValueError:
            stale_reasons.append(f"invalid price date for {tk}: {pd_date!r}")
            continue
        if age > max_price_age_days:
            stale_reasons.append(
                f"stale price for {tk}: {pd_date}, {age} days before {date}")
        elif age < 0:
            stale_reasons.append(f"price date for {tk} ({pd_date}) is after report date {date}")

    # Every HELD ticker must carry its own date. If per-ticker dates are omitted entirely while
    # positions are held, per-ticker freshness was never verified — that must not pass silently,
    # so it blocks too (callers supply `price_dates`, as the CLI does).
    if price_dates is None:
        if ledger.positions:
            stale_reasons.append(
                "no per-ticker price dates supplied; per-ticker freshness cannot be verified")
    else:
        for tk in sorted(ledger.positions):
            if tk not in price_dates:
                stale_reasons.append(f"no price date for {tk}")

    if stale_reasons:
        risk.blocked.extend(stale_reasons)
        risk.trustworthy = False
        if not any(a.kind == "risk_check_incomplete" for a in risk.alerts):
            risk.alerts.append(Alert(date, "PORTFOLIO", "risk_check_incomplete", "critical",
                                     "risk check could not be trusted; no clean verdict issued",
                                     {"blocked": len(risk.blocked),
                                      "reasons": "; ".join(risk.blocked)}))

    # --- price-sanity gate (flag for verification; block only when unvalidatable) ---
    sanity_alerts: list[Alert] = []
    for c in checks:
        if c.status == "OK":
            continue
        sev = {"BLOCKED": "critical", "SUSPECT": "warn", "UNVERIFIABLE": "info"}[c.status]
        sanity_alerts.append(Alert(c.latest_date, c.ticker, "price_sanity", sev, c.reason,
                                   dict(c.evidence)))
        if c.blocks:
            risk.blocked.append(
                f"price_sanity[{c.ticker}]: {c.reason} (latest {c.latest_close} on "
                f"{c.latest_date}, previous {c.previous_close} on {c.previous_date})")
            risk.trustworthy = False
    if any(c.blocks for c in checks) and not any(
            a.kind == "risk_check_incomplete" for a in risk.alerts):
        risk.alerts.append(Alert(date, "PORTFOLIO", "risk_check_incomplete", "critical",
                                 "risk check could not be trusted; no clean verdict issued",
                                 {"blocked": len(risk.blocked),
                                  "reasons": "; ".join(risk.blocked)}))

    # --- configuration gate (fail closed) ---
    # A present-but-unusable config file (e.g. a corrupt known_actions.json) is NOT the same as
    # having no actions: an operator may have meant to explain a split. Fail closed and say so.
    config_list = list(config_problems or [])
    if config_list:
        for problem in config_list:
            risk.blocked.append(f"config: {problem}")
        risk.trustworthy = False
        if not any(a.kind == "risk_check_incomplete" for a in risk.alerts):
            risk.alerts.append(Alert(date, "PORTFOLIO", "risk_check_incomplete", "critical",
                                     "risk check could not be trusted; no clean verdict issued",
                                     {"blocked": len(risk.blocked),
                                      "reasons": "; ".join(risk.blocked)}))

    holdings: list[dict] = []
    positions = ledger.positions
    for tk, pos in sorted(positions.items()):
        price = valued_prices.get(tk)
        price = price if isinstance(price, (int, float)) and price == price else None
        mv = pos.market_value(price) if price is not None else None
        holdings.append({
            "ticker": tk, "shares": pos.shares, "avg_cost": pos.avg_cost,
            "price": price, "market_value": mv,
            "weight": (mv / risk.equity) if (mv is not None and risk.equity) else None,
            "unrealized": pos.unrealized(price) if price is not None else None,
        })

    all_alerts = dedupe(list(alerts or []) + sanity_alerts + risk.alerts)

    return DailyReport(
        date=date, generated_at=generated_at or _utc_stamp(),
        equity=risk.equity, cash=ledger.cash, cash_weight=risk.cash_weight,
        previous_equity=previous_equity, holdings=holdings, risk=risk, alerts=all_alerts,
        price_source=price_source, price_as_of=price_as_of,
        volume_baseline_days=volume_baseline_days, risk_limits=limits,
        quote_checks=checks, config_problems=config_list,
        price_sources=dict(price_sources or {}),
        advisory=advisory,
    )


def _write_backup_exclusive(d: Path, date: str, stamp: str, content: str) -> Path:
    """Create a backup file atomically, never overwriting an existing one.

    Uses O_CREAT|O_EXCL so two concurrent processes cannot select and write the same filename:
    the loser gets FileExistsError and retries with a numeric suffix.
    """
    base = f"{date}.original-{_compact(stamp)}"
    n = 0
    while True:
        name = f"{base}.md" if n == 0 else f"{base}.{n}.md"
        candidate = d / name
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            n += 1
            if n > 10_000:  # defensive bound; practically unreachable
                raise
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        return candidate


def write_report(report: DailyReport, reports_dir: Path | str = DEFAULT_REPORTS_DIR, *,
                 regenerate: bool = False) -> Path:
    """Write reports/daily/YYYY-MM-DD.md. Never silently overwrites (report OR backup)."""
    d = Path(reports_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{report.date}.md"
    if path.exists():
        if not regenerate:
            raise FileExistsError(
                f"report already exists: {path} (pass regenerate=True to preserve + replace)")
        stamp = report.generated_at or _utc_stamp()
        backup = _write_backup_exclusive(d, report.date, stamp, path.read_text())
        report.regenerated_at = stamp
        report.preserved_original = backup.name
    path.write_text(report.to_markdown())
    return path
