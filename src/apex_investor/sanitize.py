"""Deterministic document sanitizer (Phase 2 / M1b).

Scope (M1b): turn retrieved bytes into the downstream-facing TEXT that is stored and later shown
to a model. PURE and DETERMINISTIC: no network, no model/LLM calls, no portfolio or ledger
contact. It removes ACTIVE and INVISIBLE content while preserving legitimate visible text.

What it strips / neutralizes:
  - <script>, <style>, <template>, <noscript> elements (element AND contents);
  - HTML comments (a classic place to hide an injected instruction);
  - all element tags/attributes, so event handlers (onclick=...), `javascript:` URLs and other
    active attributes cannot survive — only visible text is emitted;
  - hidden text: elements with the `hidden` attribute, `aria-hidden="true"`, or an inline style
    that hides them (display:none, visibility:hidden, opacity:0, font-size:0, mso-hide:all);
  - zero-width and bidi-control characters (U+200B..U+200D, U+2060, U+FEFF, U+00AD, U+200E/F,
    U+202A..E, U+2066..9, ...) — these are used to obfuscate an instruction past a naive filter;
  - C0/C1 control characters except TAB, LF, CR.

What it deliberately does NOT do:
  - It does not delete legitimate visible prose. A prompt injection written as ordinary visible
    text ("ignore previous instructions ...") is *data* and is preserved as inert text; a
    deterministic sanitizer must not guess which sentences are "instructions", and broad deletion
    rules would silently remove meaningful financial content. That semantic attack is handled by
    the LAYERED defenses (delimited data channel, no-tool model, and the M4 citation/relevance
    gate), not here. See the Phase 2 plan §4.1.

KNOWN LIMITATION — it cannot detect every form of visually concealed content:
  Only the inline hiding techniques enumerated above are recognized (the `hidden` attribute,
  `aria-hidden="true"`, and the listed inline-style substrings). Text concealed by means it does
  not inspect — e.g. a CSS class defined in an EXTERNAL stylesheet, off-screen positioning
  (`position:absolute;left:-9999px`), white-on-white colour, `clip`, `text-indent:-9999px`, or
  `max-height:0;overflow:hidden` — is NOT detected and will be preserved as if visible. The
  sanitizer strips active content and the common inline hiding tricks; it does NOT guarantee that
  all hidden text is removed. Downstream layers (the delimited data channel and the M4 gate) must
  therefore continue to treat the entire document as untrusted data.

KNOWN LIMITATION — this is not a browser-equivalent HTML/CSS engine:
  It is built on the standard library `HTMLParser`, which tokenizes tags and attributes but does
  NOT build a DOM, apply CSS, run layout, or resolve external resources. It is therefore not a
  rendering engine and must not be described as removing every form of concealed content; it is a
  conservative text extractor for untrusted input.

FAIL CLOSED: undecodable bytes, a non-bytes input, or an unsupported content type raise
SanitizerError rather than returning a partial or guessed result.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

# Bump when the stripping rules change; stored with each Document so the exact rules used are
# recorded alongside the text they produced.
SANITIZER_VERSION = "1"

# Content types this sanitizer understands. Anything else fails closed.
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_TEXT_TYPES = frozenset({"text/plain"})

# Elements whose ENTIRE content is dropped (never visible, common injection carriers).
_SKIP_TAGS = frozenset({"script", "style", "template", "noscript"})

# Void elements: no closing tag, never pushed on the open-tag stack.
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                        "meta", "param", "source", "track", "wbr"})

# Block-level elements: emit a line break so paragraph/section boundaries are preserved.
_BLOCK_TAGS = frozenset({"address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
                         "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
                         "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
                         "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr",
                         "ul"})

# Inline-style substrings that hide an element (compared with spaces removed, lowercased).
_HIDDEN_STYLE_TOKENS = ("display:none", "visibility:hidden", "opacity:0", "font-size:0",
                        "mso-hide:all")

_ZERO_WIDTH = frozenset({
    "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad", "\u180e",
    "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2066", "\u2067", "\u2068", "\u2069",
})


class SanitizerError(Exception):
    """Input could not be sanitized safely (undecodable, unsupported, or malformed)."""


def _is_hidden(attrs: list[tuple[str, str | None]]) -> bool:
    """True if the element is hidden by ANY attribute occurrence.

    Scans EVERY attribute rather than collapsing into a dict: duplicate attributes (e.g.
    `aria-hidden="true" aria-hidden="false"`, or a `style` pair where only one hides) must not let
    a later value mask an earlier hiding declaration. Presence of `hidden`, any `aria-hidden` equal
    to "true", or any `style` containing a recognized hiding declaration is sufficient.
    """
    for k, v in attrs:
        key = k.lower()
        val = v or ""
        if key == "hidden":
            return True
        if key == "aria-hidden" and val.strip().lower() == "true":
            return True
        if key == "style":
            compact = re.sub(r"\s+", "", val.lower())
            if any(tok in compact for tok in _HIDDEN_STYLE_TOKENS):
                return True
    return False


class _TextExtractor(HTMLParser):
    """Collect visible text; drop skipped/hidden elements, comments, tags and attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._open: list[tuple[str, str]] = []  # (tag, kind) where kind in {normal, skip, hidden}

    def _skip_active(self) -> bool:
        return any(kind == "skip" for _, kind in self._open)

    def _hidden_active(self) -> bool:
        return any(kind == "hidden" for _, kind in self._open)

    def _classify(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        if self._skip_active() or tag in _SKIP_TAGS:
            return "skip"
        if self._hidden_active() or _is_hidden(attrs):
            return "hidden"
        return "normal"

    def _block_break(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _VOID_TAGS:
            if not self._skip_active() and not self._hidden_active():
                self._block_break(tag)
            return
        kind = self._classify(tag, attrs)
        self._open.append((tag, kind))
        if kind == "normal":
            self._block_break(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _VOID_TAGS:
            return
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i][0] == tag:
                kind = self._open[i][1]
                del self._open[i:]
                if kind == "normal":
                    self._block_break(tag)
                return

    def handle_data(self, data):
        if self._skip_active() or self._hidden_active():
            return
        self._parts.append(data)

    def handle_comment(self, data):  # noqa: ARG002 - comments are dropped deliberately
        return

    def handle_decl(self, decl):  # noqa: ARG002 - doctype etc. dropped
        return

    def handle_pi(self, data):  # noqa: ARG002 - processing instructions dropped
        return

    def unknown_decl(self, data):  # noqa: ARG002
        return

    def text(self) -> str:
        return "".join(self._parts)


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SanitizerError(f"undecodable input (expected UTF-8): {exc}") from exc


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # parser failure must fail closed, never a partial result
        raise SanitizerError(f"malformed HTML: {exc}") from exc
    return parser.text()


def _strip_invisible(s: str) -> str:
    out: list[str] = []
    for ch in s:
        o = ord(ch)
        if ch in _ZERO_WIDTH:
            continue
        if o < 0x20:
            if ch in ("\t", "\n", "\r"):
                out.append(ch)
            continue
        if o == 0x7F or 0x80 <= o <= 0x9F:  # DEL and C1 controls
            continue
        if ch in ("\u2028", "\u2029"):  # line/paragraph separators -> newline
            out.append("\n")
            continue
        out.append(ch)
    return "".join(out)


def _normalize_ws(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    result: list[str] = []
    blank = 0
    for ln in lines:
        if ln == "":
            blank += 1
            if blank <= 1:
                result.append("")
        else:
            blank = 0
            result.append(ln)
    return "\n".join(result).strip()


def sanitize(raw: bytes, *, content_type: str) -> str:
    """Return the sanitized downstream-facing text for `raw`. Deterministic; no I/O.

    `content_type` selects the strategy: an HTML type (tags stripped, hidden/skip content
    removed) or `text/plain` (invisible characters removed only). Unsupported types and
    undecodable input fail closed with SanitizerError.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise SanitizerError("raw must be bytes")
    ct = (content_type or "").split(";")[0].strip().lower()
    text = _decode(bytes(raw))
    if ct in _HTML_TYPES:
        text = _html_to_text(text)
    elif ct in _TEXT_TYPES:
        pass
    else:
        raise SanitizerError(f"unsupported content_type: {content_type!r}")
    return _normalize_ws(_strip_invisible(text))
