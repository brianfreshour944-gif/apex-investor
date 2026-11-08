"""M6 — isolation/regression harness (Phase 2).

THE KEY DEMO (Phase 2 plan §6): the advisory/interpretation pipeline can only ADD a clearly-labelled
section to the daily report; it cannot change a single Phase 1 byte — ledger figures, risk verdicts,
alerts, prices, section order, or formatting.

Contract asserted for EVERY advisory state S (A1–A5):
  A1  advisory=None rendering is the byte-prefix of the advisory=S rendering up to the advisory
      heading (the advisory block is strictly appended; nothing is inserted or reordered);
  A2  the md=None bytes are a byte-prefix of md=S with the advisory heading as the first divergence
      (whole-prefix byte equality — the strictest form of "nothing before section 7 changed");
  A3  the advisory block appears EXACTLY once, starts with the canonical heading, and is the last
      section;
  A4  to_console() == to_markdown().rstrip() for both renderings;
  A5  (state=None) byte-identity to the immutable pre-M5 fixture (M5 guarantee preserved).

Hostile battery H1–H5 verifies payloads cannot ESCAPE their labelled advisory section or alter the
Phase 1 prefix — exercising the real rendering boundary (report._md_safe), not just storage.

Design: reports/M6_ISOLATION_HARNESS_DESIGN_2026-11-07.md. Boundaries: purely additive; M5 helpers
imported (verified side-effect-free); no accepted module touched; no network; fixture AdvisorySection
objects only; report_pre_m5.md never regenerated.
"""
from __future__ import annotations

import pytest

from apex_investor.advisory import AdvisoryEvidence, AdvisoryItem, AdvisorySection
from apex_investor.interpret import build_section_for

# M5 helpers imported, NOT duplicated (operator condition 3). Verified side-effect-free: importing
# this module only pulls apex_investor.* modules; helpers are deterministic pure functions.
from tests.test_advisory import (
    FIXTURE as PRE_M5_FIXTURE,
)
from tests.test_advisory import (
    _doc as m5_doc,
)
from tests.test_advisory import (
    _finding as m5_finding,
)
from tests.test_advisory import (
    _phase1_report as m5_phase1_report,
)
from tests.test_advisory import store  # noqa: F401  (imported pytest fixture, used as a param below)

ADVISORY_HEADING = "## 7. Interpretation (advisory)"


# ---- the canonical non-advisory baseline ---------------------------------------------------------

@pytest.fixture(scope="module")
def baseline_md() -> str:
    """The Phase 1 report with advisory=None — the immutable comparison base for every state."""
    return m5_phase1_report().to_markdown()


# ---- advisory-state matrix ----------------------------------------------------------------------

def _evidence(doc_id="d0" * 32, quote='"quoted text"'):
    return AdvisoryEvidence(doc_id=doc_id, quote=quote, source_url="https://www.sec.gov/x.htm",
                            title="10-Q", published_at="2024-05-30", retrieved_at="2024-06-01",
                            published_at_status="known")


def _item(claim="net revenue was up", quote="quoted text", **over):
    fields = dict(finding_id="F1", ticker="ACME", claim=claim, reason="revenue grew",
                  falsifier="revenue fell", confidence=0.5, model="m", prompt_version="v1",
                  recency="recent", evidence_age_days=2, evidence=(_evidence(quote=quote),))
    fields.update(over)
    return AdvisoryItem(**fields)


def _states():
    """The full advisory-state matrix: structural states + populated + hostile battery."""
    single = AdvisorySection(items=(_item(),), available=True)
    multi = AdvisorySection(items=(
        _item(finding_id="F1", claim="first claim"),
        _item(finding_id="F2", claim="second claim", ticker="JUNK", recency="older",
              evidence_age_days=40, evidence=(_evidence(quote="other quote", doc_id="d1" * 32),)),
    ), available=True)
    hostile = AdvisorySection(items=(
        # H1 newline/control carriage
        _item(claim="line one\nline two\r\nline three\ttabbed\x00\x1f\x7f",
              quote='quote with\nnewline and \r carriage'),
        # H2 markdown-structural payloads
        _item(claim="# heading ``` code - list | table ](link", quote="## fake heading"),
        # H3 a fake advisory heading embedded as text
        _item(claim=f"preamble {ADVISORY_HEADING} trailing", quote=ADVISORY_HEADING),
        # H4 10k-character single-line payload
        _item(claim="X" * 10_000, quote="Y" * 10_000),
        # H5 unicode confusables of the heading itself
        _item(claim="＃＃ ７．Ｉｎｔｅｒｐｒｅｔａｔｉｏｎ", quote="секция интерпретации"),
    ), available=True)
    return {
        "unavailable": AdvisorySection(items=(), available=False),
        "empty": AdvisorySection(items=(), available=True),
        "single": single,
        "multi": multi,
        "hostile": hostile,
    }


_STATES = _states()
_STATE_IDS = sorted(_STATES)


@pytest.fixture(params=_STATE_IDS)
def advisory_state(request):
    return _STATES[request.param]


# ---- A1/A2: the advisory block is strictly appended; Phase 1 prefix byte-identical ----------------

def _advisory_start(md: str) -> int:
    """Byte offset of the advisory heading line start, or len(md) if absent."""
    probe = f"\n{ADVISORY_HEADING}\n"
    i = md.find(probe)
    if i == -1:
        assert not md.startswith(ADVISORY_HEADING), "advisory heading must not start the report"
        return len(md)
    return i + 1  # point AT the heading's first character


def test_a1_a2_phase1_prefix_byte_identical(baseline_md, advisory_state):
    md_none = baseline_md
    md_s = m5_phase1_report(advisory=advisory_state).to_markdown()
    cut_none = _advisory_start(md_none)
    cut_s = _advisory_start(md_s)
    # A1: advisory=None has NO advisory section at all.
    assert ADVISORY_HEADING not in md_none
    assert cut_none == len(md_none)
    # A2: EVERY Phase 1 byte is identical between None and state S. The only bytes preceding the
    # advisory heading that are absent from md_none are the renderer's own section separator
    # (exactly one blank line appended when a section follows). Asserted as exact byte layout:
    #   md_none ends with exactly one "\n"; md_s's prefix is md_none minus that final newline,
    #   plus exactly "\n\n" (the separator), then the advisory heading.
    assert md_none.endswith("\n") and not md_none.endswith("\n\n")
    assert md_s[:cut_s] == md_none[:-1] + "\n\n"
    assert md_s[cut_s:].startswith(ADVISORY_HEADING)


def test_a3_advisory_block_appears_exactly_once_and_last(baseline_md, advisory_state):
    md_s = m5_phase1_report(advisory=advisory_state).to_markdown()
    first = md_s.find(ADVISORY_HEADING)
    assert first != -1
    assert md_s.count(ADVISORY_HEADING) == 1, "advisory section must appear exactly once"
    # It is the LAST section: everything after the heading belongs to the advisory block, and the
    # report ends with a single newline.
    assert md_s.endswith("\n") and not md_s.endswith("\n\n")


def test_a4_console_matches_markdown(baseline_md, advisory_state):
    for adv in (None, advisory_state):
        r = m5_phase1_report(advisory=adv)
        assert r.to_console() == r.to_markdown().rstrip()


def test_a5_none_state_matches_immutable_fixture():
    # M5 guarantee preserved: advisory=None is byte-identical to the captured pre-M5 fixture.
    assert m5_phase1_report().to_markdown().encode("utf-8") == PRE_M5_FIXTURE.read_bytes()


# ---- hostile battery: payloads cannot escape or alter the Phase 1 prefix -------------------------

def test_hostile_payloads_cannot_alter_phase1_prefix(baseline_md):
    cut_none = _advisory_start(baseline_md)
    md_h = m5_phase1_report(advisory=_STATES["hostile"]).to_markdown()
    # The Phase 1 prefix survives the hostile battery byte-for-byte.
    assert md_h[:cut_none] == baseline_md[:cut_none]
    # The fake heading inside a claim/quote (H3) must NOT create a second advisory section: the
    # canonical heading appears exactly once (the real one).
    assert md_h.count(ADVISORY_HEADING) == 1
    # Confusables (H5) must not be treated as the heading either: no additional occurrence.
    assert md_h.count("＃＃ ７．Ｉｎｔｅｒｐｒｅｔａｔｉｏｎ") == 1


def test_hostile_payloads_render_on_single_escaped_lines():
    md_h = m5_phase1_report(advisory=_STATES["hostile"]).to_markdown()
    advisory_block = md_h[_advisory_start(md_h):]
    # The renderer's own structural lines are known exactly; every OTHER line is an escaped payload
    # line. Raw injected structure must never appear:
    assert "\n# heading" not in advisory_block
    assert "\n## fake heading" not in advisory_block
    assert "\n```" not in advisory_block
    assert "](" not in advisory_block or "\\](" in advisory_block  # ]( must be escaped when present
    # Control characters must be stripped, never passed through:
    for ctrl in ("\x00", "\x1f", "\x7f", "\r"):
        assert ctrl not in advisory_block
    # Tabs are collapsed to spaces (no raw tab survives):
    assert "\t" not in advisory_block
    # The newline-bearing claim must NOT split into multiple list items: the payload's "line two"
    # appears on the SAME line as the rest of the claim (single-line containment).
    claim_lines = [ln for ln in advisory_block.split("\n") if "line one" in ln]
    assert len(claim_lines) == 1 and "line two" in claim_lines[0] and "line three" in claim_lines[0]
    # The multi-line quote likewise stays on one line:
    quote_lines = [ln for ln in advisory_block.split("\n") if "quote with" in ln]
    assert len(quote_lines) == 1 and "newline" in quote_lines[0]
    # The 10k payload stays on a single (very long) line:
    big_lines = [ln for ln in advisory_block.split("\n") if "X" * 100 in ln]
    assert len(big_lines) == 1 and len(big_lines[0]) >= 10_000


def test_console_matches_markdown_for_hostile_state():
    r = m5_phase1_report(advisory=_STATES["hostile"])
    assert r.to_console() == r.to_markdown().rstrip()


# ---- determinism across the whole matrix ---------------------------------------------------------

def test_every_state_is_deterministic(advisory_state):
    a = m5_phase1_report(advisory=advisory_state).to_markdown()
    b = m5_phase1_report(advisory=advisory_state).to_markdown()
    assert a == b


# ---- isolation end-to-end via the real pipeline (M4→M4b→M5), not just display types --------------

def test_real_pipeline_section_also_preserves_phase1_prefix(baseline_md, store):  # noqa: F811
    # Beyond fixture AdvisorySections: a REAL VerifiedFinding through build_section_for must produce
    # a section whose presence still leaves the Phase 1 prefix byte-identical.
    doc = m5_doc(store)
    finding = m5_finding(doc)
    section = build_section_for([finding], store, as_of="2024-06-01")
    assert section.available
    md_s = m5_phase1_report(advisory=section).to_markdown()
    cut = _advisory_start(baseline_md)
    assert md_s[:cut] == baseline_md[:cut]
    assert md_s.count(ADVISORY_HEADING) == 1
