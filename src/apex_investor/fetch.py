"""M2 network fetch: allowlist-checked, SSRF-hardened HTTPS retrieval (Phase 2).

This is the ONLY module in M2 that touches the network. It is written so the security decisions are
testable offline: DNS resolution, connection and sleeping are injected, so tests drive the logic
without a socket.

Security posture (fail closed):
  - every request and every redirect hop is re-checked against the allowlist (exact host) and the
    DNS guard BEFORE the next request is issued;
  - the DNS guard requires EVERY resolved address to be globally routable (`is_global`); a host
    that resolves to any private/loopback/link-local/reserved/multicast/unspecified address — or a
    mix of public and non-public addresses — is rejected. IPv4-mapped IPv6 addresses are unwrapped
    and checked as IPv4;
  - the connection is PINNED to the validated IP address (no second, unvalidated resolution), while
    the `Host` header and TLS SNI/hostname verification use the HOSTNAME — the certificate is
    verified against the hostname, never the IP;
  - after connecting, the peer address is asserted to equal the pinned address;
  - environment proxies are ignored (no proxy handler, proxy env vars not consulted);
  - redirects are followed MANUALLY with a hop cap; an off-allowlist target receives zero requests;
  - responses are capped at MAX_RESPONSE_BYTES on BOTH the compressed input and the DECOMPRESSED
    output, so a small compressed body cannot expand past the limit (decompression bomb);
  - only the content types the M1b sanitizer supports are accepted.

The transport is injectable: `Fetcher(transport=...)`. The default transport is a real pinned-IP
TLS transport; tests pass a fake so no socket is ever opened.
"""
from __future__ import annotations

import ipaddress
import socket
import ssl
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from .sources import Allowlist, SourcePolicyError, normalize_url

MAX_RESPONSE_BYTES = 5 * 1024 * 1024          # 5 MiB, applied to compressed AND decompressed bytes
MAX_REDIRECTS = 5
CONNECT_TIMEOUT = 10.0                         # seconds
READ_TIMEOUT = 20.0                            # seconds
DEFAULT_RATE_LIMIT_SECONDS = 1.0               # one request per second per host (D7)
SUPPORTED_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
SUPPORTED_CONTENT_ENCODINGS = frozenset({"", "identity", "gzip", "deflate"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class FetchError(Exception):
    """A fetch could not be completed safely (network, policy, size, or content-type failure)."""


class PolicyViolation(FetchError):
    """The request was refused by policy (allowlist, scheme, or DNS guard)."""


class ResponseTooLarge(FetchError):
    """The response exceeded MAX_RESPONSE_BYTES (compressed or decompressed)."""


@dataclass(frozen=True)
class Response:
    """A fetched response, already size-checked and content-type-checked."""

    status: int
    headers: dict[str, str]                 # lowercased keys
    body: bytes                             # decompressed body (<= MAX_RESPONSE_BYTES)
    final_url: str

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type", "") or "").split(";")[0].strip().lower()


@dataclass(frozen=True)
class FetchedDocument:
    """The outcome of a successful allowlisted fetch."""

    requested_url: str
    final_url: str
    raw: bytes
    content_type: str


class Transport(Protocol):
    """The injectable network boundary.

    `request(host, ip, path, headers)` must connect to `ip` (already validated as globally
    routable) while presenting `host` for the `Host` header and TLS SNI/verification. It returns
    a `Response` with an UNREAD stream (or already-read bytes for a fake). Real implementations
    return a raw socket-like object; the default transport below returns a `_RawResponse`.
    """

    def request(self, host: str, ip: str, target: str, headers: dict[str, str]) -> _RawResponse:
        ...


@dataclass
class _RawResponse:
    """A minimal HTTP response as produced by a transport, before size/decoding checks."""

    status: int
    headers: dict[str, str]
    body: bytes
    peer_ip: str | None = None


def resolve_and_validate(host: str, resolver: Callable[[str], list[str]] | None = None) -> list[str]:
    """Resolve `host` and return its addresses ONLY if every one is globally routable.

    Fail closed: any non-global address (private, loopback, link-local, reserved, multicast,
    unspecified, or a mixture) rejects the whole host. Addresses that EMBED an IPv4 address
    (IPv4-mapped, 6to4, Teredo, and the NAT64 well-known prefix) are unwrapped and the embedded
    IPv4 is judged instead, so e.g. `64:ff9b::7f00:1` (NAT64-encoded 127.0.0.1) is rejected.
    Raises PolicyViolation on failure.
    """
    resolver = resolver or _default_resolver
    try:
        addrs = resolver(host)
    except OSError as e:
        raise PolicyViolation(f"DNS resolution failed for {host}: {e!r}") from e
    if not addrs:
        raise PolicyViolation(f"no addresses resolved for {host}")
    validated: list[str] = []
    for raw in addrs:
        effective = _effective_ip(ipaddress.ip_address(raw))
        if not effective.is_global:
            raise PolicyViolation(
                f"{host} resolves to a non-global address {raw!r}; refusing (fail closed)")
        validated.append(str(effective))
    return validated


_NAT64_WKP = ipaddress.ip_network("64:ff9b::/96")


def _effective_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """Return the address whose routability should be judged.

    For an IPv6 address that EMBEDS an IPv4 address (IPv4-mapped, 6to4, Teredo, NAT64 WKP), return
    that IPv4 so a private/loopback IPv4 cannot hide inside a globally-routable-looking IPv6.
    """
    if isinstance(ip, ipaddress.IPv4Address):
        return ip
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    teredo = ip.teredo
    if teredo is not None:
        return teredo[1]
    if ip in _NAT64_WKP:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def _default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


@dataclass
class Fetcher:
    """Allowlisted fetcher with an injectable transport, resolver and clock."""

    allowlist: Allowlist
    user_agent: str
    transport: Transport | None = None
    resolver: Callable[[str], list[str]] | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS
    _last_request_at: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.user_agent, str) or not self.user_agent.strip():
            raise FetchError("a non-empty User-Agent is required (D6: refuse to run without it)")
        if self.transport is None:
            self.transport = _PinnedTLS(self.user_agent, self.resolver or _default_resolver)

    def fetch(self, url: str, ticker: str) -> FetchedDocument:
        """Fetch `url` for `ticker`, following redirects with per-hop policy checks.

        Raises PolicyViolation / ResponseTooLarge / FetchError on any failure; never returns a
        partial document.
        """
        requested_url = url
        try:
            current = normalize_url(url)
            for _hop in range(MAX_REDIRECTS + 1):
                host = urlsplit(current).hostname or ""
                if not self.allowlist.allows(host, ticker):
                    raise PolicyViolation(f"host {host!r} is not approved for {ticker}")
                ips = resolve_and_validate(host, self.resolver)
                self._respect_rate_limit(host)
                resp = self._request(host, ips[0], current)
                if resp.status in _REDIRECT_STATUSES:
                    current = self._redirect_target(current, resp)
                    continue
                if resp.status != 200:
                    raise FetchError(f"unexpected status {resp.status} for {current}")
                return self._build_document(requested_url, current, resp)
        except SourcePolicyError as e:
            # normalize the pure-policy error into the fetch error type so callers catch one family
            raise PolicyViolation(str(e)) from e
        raise FetchError(f"too many redirects fetching {requested_url}")

    # ---- internals ---------------------------------------------------

    def _respect_rate_limit(self, host: str) -> None:
        last = self._last_request_at.get(host)
        now = self.clock()
        if last is not None:
            wait = self.rate_limit_seconds - (now - last)
            if wait > 0:
                self.sleep(wait)
        self._last_request_at[host] = self.clock()

    def _request(self, host: str, ip: str, url: str) -> _RawResponse:
        parts = urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target += f"?{parts.query}"
        headers = {
            "Host": host,
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,text/plain",
            "Accept-Encoding": "identity",   # we do not invite compression (see _decode_body)
            "Connection": "close",
        }
        try:
            raw = self.transport.request(host, ip, target, headers)  # type: ignore[union-attr]
        except FetchError:
            raise
        except OSError as e:
            raise FetchError(f"network error contacting {host}: {e!r}") from e
        if raw.peer_ip is not None and raw.peer_ip != ip:
            raise PolicyViolation(
                f"connected peer {raw.peer_ip} != pinned address {ip} (possible rebinding)")
        if len(raw.body) > MAX_RESPONSE_BYTES:
            raise ResponseTooLarge("compressed body exceeds the 5 MiB limit")
        return raw

    def _redirect_target(self, current: str, resp: _RawResponse) -> str:
        location = resp.headers.get("location")
        if not location or not location.strip():
            raise FetchError(f"redirect with missing/empty Location from {current}")
        if urlsplit(location).scheme == "http":
            raise PolicyViolation(f"refusing HTTPS->HTTP downgrade redirect to {location!r}")
        # urljoin handles both absolute and relative Location values.
        return normalize_url(urljoin(current, location))

    def _build_document(self, requested_url: str, final_url: str, resp: _RawResponse) -> FetchedDocument:
        ct = (resp.headers.get("content-type", "") or "").split(";")[0].strip().lower()
        if ct not in SUPPORTED_CONTENT_TYPES:
            raise FetchError(f"unsupported content-type {ct!r} (no document produced)")
        body = _decode_body(resp.headers.get("content-encoding", ""), resp.body)
        return FetchedDocument(requested_url=requested_url, final_url=final_url, raw=body,
                               content_type=ct)


def _decode_body(encoding: str, raw: bytes) -> bytes:
    """Decode a response body, enforcing MAX_RESPONSE_BYTES on the DECOMPRESSED result.

    `raw` is already <= MAX_RESPONSE_BYTES (the transport reads with a cap). Here we stream-decode
    and abort as soon as the decompressed output would exceed the limit, so a compression bomb is
    rejected rather than buffered.
    """
    enc = (encoding or "").strip().lower()
    if enc not in SUPPORTED_CONTENT_ENCODINGS:
        raise FetchError(f"unsupported content-encoding {enc!r}")
    if enc in ("", "identity"):
        return raw
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS) if enc == "gzip" else zlib.decompressobj()
    try:
        out = decompressor.decompress(raw, MAX_RESPONSE_BYTES + 1)
    except zlib.error as e:
        raise FetchError(f"malformed {enc} body: {e!r}") from e
    if len(out) > MAX_RESPONSE_BYTES:
        raise ResponseTooLarge("decompressed body exceeds the 5 MiB limit")
    # Flush any remaining buffered output and check again (decompress() may hold back bytes).
    out += decompressor.flush(MAX_RESPONSE_BYTES + 1 - len(out))
    if len(out) > MAX_RESPONSE_BYTES:
        raise ResponseTooLarge("decompressed body exceeds the 5 MiB limit")
    return out


class _PinnedTLS:
    """Default transport: connect to a validated IP, verify TLS against the HOSTNAME."""

    def __init__(self, user_agent: str, resolver: Callable[[str], list[str]]) -> None:
        self.user_agent = user_agent
        self.resolver = resolver

    def request(self, host: str, ip: str, target: str, headers: dict[str, str]) -> _RawResponse:
        ctx = ssl.create_default_context()
        if not ctx.check_hostname or ctx.verify_mode != ssl.CERT_REQUIRED:
            raise FetchError("TLS verification must be enabled (check_hostname + CERT_REQUIRED)")
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect((ip, 443))
            peer = sock.getpeername()[0]
            if peer != ip:
                raise PolicyViolation(f"peer {peer} != pinned {ip} (possible rebinding)")
            # server_hostname drives SNI and hostname verification against the HOSTNAME, not the IP.
            tls = ctx.wrap_socket(sock, server_hostname=host)
            try:
                tls.settimeout(READ_TIMEOUT)
                return _read_http(tls, host, target, headers)
            finally:
                tls.close()


def _read_http(sock, host: str, target: str, headers: dict[str, str]) -> _RawResponse:
    """Send a GET and read a capped HTTP/1.1 response (status, headers, body)."""
    request = [f"GET {target} HTTP/1.1", f"Host: {host}"]
    request += [f"{k}: {v}" for k, v in headers.items() if k.lower() != "host"]
    request += ["", ""]
    sock.sendall("\r\n".join(request).encode("ascii"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            raise FetchError("response headers too large")
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    resp_headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            resp_headers[k.strip().lower()] = v.strip()
    body = bytearray(rest)
    while len(body) < MAX_RESPONSE_BYTES:
        chunk = sock.recv(min(65536, MAX_RESPONSE_BYTES - len(body)))
        if not chunk:
            break
        body += chunk
    if len(body) >= MAX_RESPONSE_BYTES:
        # May or may not be complete; treat hitting the cap as too large (fail closed).
        raise ResponseTooLarge("compressed body exceeds the 5 MiB limit")
    if resp_headers.get("transfer-encoding", "").lower() == "chunked":
        body = bytearray(_dechunk(bytes(body)))
    return _RawResponse(status=status, headers=resp_headers, body=bytes(body))


def _dechunk(data: bytes) -> bytes:
    """Decode HTTP/1.1 chunked transfer-encoding. Raises FetchError on malformed input."""
    out = bytearray()
    pos = 0
    while True:
        end = data.find(b"\r\n", pos)
        if end == -1:
            raise FetchError("malformed chunked body: missing chunk-size line")
        size_field = data[pos:end].split(b";", 1)[0].strip()
        try:
            size = int(size_field, 16)
        except ValueError as e:
            raise FetchError(f"malformed chunked body: bad chunk size {size_field!r}") from e
        pos = end + 2
        if size == 0:
            break
        if pos + size > len(data):
            raise FetchError("malformed chunked body: chunk shorter than declared size")
        out += data[pos:pos + size]
        if len(out) > MAX_RESPONSE_BYTES:
            raise ResponseTooLarge("dechunked body exceeds the 5 MiB limit")
        pos += size + 2  # skip trailing CRLF
    return bytes(out)
