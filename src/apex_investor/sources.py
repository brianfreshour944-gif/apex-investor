"""M2 source policy: allowlist, URL validation and held-ticker selection (Phase 2).

PURE, OFFLINE module: it imports NO network library (`urllib`, `socket`, `ssl`, `http`) and performs
NO I/O. It answers policy questions — "is this URL allowed?", "is this ticker held?" — that
`fetch.py` and `ingest.py` enforce. Keeping policy here means the rules are unit-testable without a
network, and the network layer cannot silently invent a different rule.

Security posture (all fail closed):
  - exact host equality against an explicit allowlist — NEVER a wildcard or suffix match, so
    `sec.gov` does not approve `www.sec.gov` or `evil-sec.gov`;
  - `https` only — `http`, `file`, `ftp`, `data`, and every other scheme are rejected;
  - userinfo (`user@host`), IP literals, non-ASCII/IDN hosts, explicit ports and trailing-dot
    hosts are rejected (they are all bypasses or ambiguities);
  - a hostname that is syntactically an IP literal is refused here; DNS-based private/loopback
    rejection happens in `fetch.py` at connect time.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

APPROVED_SCHEME = "https"


class SourcePolicyError(Exception):
    """A URL or allowlist configuration violates the M2 source policy (fail closed)."""


@dataclass(frozen=True)
class Allowlist:
    """An explicit, per-ticker allowlist of exact hostnames.

    `domains` are hostnames approved for ANY held ticker (e.g. `sec.gov`).
    `ticker_domains` maps a ticker to hostnames approved only for THAT ticker (company IR sites).
    Matching is exact-equality and case-insensitive; there is no wildcard or suffix behaviour.
    """

    domains: frozenset[str]
    ticker_domains: dict[str, frozenset[str]]

    @classmethod
    def from_config(cls, config: dict) -> Allowlist:
        """Build an Allowlist from parsed JSON: {"domains": [...], "ticker_domains": {...}}."""
        if not isinstance(config, dict):
            raise SourcePolicyError("allowlist config must be a JSON object")
        raw_domains = config.get("domains", [])
        raw_ticker = config.get("ticker_domains", {})
        if not isinstance(raw_domains, list) or not all(isinstance(d, str) for d in raw_domains):
            raise SourcePolicyError("allowlist 'domains' must be a list of strings")
        if not isinstance(raw_ticker, dict):
            raise SourcePolicyError("allowlist 'ticker_domains' must be an object")
        domains = frozenset(_norm_host(d) for d in raw_domains)
        ticker_domains: dict[str, frozenset[str]] = {}
        for ticker, hosts in raw_ticker.items():
            if not isinstance(ticker, str) or not ticker:
                raise SourcePolicyError("ticker_domains keys must be non-empty strings")
            if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
                raise SourcePolicyError(f"ticker_domains[{ticker!r}] must be a list of strings")
            ticker_domains[ticker.upper()] = frozenset(_norm_host(h) for h in hosts)
        return cls(domains=domains, ticker_domains=ticker_domains)

    def allows(self, host: str, ticker: str) -> bool:
        """True only if `host` is an exact approved entry for `ticker` (or globally)."""
        h = _norm_host(host)
        if h in self.domains:
            return True
        return h in self.ticker_domains.get(ticker.upper(), frozenset())


def _norm_host(host: str) -> str:
    """Lowercase a hostname for exact comparison. Rejects anything non-trivial."""
    if not isinstance(host, str) or not host:
        raise SourcePolicyError("host must be a non-empty string")
    if host != host.strip():
        raise SourcePolicyError(f"host has surrounding whitespace: {host!r}")
    h = host.lower()
    if h.endswith("."):
        # A trailing dot (FQDN root) is a distinct host string; reject rather than normalize, so
        # `sec.gov.` cannot be used to slip past an exact-equality check by accident.
        raise SourcePolicyError(f"host must not have a trailing dot: {host!r}")
    if any(ch.isspace() for ch in h):
        raise SourcePolicyError(f"host contains whitespace: {host!r}")
    return h


def _is_ip_literal(host: str) -> bool:
    """True if `host` is an IPv4/IPv6 literal (including bracketed IPv6)."""
    candidate = host
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        return False


def normalize_url(url: str) -> str:
    """Validate and return a canonical `https://host/path` URL, or raise SourcePolicyError.

    Rejects: non-https schemes, missing host, userinfo, explicit ports, IP-literal hosts,
    non-ASCII/IDN hosts, trailing-dot hosts, and control characters.
    """
    if not isinstance(url, str) or not url:
        raise SourcePolicyError("url must be a non-empty string")
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in url):
        raise SourcePolicyError("url must not contain control characters")
    parts = urlsplit(url)
    if parts.scheme.lower() != APPROVED_SCHEME:
        raise SourcePolicyError(f"only {APPROVED_SCHEME} URLs are allowed, got {parts.scheme!r}")
    if not parts.hostname:
        raise SourcePolicyError("url has no host")
    if parts.username is not None or parts.password is not None:
        raise SourcePolicyError("url must not contain userinfo (user@host)")
    try:
        port = parts.port
    except ValueError as e:
        raise SourcePolicyError(f"url has an invalid port: {e}") from e
    if port is not None:
        raise SourcePolicyError("url must not specify a port")
    host = parts.hostname  # already lowercased by urlsplit
    if host != host.strip() or host.endswith("."):
        raise SourcePolicyError(f"url host is malformed: {host!r}")
    if _is_ip_literal(host):
        raise SourcePolicyError("url host must be a hostname, not an IP literal")
    if not host.isascii():
        raise SourcePolicyError("url host must be ASCII (IDN hosts are rejected)")
    # urlsplit lowercases and strips brackets; re-validate the normalized form for whitespace.
    _norm_host(host)
    return parts.geturl()


def extract_links(text: str, base_host: str) -> tuple[str, ...]:
    """Return absolute `https` URLs found in `text` whose host == `base_host` (exact).

    A link inside an approved document is NOT thereby approved (plan §9.2): M2 never follows these
    in this milestone, and this helper deliberately returns only same-host links to make that
    explicit. It exists so a future crawler cannot claim the rule was absent.
    """
    found: list[str] = []
    for match in re.finditer(r"""https?://[^\s"'<>]+""", text):
        raw = match.group(0)
        try:
            parts = urlsplit(raw)
        except ValueError:
            continue
        if parts.scheme.lower() != APPROVED_SCHEME or not parts.hostname:
            continue
        if parts.hostname.lower() == base_host.lower():
            found.append(raw)
    return tuple(found)


def held_tickers(ledger) -> list[str]:
    """Tickers with a CURRENT, nonzero held quantity, from a read-only view of the ledger.

    Uses `ledger.positions`, which already excludes zero and closed positions (a reduce that
    reaches ~0 is clamped and filtered out) and cannot represent a negative quantity. We require
    `pos.shares > 0` explicitly rather than trusting the container, so a future change to the
    ledger cannot silently widen what M2 treats as held. The ledger is never mutated.
    """
    return sorted(tk for tk, pos in ledger.positions.items() if pos.shares > 0)
