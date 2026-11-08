"""M2 tests: ingestion — allowlist, SSRF hardening, redirects, size caps, budget, dedup.

All tests run OFFLINE: the network boundary is an injected fake transport, DNS is an injected
resolver, and the clock/sleep are injected. No socket is ever opened.
"""
from __future__ import annotations

import dataclasses
import gzip
import json
import os
import ssl
from pathlib import Path

import pytest

from apex_investor.docstore import Document, DocumentStore, _hash_text, make_doc_id, new_document
from apex_investor.fetch import (
    MAX_RESPONSE_BYTES,
    Fetcher,
    FetchError,
    PolicyViolation,
    ResponseTooLarge,
    _decode_body,
    _PinnedTLS,
    _RawResponse,
    resolve_and_validate,
)
from apex_investor.ingest import ingest
from apex_investor.ledger import Ledger, Side, Tag, Transaction
from apex_investor.sanitize import SANITIZER_VERSION, sanitize
from apex_investor.sources import Allowlist, SourcePolicyError, held_tickers, normalize_url

GLOBAL = "93.184.216.34"  # a public (globally routable) example address


# ---- helpers ---------------------------------------------------------------

class RecordingTransport:
    """A fake transport: records every request and never opens a socket."""

    def __init__(self, responder, peer_ip=None):
        self.responder = responder
        self.peer_ip = peer_ip
        self.requests: list[dict] = []

    def request(self, host, ip, target, headers):
        self.requests.append({"host": host, "ip": ip, "target": target, "headers": headers})
        resp = self.responder(host, target)
        if self.peer_ip is not None:
            resp = dataclasses.replace(resp, peer_ip=self.peer_ip)
        return resp

    @property
    def hosts(self):
        return [r["host"] for r in self.requests]


def resp(status=200, body=b"<html>ok</html>", ctype="text/html", location=None,
         encoding=None, peer_ip=None):
    headers = {"content-type": ctype}
    if location is not None:
        headers["location"] = location
    if encoding is not None:
        headers["content-encoding"] = encoding
    return _RawResponse(status=status, headers=headers, body=body, peer_ip=peer_ip)


def make_allowlist(domains=("sec.gov", "www.sec.gov"), ticker_domains=None):
    return Allowlist.from_config({"domains": list(domains), "ticker_domains": ticker_domains or {}})


def make_resolver(mapping=None, default=GLOBAL):
    def r(host):
        return list(mapping.get(host, [default])) if mapping else [default]
    return r


def make_fetcher(transport, *, allowlist=None, resolver=None, sleep=None, clock=None):
    return Fetcher(
        allowlist=allowlist or make_allowlist(),
        user_agent="apex-investor/0.1 (test)",
        transport=transport,
        resolver=resolver or make_resolver(),
        sleep=sleep or (lambda _s: None),
        clock=clock or (lambda: 0.0),
    )


def ledger_with(holdings, closed=()):
    txs, cost = [], 0.0
    for tk, sh in holdings.items():
        txs.append(Transaction("2000-01-01", tk, Side.BUY, sh, 100.0))
        cost += sh * 100.0
    for tk in closed:  # buy then fully sell -> net zero -> filtered out of positions
        txs.append(Transaction("2000-01-01", tk, Side.BUY, 10.0, 100.0))
        txs.append(Transaction("2000-01-02", tk, Side.SELL, 10.0, 110.0))
    return Ledger.from_transactions(txs, starting_cash=1000.0 + cost,
                                    tags={tk: Tag.GROWTH for tk in holdings})


# ---- 1-3: held tickers -----------------------------------------------------

def test_held_tickers_selects_only_positive(tmp_path):
    led = ledger_with({"AAPL": 5.0}, closed=["CLOSED"])
    assert held_tickers(led) == ["AAPL"]


def test_ingest_only_fetches_held_tickers(tmp_path):
    led = ledger_with({"AAPL": 5.0}, closed=["CLOSED"])
    store = DocumentStore(tmp_path / "docs")
    transport = RecordingTransport(lambda h, t: resp())
    seeds = {"AAPL": ["https://www.sec.gov/aapl.htm"], "CLOSED": ["https://www.sec.gov/c.htm"],
             "GHOST": ["https://www.sec.gov/g.htm"]}  # GHOST has no position
    result = ingest(led, make_allowlist(), seeds, store, fetcher=make_fetcher(transport))
    assert transport.hosts == ["www.sec.gov"]      # only the held ticker was fetched
    assert len(result.stored) == 1


def test_ingest_does_not_mutate_ledger(tmp_path):
    led = ledger_with({"AAPL": 5.0})
    before_positions = dict(led.positions)
    before_tx = list(led.transactions)
    store = DocumentStore(tmp_path / "docs")
    ingest(led, make_allowlist(), {"AAPL": ["https://www.sec.gov/a.htm"]}, store,
           fetcher=make_fetcher(RecordingTransport(lambda h, t: resp())))
    assert dict(led.positions) == before_positions
    assert list(led.transactions) == before_tx


# ---- 4-6: allowlist --------------------------------------------------------

def test_off_allowlist_domain_not_fetched(tmp_path):
    store = DocumentStore(tmp_path / "docs")
    transport = RecordingTransport(lambda h, t: resp())
    result = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(),
                    {"AAPL": ["https://evil.example/x.htm"]}, store, fetcher=make_fetcher(transport))
    assert transport.requests == []                # never fetched
    assert len(result.failures) == 1


def test_no_wildcard_subdomain_match():
    al = make_allowlist(domains=["sec.gov"])       # only the apex host is approved
    assert al.allows("sec.gov", "AAPL") is True
    assert al.allows("www.sec.gov", "AAPL") is False
    assert al.allows("evil-sec.gov", "AAPL") is False
    assert al.allows("sec.gov.evil.example", "AAPL") is False


def test_per_ticker_ir_domain_only_for_that_ticker(tmp_path):
    al = make_allowlist(domains=["sec.gov"], ticker_domains={"AAPL": ["investor.apple.com"]})
    assert al.allows("investor.apple.com", "AAPL") is True
    assert al.allows("investor.apple.com", "MSFT") is False
    transport = RecordingTransport(lambda h, t: resp())
    result = ingest(ledger_with({"MSFT": 5.0}), al,
                    {"MSFT": ["https://investor.apple.com/x"]}, DocumentStore(tmp_path / "docs"),
                    fetcher=make_fetcher(transport, allowlist=al))
    assert transport.requests == []
    assert len(result.failures) == 1


# ---- 7: URL forms ----------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://www.sec.gov/x",                 # not https
    "https://user@www.sec.gov/x",           # userinfo
    "https://93.184.216.34/x",              # IP literal
    "https://[2606:4700::1111]/x",          # IPv6 literal
    "https://www.sec.gov:8443/x",           # explicit port
    "https://www.sec.gov./x",               # trailing dot
    "ftp://www.sec.gov/x",                  # wrong scheme
])
def test_normalize_url_rejects(url):
    with pytest.raises(SourcePolicyError):
        normalize_url(url)


def test_normalize_url_accepts_https_hostname():
    assert normalize_url("https://www.sec.gov/Archives/x.htm") == "https://www.sec.gov/Archives/x.htm"


# ---- 8: DNS guard ----------------------------------------------------------

@pytest.mark.parametrize("addr", ["127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.1.1",
                                  "0.0.0.0", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1",
                                  "::ffff:10.0.0.1", "64:ff9b::7f00:1", "2002:0a00:0001::1"])
def test_dns_guard_rejects_non_global(addr):
    with pytest.raises(PolicyViolation):
        resolve_and_validate("www.sec.gov", lambda h: [addr])


def test_dns_guard_rejects_mixture_of_public_and_private():
    with pytest.raises(PolicyViolation):
        resolve_and_validate("www.sec.gov", lambda h: ["8.8.8.8", "10.0.0.1"])


def test_dns_guard_accepts_ipv4_mapped_public():
    assert resolve_and_validate("www.sec.gov", lambda h: ["::ffff:8.8.8.8"]) == ["8.8.8.8"]


def test_fetch_rejects_private_resolution_before_connecting():
    transport = RecordingTransport(lambda h, t: resp())
    f = make_fetcher(transport, resolver=make_resolver(default="10.0.0.1"))
    with pytest.raises(PolicyViolation):
        f.fetch("https://www.sec.gov/x", "AAPL")
    assert transport.requests == []


# ---- 9: rebinding / peer pinning ------------------------------------------

def test_peer_mismatch_is_rejected():
    transport = RecordingTransport(lambda h, t: resp(), peer_ip="203.0.113.9")
    f = make_fetcher(transport)
    with pytest.raises(PolicyViolation, match="rebinding"):
        f.fetch("https://www.sec.gov/x", "AAPL")


def test_pinned_tls_uses_hostname_for_sni_and_checks_peer(monkeypatch):
    recorded = {}

    class FakeSock:
        def __init__(self, *a, **k):
            pass
        def settimeout(self, t):
            pass
        def connect(self, addr):
            recorded["connect"] = addr
        def getpeername(self):
            return (GLOBAL, 443)
        def close(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class FakeTLS:
        def settimeout(self, t):
            pass
        def close(self):
            pass

    class FakeCtx:
        check_hostname = True
        verify_mode = ssl.CERT_REQUIRED
        def wrap_socket(self, sock, server_hostname):
            recorded["sni"] = server_hostname
            return FakeTLS()

    monkeypatch.setattr("apex_investor.fetch.socket.socket", lambda *a, **k: FakeSock())
    monkeypatch.setattr("apex_investor.fetch.ssl.create_default_context", lambda: FakeCtx())
    monkeypatch.setattr("apex_investor.fetch._read_http", lambda *a, **k: resp())

    _PinnedTLS("ua", lambda h: [GLOBAL]).request("www.sec.gov", GLOBAL, "/x", {"Host": "www.sec.gov"})
    assert recorded["connect"] == (GLOBAL, 443)          # connected to the pinned IP
    assert recorded["sni"] == "www.sec.gov"              # TLS verified against the HOSTNAME


# ---- 10: proxies ignored ---------------------------------------------------

def test_env_proxy_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    transport = RecordingTransport(lambda h, t: resp())
    store = DocumentStore(tmp_path / "docs")
    result = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(),
                    {"AAPL": ["https://www.sec.gov/a.htm"]}, store, fetcher=make_fetcher(transport))
    assert len(result.stored) == 1                       # went through our transport, not a proxy
    assert transport.hosts == ["www.sec.gov"]


# ---- 11-16: redirects ------------------------------------------------------

def test_off_allowlist_redirect_target_receives_zero_requests():
    def responder(host, target):
        return resp(302, location="https://evil.example/x")
    transport = RecordingTransport(responder)
    f = make_fetcher(transport)
    with pytest.raises(PolicyViolation):
        f.fetch("https://www.sec.gov/x", "AAPL")
    assert "evil.example" not in transport.hosts          # target host got zero requests


def test_redirect_loop_is_aborted():
    def responder(host, target):
        if host == "sec.gov":
            return resp(302, location="https://www.sec.gov/b")
        return resp(302, location="https://sec.gov/a")
    transport = RecordingTransport(responder)
    f = make_fetcher(transport, allowlist=make_allowlist(domains=["sec.gov", "www.sec.gov"]))
    with pytest.raises(FetchError, match="too many redirects"):
        f.fetch("https://sec.gov/a", "AAPL")


def test_too_many_hops_is_aborted():
    def responder(host, target):
        n = int(target.strip("/") or "0")
        return resp(302, location=f"https://www.sec.gov/{n + 1}")
    transport = RecordingTransport(responder)
    f = make_fetcher(transport)
    with pytest.raises(FetchError, match="too many redirects"):
        f.fetch("https://www.sec.gov/0", "AAPL")


def test_missing_location_is_rejected():
    transport = RecordingTransport(lambda h, t: resp(302, location=None))
    f = make_fetcher(transport)
    with pytest.raises(FetchError, match="missing/empty Location"):
        f.fetch("https://www.sec.gov/x", "AAPL")


def test_https_to_http_downgrade_is_rejected():
    transport = RecordingTransport(lambda h, t: resp(302, location="http://www.sec.gov/x"))
    f = make_fetcher(transport)
    with pytest.raises(PolicyViolation, match="downgrade"):
        f.fetch("https://www.sec.gov/x", "AAPL")


def test_approved_to_approved_redirect_is_allowed_and_stored(tmp_path):
    def responder(host, target):
        if host == "www.sec.gov":
            return resp(302, location="https://sec.gov/final.htm")
        return resp(200, body=b"<html>final</html>")
    transport = RecordingTransport(responder)
    store = DocumentStore(tmp_path / "docs")
    result = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(),
                    {"AAPL": ["https://www.sec.gov/start.htm"]}, store,
                    fetcher=make_fetcher(transport))
    assert len(result.stored) == 1
    assert result.requested_to_final == {"https://www.sec.gov/start.htm": "https://sec.gov/final.htm"}
    doc = store.get(result.stored[0])
    assert doc.source_url == "https://sec.gov/final.htm"   # final URL stored, not the original host


# ---- 17-19: size limits ----------------------------------------------------

def test_oversized_raw_body_is_rejected(tmp_path):
    big = b"a" * (MAX_RESPONSE_BYTES + 1)
    transport = RecordingTransport(lambda h, t: resp(200, body=big))
    f = make_fetcher(transport)
    with pytest.raises(ResponseTooLarge):
        f.fetch("https://www.sec.gov/x", "AAPL")


def test_gzip_bomb_is_rejected_on_decompressed_size():
    bomb = gzip.compress(b"\x00" * (MAX_RESPONSE_BYTES + 1024))
    assert len(bomb) < MAX_RESPONSE_BYTES            # small on the wire...
    with pytest.raises(ResponseTooLarge):
        _decode_body("gzip", bomb)                   # ...but rejected once decompressed


def test_unsupported_content_encoding_is_rejected():
    with pytest.raises(FetchError, match="content-encoding"):
        _decode_body("br", b"whatever")


def test_small_gzip_body_is_accepted():
    payload = b"<html>ok</html>"
    assert _decode_body("gzip", gzip.compress(payload)) == payload


def test_chunked_body_is_decoded():
    from apex_investor.fetch import _dechunk
    assert _dechunk(b"5\r\nhello\r\n0\r\n\r\n") == b"hello"


def test_malformed_chunked_body_is_rejected():
    from apex_investor.fetch import _dechunk
    with pytest.raises(FetchError, match="chunked"):
        _dechunk(b"zz\r\nhello\r\n")


def test_no_hostname_fallback_when_pinning_tls_fails(monkeypatch):
    """If the pinned TLS handshake fails, the fetcher must NOT retry via a hostname connection."""
    connects = []

    class FakeSock:
        def __init__(self, *a, **k):
            pass
        def settimeout(self, t):
            pass
        def connect(self, addr):
            connects.append(addr)
        def getpeername(self):
            return (GLOBAL, 443)
        def close(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class FailCtx:
        check_hostname = True
        verify_mode = ssl.CERT_REQUIRED
        def wrap_socket(self, sock, server_hostname):
            raise ssl.SSLError("certificate verify failed")

    def no_getaddrinfo(*a, **k):
        raise AssertionError("hostname-based fallback connection attempted!")

    monkeypatch.setattr("apex_investor.fetch.socket.socket", lambda *a, **k: FakeSock())
    monkeypatch.setattr("apex_investor.fetch.ssl.create_default_context", lambda: FailCtx())
    monkeypatch.setattr("apex_investor.fetch.socket.getaddrinfo", no_getaddrinfo)

    fetcher = make_fetcher(transport=None)           # exercises the real _PinnedTLS
    with pytest.raises(FetchError):
        fetcher.fetch("https://www.sec.gov/x", "AAPL")
    assert connects == [(GLOBAL, 443)]               # one attempt, to the pinned IP only


# ---- 20: content types -----------------------------------------------------

def test_unsupported_content_type_produces_no_document(tmp_path):
    store = DocumentStore(tmp_path / "docs")
    transport = RecordingTransport(lambda h, t: resp(200, body=b"%PDF-1.4", ctype="application/pdf"))
    result = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(),
                    {"AAPL": ["https://www.sec.gov/x.pdf"]}, store, fetcher=make_fetcher(transport))
    assert result.stored == []
    assert len(result.failures) == 1
    assert store.list_doc_ids() == []


# ---- 22: network failure ---------------------------------------------------

def test_network_failure_yields_no_docs_not_a_crash(tmp_path):
    def boom(host, target):
        raise OSError("connection reset")
    transport = RecordingTransport(boom)
    store = DocumentStore(tmp_path / "docs")
    result = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(),
                    {"AAPL": ["https://www.sec.gov/a.htm"]}, store, fetcher=make_fetcher(transport))
    assert result.stored == []
    assert len(result.failures) == 1
    assert result.failures[0]["reason"].startswith("FetchError")


# ---- 23-27: budget, dedup, partial, result shape ---------------------------

def test_budget_stops_before_the_eleventh_fetch(tmp_path):
    store = DocumentStore(tmp_path / "docs")
    urls = [f"https://www.sec.gov/doc{i}.htm" for i in range(12)]
    transport = RecordingTransport(lambda h, t: resp(200, body=f"<html>{t}</html>".encode()))
    result = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(), {"AAPL": urls}, store,
                    fetcher=make_fetcher(transport), budget=10)
    assert len(result.stored) == 10
    assert result.incomplete is True
    assert len(transport.requests) == 10            # the 11th fetch was never issued


def test_rerun_is_idempotent_duplicate(tmp_path):
    store = DocumentStore(tmp_path / "docs")
    transport = RecordingTransport(lambda h, t: resp(200, body=b"<html>same</html>"))
    seeds = {"AAPL": ["https://www.sec.gov/a.htm"]}
    first = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(), seeds, store,
                   fetcher=make_fetcher(transport))
    second = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(), seeds, store,
                    fetcher=make_fetcher(transport))
    assert len(first.stored) == 1 and second.stored == []
    assert second.duplicates == first.stored


def test_same_id_different_content_is_a_failure_not_a_crash(tmp_path):
    store = DocumentStore(tmp_path / "docs")
    url = "https://www.sec.gov/a.htm"
    raw = b"<html>original</html>"
    # Pre-seed a DIFFERENT document under the same doc_id (same url + raw_hash).
    doc_id = make_doc_id(url, new_document(
        ticker="AAPL", kind="filing", source_url=url, approved_domain="sec.gov",
        retrieved_at="2026-11-07", raw=raw, text="other", sanitizer_version=SANITIZER_VERSION).raw_hash)
    tampered = Document(
        doc_id=doc_id, ticker="AAPL", kind="filing", source_url=url, approved_domain="sec.gov",
        retrieved_at="2026-11-07", published_at=None,
        raw_hash=new_document(ticker="AAPL", kind="filing", source_url=url, approved_domain="sec.gov",
                              retrieved_at="2026-11-07", raw=raw, text="x",
                              sanitizer_version=SANITIZER_VERSION).raw_hash,
        content_hash=_hash_text("tampered"), sanitizer_version=SANITIZER_VERSION,
        title="t", text="tampered")
    store.put(tampered)
    transport = RecordingTransport(lambda h, t: resp(200, body=raw))
    result = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(), {"AAPL": [url]}, store,
                    fetcher=make_fetcher(transport))
    assert result.stored == []
    assert len(result.failures) == 1


def test_sanitize_failure_stores_nothing(tmp_path):
    store = DocumentStore(tmp_path / "docs")
    transport = RecordingTransport(lambda h, t: resp(200, body=b"\xff\xfe not utf8", ctype="text/html"))
    result = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(),
                    {"AAPL": ["https://www.sec.gov/a.htm"]}, store, fetcher=make_fetcher(transport))
    assert result.stored == []
    assert store.list_doc_ids() == []
    assert len(result.failures) == 1


def test_result_distinguishes_all_outcomes(tmp_path):
    store = DocumentStore(tmp_path / "docs")
    def responder(host, target):
        if target == "/fail":
            return resp(500)
        return resp(200, body=b"<html>ok</html>")
    transport = RecordingTransport(responder)
    led = ledger_with({"AAPL": 5.0, "NOSEED": 5.0}, closed=["CLOSED"])
    seeds = {"AAPL": ["https://www.sec.gov/ok", "https://www.sec.gov/fail"]}
    result = ingest(led, make_allowlist(), seeds, store, fetcher=make_fetcher(transport))
    assert len(result.stored) == 1
    assert len(result.failures) == 1
    assert result.tickers_skipped == ["NOSEED"]      # held but no seed URL
    assert "stored=1" in result.summary()


# ---- 29: rate limit --------------------------------------------------------

def test_rate_limit_spaces_requests_per_host():
    sleeps = []
    times = iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    transport = RecordingTransport(lambda h, t: resp(200, body=b"<html>ok</html>"))
    f = make_fetcher(transport, sleep=sleeps.append, clock=lambda: next(times))
    f.fetch("https://www.sec.gov/a", "AAPL")
    f.fetch("https://www.sec.gov/b", "AAPL")
    assert any(s > 0 for s in sleeps)                # second request to the host was delayed


# ---- 30: provenance --------------------------------------------------------

def test_provenance_dual_hash_is_correct(tmp_path):
    store = DocumentStore(tmp_path / "docs")
    raw = b"<html><body>Hello <b>world</b></body></html>"
    transport = RecordingTransport(lambda h, t: resp(200, body=raw, ctype="text/html"))
    result = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(),
                    {"AAPL": ["https://www.sec.gov/a.htm"]}, store, fetcher=make_fetcher(transport))
    doc = store.get(result.stored[0])
    text = sanitize(raw, content_type="text/html")
    assert doc.text == text
    assert doc.content_hash == _hash_text(text)
    assert doc.raw_hash != doc.content_hash         # sanitizer changed the bytes


# ---- 28: isolation ---------------------------------------------------------

def test_sources_module_has_no_network_imports():
    src = Path("src/apex_investor/sources.py").read_text()
    for forbidden in ("import urllib", "import socket", "import ssl", "import http", "import requests"):
        assert forbidden not in src


# ---- corrections: content-identity classification --------------------------

def _store_one(tmp_path, *, raw=b"<html>body</html>", ticker="AAPL", kind="filing",
               retrieved_at="2026-11-07", title_override=None, url="https://www.sec.gov/a.htm"):
    """Store one document directly, returning (store, doc)."""
    store = DocumentStore(tmp_path / "docs")
    text = sanitize(raw, content_type="text/html")
    doc = new_document(ticker=ticker, kind=kind, source_url=url, approved_domain="sec.gov",
                       retrieved_at=retrieved_at, raw=raw, text=text,
                       sanitizer_version=SANITIZER_VERSION,
                       title=title_override if title_override is not None else "orig")
    store.put(doc)
    return store, doc


def test_same_content_different_retrieval_timestamp_is_duplicate(tmp_path):
    # Re-ingest the SAME source on a LATER day: doc_id and content match, retrieved_at differs.
    store = DocumentStore(tmp_path / "docs")
    transport = RecordingTransport(lambda h, t: resp(200, body=b"<html>same</html>"))
    seeds = {"AAPL": ["https://www.sec.gov/a.htm"]}
    first = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(), seeds, store,
                   fetcher=make_fetcher(transport), today="2026-11-07")
    second = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(), seeds, store,
                    fetcher=make_fetcher(transport), today="2026-11-08")   # different day
    assert len(first.stored) == 1
    assert second.stored == []
    assert second.duplicates == first.stored        # duplicate, NOT an integrity conflict
    assert second.failures == []


def test_same_content_different_title_is_duplicate(tmp_path):
    store, doc = _store_one(tmp_path, title_override="original title")
    # A second document with identical content fields but a different title.
    same = new_document(ticker="AAPL", kind="filing", source_url=doc.source_url,
                        approved_domain="sec.gov", retrieved_at=doc.retrieved_at,
                        raw=b"<html>body</html>", text=doc.text,
                        sanitizer_version=SANITIZER_VERSION, title="DIFFERENT title")
    from apex_investor.ingest import _same_content
    assert same.doc_id == doc.doc_id
    assert _same_content(store.get(same.doc_id), same) is True


def test_different_content_hash_is_conflict(tmp_path):
    store, doc = _store_one(tmp_path, raw=b"<html>original</html>")
    from apex_investor.ingest import _same_content
    changed = new_document(ticker="AAPL", kind="filing", source_url=doc.source_url,
                           approved_domain="sec.gov", retrieved_at=doc.retrieved_at,
                           raw=b"<html>original</html>", text="tampered text",
                           sanitizer_version=SANITIZER_VERSION)
    assert changed.doc_id == doc.doc_id              # same url+raw => same id
    assert _same_content(store.get(changed.doc_id), changed) is False


def test_different_ticker_or_kind_is_conflict(tmp_path):
    store, doc = _store_one(tmp_path)
    from apex_investor.ingest import _same_content
    other_ticker = new_document(ticker="MSFT", kind="filing", source_url=doc.source_url,
                                approved_domain="sec.gov", retrieved_at=doc.retrieved_at,
                                raw=b"<html>body</html>", text=doc.text,
                                sanitizer_version=SANITIZER_VERSION)
    other_kind = new_document(ticker="AAPL", kind="earnings", source_url=doc.source_url,
                              approved_domain="sec.gov", retrieved_at=doc.retrieved_at,
                              raw=b"<html>body</html>", text=doc.text,
                              sanitizer_version=SANITIZER_VERSION)
    assert _same_content(store.get(doc.doc_id), other_ticker) is False
    assert _same_content(store.get(doc.doc_id), other_kind) is False


def test_existing_document_unchanged_on_duplicate_and_conflict(tmp_path):
    # Same content, different day -> duplicate; the stored file must be byte-identical afterwards.
    store = DocumentStore(tmp_path / "docs")
    transport = RecordingTransport(lambda h, t: resp(200, body=b"<html>stable</html>"))
    seeds = {"AAPL": ["https://www.sec.gov/a.htm"]}
    first = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(), seeds, store,
                   fetcher=make_fetcher(transport), today="2026-11-07")
    doc_id = first.stored[0]
    before = (store._path(doc_id)).read_bytes()
    ingest(ledger_with({"AAPL": 5.0}), make_allowlist(), seeds, store,
           fetcher=make_fetcher(transport), today="2026-11-09")
    after = (store._path(doc_id)).read_bytes()
    assert before == after                            # never overwritten
    assert store.get(doc_id).retrieved_at == "2026-11-07"   # original retained


def test_conflict_does_not_overwrite_existing(tmp_path):
    # Store a document, then run ingest against the SAME url+raw but with a stored doc whose TEXT
    # differs -> same doc_id, different content identity -> integrity conflict, no overwrite.
    store = DocumentStore(tmp_path / "docs")
    url = "https://www.sec.gov/a.htm"
    raw = b"<html>original</html>"
    raw_hash = new_document(ticker="AAPL", kind="filing", source_url=url, approved_domain="sec.gov",
                            retrieved_at="2026-11-07", raw=raw, text="x",
                            sanitizer_version=SANITIZER_VERSION).raw_hash
    stored = Document(
        doc_id=make_doc_id(url, raw_hash), ticker="AAPL", kind="filing", source_url=url,
        approved_domain="sec.gov", retrieved_at="2026-11-07", published_at=None,
        raw_hash=raw_hash, content_hash=_hash_text("tampered"),
        sanitizer_version=SANITIZER_VERSION, title="t", text="tampered")
    store.put(stored)
    before = store._path(stored.doc_id).read_bytes()

    transport = RecordingTransport(lambda h, t: resp(200, body=raw))
    result = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(), {"AAPL": [url]}, store,
                    fetcher=make_fetcher(transport))
    assert result.stored == []
    assert len(result.failures) == 1
    assert "different content" in result.failures[0]["reason"]
    assert store._path(stored.doc_id).read_bytes() == before   # never overwritten


# ---- corrections: manifest -------------------------------------------------

def _manifest_worker(args):
    """Module-level (picklable) worker for the concurrent-append test."""
    from apex_investor.ingest import IngestResult, _write_manifest
    manifest_path, i = args
    res = IngestResult()
    _write_manifest(manifest_path, f"https://www.sec.gov/{i}", None, "AAPL", "stored",
                    "2026-11-07", res)
    return res.manifest_failures


def _read_manifest(path):
    from apex_investor.ingest import read_manifest
    return read_manifest(path)[0]


def _manifest_problems(path):
    from apex_investor.ingest import read_manifest
    return read_manifest(path)[1]


def test_manifest_records_each_outcome(tmp_path):
    store = DocumentStore(tmp_path / "docs")
    manifest = tmp_path / "docs" / "manifest.jsonl"
    # Pre-store a doc so the re-fetch becomes a duplicate.
    ingest(ledger_with({"AAPL": 5.0}), make_allowlist(),
           {"AAPL": ["https://www.sec.gov/dup"]}, store,
           fetcher=make_fetcher(RecordingTransport(lambda h, t: resp(200, body=b"<html>d</html>"))),
           today="2026-11-07")

    def responder(host, target):
        if target == "/ok":
            return resp(200, body=b"<html>ok</html>")
        if target == "/fail":
            return resp(500)
        if target == "/pdf":
            return resp(200, body=b"%PDF", ctype="application/pdf")
        return resp(200, body=b"<html>d</html>")     # /dup
    transport = RecordingTransport(responder)
    seeds = {"AAPL": ["https://www.sec.gov/ok", "https://www.sec.gov/dup",
                      "https://www.sec.gov/fail", "https://www.sec.gov/pdf"]}
    result = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(), seeds, store,
                    fetcher=make_fetcher(transport), today="2026-11-07", manifest=manifest)
    records = _read_manifest(manifest)
    outcomes = {r["requested_url"]: r["outcome"] for r in records}
    assert outcomes["https://www.sec.gov/ok"] == "stored"
    assert outcomes["https://www.sec.gov/dup"] == "duplicate"
    assert outcomes["https://www.sec.gov/fail"] == "fetch_error"
    assert outcomes["https://www.sec.gov/pdf"] == "fetch_error"   # unsupported content type
    assert result.manifest_failures == []


def test_manifest_records_sanitize_error(tmp_path):
    store = DocumentStore(tmp_path / "docs")
    manifest = tmp_path / "docs" / "manifest.jsonl"
    transport = RecordingTransport(lambda h, t: resp(200, body=b"\xff\xfe not utf8", ctype="text/html"))
    ingest(ledger_with({"AAPL": 5.0}), make_allowlist(),
           {"AAPL": ["https://www.sec.gov/a"]}, store, fetcher=make_fetcher(transport),
           manifest=manifest)
    rec = _read_manifest(manifest)[0]
    assert rec["outcome"] == "sanitize_error"
    assert rec["final_url"] == "https://www.sec.gov/a"


def test_manifest_records_policy_refusal(tmp_path):
    store = DocumentStore(tmp_path / "docs")
    manifest = tmp_path / "docs" / "manifest.jsonl"
    result = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(),
                    {"AAPL": ["https://evil.example/x"]}, store,
                    fetcher=make_fetcher(RecordingTransport(lambda h, t: resp())),
                    manifest=manifest)
    rec = _read_manifest(manifest)[0]
    assert rec["outcome"] == "policy_refused"
    assert result.failures and "PolicyViolation" in result.failures[0]["reason"]
    assert rec["final_url"] is None
    assert set(rec) == {"requested_url", "final_url", "ticker", "outcome", "retrieved_at"}


def test_manifest_is_append_only(tmp_path):
    store = DocumentStore(tmp_path / "docs")
    manifest = tmp_path / "docs" / "manifest.jsonl"
    seeds = {"AAPL": ["https://www.sec.gov/a"]}
    transport = RecordingTransport(lambda h, t: resp(200, body=b"<html>a</html>"))
    ingest(ledger_with({"AAPL": 5.0}), make_allowlist(), seeds, store,
           fetcher=make_fetcher(transport), manifest=manifest)
    ingest(ledger_with({"AAPL": 5.0}), make_allowlist(), seeds, store,
           fetcher=make_fetcher(transport), manifest=manifest)
    lines = [ln for ln in manifest.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2                            # appended, not rewritten
    assert _read_manifest(manifest)[0]["outcome"] == "stored"
    assert _read_manifest(manifest)[1]["outcome"] == "duplicate"


def test_manifest_records_conflict(tmp_path):
    # A same-id/different-content situation must be recorded with outcome "conflict".
    store = DocumentStore(tmp_path / "docs")
    manifest = tmp_path / "docs" / "manifest.jsonl"
    url = "https://www.sec.gov/c"
    raw = b"<html>c</html>"
    raw_hash = new_document(ticker="AAPL", kind="filing", source_url=url, approved_domain="sec.gov",
                            retrieved_at="2026-11-07", raw=raw, text="x",
                            sanitizer_version=SANITIZER_VERSION).raw_hash
    store.put(Document(doc_id=make_doc_id(url, raw_hash), ticker="AAPL", kind="filing",
                       source_url=url, approved_domain="sec.gov", retrieved_at="2026-11-07",
                       published_at=None, raw_hash=raw_hash, content_hash=_hash_text("TAMPERED"),
                       sanitizer_version=SANITIZER_VERSION, title="t", text="TAMPERED"))
    transport = RecordingTransport(lambda h, t: resp(200, body=raw))
    result = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(), {"AAPL": [url]}, store,
                    fetcher=make_fetcher(transport), manifest=manifest)
    rec = _read_manifest(manifest)[0]
    assert rec["outcome"] == "conflict"
    assert rec["requested_url"] == url
    assert result.failures and "different content" in result.failures[0]["reason"]
    assert result.stored == []


def test_read_manifest_skips_truncated_final_line(tmp_path):
    from apex_investor.ingest import read_manifest
    manifest = tmp_path / "manifest.jsonl"
    good = '{"outcome": "stored", "requested_url": "https://www.sec.gov/a"}\n'
    manifest.write_text(good + '{"outcome": "dupl')      # second line cut short (no newline)
    records, problems = read_manifest(manifest)
    assert len(records) == 1                              # truncated tail skipped, not an error
    assert records[0]["outcome"] == "stored"
    assert problems and "truncated final record" in problems[0]


def test_read_manifest_reports_corrupt_interior_line(tmp_path):
    from apex_investor.ingest import read_manifest
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text('{"outcome": "stored"}\n' + "NOT JSON\n" + '{"outcome": "duplicate"}\n')
    records, problems = read_manifest(manifest)
    assert [r["outcome"] for r in records] == ["stored", "duplicate"]
    assert problems == ["corrupt record at line 2"]       # reported, not silently dropped


def test_read_manifest_handles_missing_file(tmp_path):
    from apex_investor.ingest import read_manifest
    assert read_manifest(tmp_path / "nope.jsonl") == ([], [])


def test_append_repairs_truncated_tail_and_preserves_prior_records(tmp_path):
    store = DocumentStore(tmp_path / "docs")
    manifest = tmp_path / "docs" / "manifest.jsonl"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    # A prior process left one COMPLETE record plus a truncated fragment (no newline).
    manifest.write_text('{"outcome": "stored", "requested_url": "https://www.sec.gov/prior"}\n'
                        '{"outcome": "dupl')
    transport = RecordingTransport(lambda h, t: resp(200, body=b"<html>ok</html>"))
    result = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(),
                    {"AAPL": ["https://www.sec.gov/new"]}, store, fetcher=make_fetcher(transport),
                    manifest=manifest)
    assert result.manifest_failures == []
    records = _read_manifest(manifest)
    urls = [r["requested_url"] for r in records]
    assert urls == ["https://www.sec.gov/prior", "https://www.sec.gov/new"]  # both readable
    assert _manifest_problems(manifest) == []             # tail repaired, no problems remain


def test_short_write_is_reported_and_line_is_not_left_partial(tmp_path, monkeypatch):
    import apex_investor.ingest as ing
    store = DocumentStore(tmp_path / "docs")
    manifest = tmp_path / "docs" / "manifest.jsonl"

    real_write = ing._os_write

    def short_write(fd, data):
        return real_write(fd, data[: max(1, len(data) // 2)])   # write only half, no exception

    monkeypatch.setattr(ing, "_os_write", short_write)
    transport = RecordingTransport(lambda h, t: resp(200, body=b"<html>ok</html>"))
    result = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(),
                    {"AAPL": ["https://www.sec.gov/a"]}, store, fetcher=make_fetcher(transport),
                    manifest=manifest)
    assert len(result.stored) == 1                        # the document was still stored
    assert len(result.manifest_failures) == 1             # the short write is reported
    assert "short write" in result.manifest_failures[0]["reason"]
    assert _read_manifest(manifest) == []                 # no partial line left behind
    assert _manifest_problems(manifest) == []             # the file is empty and clean


def test_concurrent_appends_produce_valid_records(tmp_path):
    import multiprocessing as mp
    manifest = tmp_path / "manifest.jsonl"

    ctx = mp.get_context("fork")
    with ctx.Pool(4) as pool:
        failures = pool.map(_manifest_worker, [(str(manifest), i) for i in range(40)])
    assert all(f == [] for f in failures)                 # no writer reported a failure
    records = _read_manifest(manifest)
    assert len(records) == 40                             # every record landed intact
    assert _manifest_problems(manifest) == []
    assert {r["requested_url"] for r in records} == {f"https://www.sec.gov/{i}" for i in range(40)}


def test_repair_scans_multiple_blocks_on_large_manifest(tmp_path):
    # Force a manifest larger than one 64 KiB scan block, ending in a truncated fragment.
    from apex_investor.ingest import IngestResult, _write_manifest
    manifest = tmp_path / "manifest.jsonl"
    good = b"".join(
        (json.dumps({"outcome": "stored", "requested_url": f"https://www.sec.gov/{i}"}) + "\n").encode()
        for i in range(4000))
    assert len(good) > 2 * 65536                        # spans several blocks
    manifest.write_bytes(good + b'{"outcome": "dupl')   # truncated tail, no newline

    res = IngestResult()
    _write_manifest(manifest, "https://www.sec.gov/final", None, "AAPL", "stored", "2026-11-07", res)
    assert res.manifest_failures == []
    from apex_investor.ingest import read_manifest
    records, problems = read_manifest(manifest)
    assert len(records) == 4001                         # 4000 preserved + 1 new
    assert problems == []                               # tail repaired, file structurally valid
    assert records[-1]["requested_url"] == "https://www.sec.gov/final"
    assert records[0]["requested_url"] == "https://www.sec.gov/0"


def test_manifest_reports_unsupported_platform(tmp_path, monkeypatch):
    import apex_investor.ingest as ing
    monkeypatch.setattr(ing, "fcntl", None)
    assert ing.manifest_locking_supported() is False
    store = DocumentStore(tmp_path / "docs")
    manifest = tmp_path / "docs" / "manifest.jsonl"
    transport = RecordingTransport(lambda h, t: resp(200, body=b"<html>ok</html>"))
    result = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(),
                    {"AAPL": ["https://www.sec.gov/a"]}, store, fetcher=make_fetcher(transport),
                    manifest=manifest)
    assert len(result.stored) == 1                       # the document is still stored
    assert len(result.manifest_failures) == 1
    assert "unavailable on this platform" in result.manifest_failures[0]["reason"]
    assert not manifest.exists()                         # no partial manifest written


def test_repair_survives_short_reads(tmp_path, monkeypatch):
    # Force os.read to return at most 5 bytes per call; recovery must still find the LAST newline
    # and keep every complete record.
    import apex_investor.ingest as ing
    manifest = tmp_path / "manifest.jsonl"
    records = [
        {"outcome": "stored", "requested_url": f"https://www.sec.gov/{i}"} for i in range(50)
    ]
    good = b"".join((json.dumps(r) + "\n").encode() for r in records)
    manifest.write_bytes(good + b'{"outcome": "dupl')   # truncated fragment, no newline

    real_read = ing._os_read

    def short_read(fd, n):
        return real_read(fd, min(n, 5))                 # never fill the requested length

    monkeypatch.setattr(ing, "_os_read", short_read)

    res = ing.IngestResult()
    ing._write_manifest(manifest, "https://www.sec.gov/final", None, "AAPL", "stored", "2026-11-07", res)
    assert res.manifest_failures == []

    monkeypatch.setattr(ing, "_os_read", real_read)     # read normally for the assertion
    got, problems = ing.read_manifest(manifest)
    assert problems == []
    assert len(got) == 51                               # 50 complete records preserved + 1 new
    assert got[-1]["requested_url"] == "https://www.sec.gov/final"
    assert got[0]["requested_url"] == "https://www.sec.gov/0"


def test_recovery_failure_reports_and_skips_append(tmp_path, monkeypatch):
    # Force _read_exactly to hit EOF early (file shortened concurrently): recovery must FAIL, the
    # failure must be reported, and the append must be skipped with the file left untouched.
    import apex_investor.ingest as ing
    manifest = tmp_path / "manifest.jsonl"
    original = (b'{"outcome": "stored", "requested_url": "https://www.sec.gov/a"}\n'
                b'{"outcome": "dupl')                    # one complete record + truncated fragment
    manifest.write_bytes(original)

    monkeypatch.setattr(ing, "_os_read", lambda fd, n: b"")   # every read returns EOF

    res = ing.IngestResult()
    ing._write_manifest(manifest, "https://www.sec.gov/new", None, "AAPL", "stored", "2026-11-07", res)

    assert len(res.manifest_failures) == 1                    # recovery failure reported
    assert "recovery failed" in res.manifest_failures[0]["reason"]
    assert manifest.read_bytes() == original                 # no append, no truncation


def test_manifest_write_failure_is_reported_not_fatal(tmp_path, monkeypatch):
    store = DocumentStore(tmp_path / "docs")
    manifest = tmp_path / "docs" / "manifest.jsonl"
    # Make the manifest open raise, so the append fails.
    real_os_open = os.open

    def flaky_os_open(path, flags, *a, **k):
        if str(path).endswith("manifest.jsonl"):
            raise OSError("disk full")
        return real_os_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", flaky_os_open)
    transport = RecordingTransport(lambda h, t: resp(200, body=b"<html>ok</html>"))
    result = ingest(ledger_with({"AAPL": 5.0}), make_allowlist(),
                    {"AAPL": ["https://www.sec.gov/a"]}, store, fetcher=make_fetcher(transport),
                    manifest=manifest)
    assert len(result.stored) == 1                    # the document WAS stored
    assert len(result.manifest_failures) == 1         # and the manifest failure is reported
    assert store.get(result.stored[0]) is not None    # stored document intact
    assert not manifest.exists()                      # nothing partial was left behind


def test_no_manifest_when_not_requested(tmp_path):
    store = DocumentStore(tmp_path / "docs")
    transport = RecordingTransport(lambda h, t: resp(200, body=b"<html>ok</html>"))
    ingest(ledger_with({"AAPL": 5.0}), make_allowlist(),
           {"AAPL": ["https://www.sec.gov/a"]}, store, fetcher=make_fetcher(transport))
    assert not (tmp_path / "docs" / "manifest.jsonl").exists()
