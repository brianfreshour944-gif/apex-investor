"""Corpus/annotation consistency enforcement (G2 renderer-v2 follow-up).

The corpus drafts carry `expectations.reference_value` so the renderer's answer-free guard can reject a
question that leaks the answer. That duplicates data held authoritatively in the operator annotations,
so this module cross-checks the two and fails closed on any drift.

Authority and scope:
  - The SINGLE source of truth is the source-controlled annotation artifact at
    `DEFAULT_ANNOTATIONS_PATH` (`data/model_gate/g2_annotations.json`), a byte-exact copy of the frozen
    operator annotations. It is pinned by the raw-file SHA-256 in `AUTHORITATIVE_ANNOTATIONS_SHA256`.
  - The operator approval of those annotations is recorded SEPARATELY, in the source-controlled
    approval record at `DEFAULT_APPROVAL_PATH` (`data/model_gate/g2_annotation_approval.json`), pinned
    by `APPROVAL_RECORD_SHA256`. The approval record is lean: it binds the approval to the authority
    by hash and does not duplicate any reference value. Both artifacts are verified together.
  - This module only READS corpus and annotations and compares them. It never scores, never calls a
    model, never touches the network, and never serializes a reference value into any model-facing
    payload.
  - It is deterministic and offline.

Fail-closed conditions (each raises `CorpusConsistencyError`):
  - the annotation artifact is missing, unreadable, not JSON, or its raw SHA-256 does not match the pin;
  - the annotation artifact has no `scenarios` list;
  - a corpus scenario has no matching annotation, or an annotation has no matching corpus scenario;
  - a scenario's `must_abstain` disagrees with the annotation's `expected_behavior`;
  - an answerable scenario is missing `reference_value`, or it is non-numeric, or it differs from the
    authoritative annotation;
  - an abstain scenario carries a non-null `reference_value` (HOLD-03 must remain null);
  - an answerable scenario's scored `measure_definition` does not represent the authority's declared
    `measure` (R1′ measure-name identity);
  - an answerable scenario's scored `evidence_description` does not match the authority's declared
    source location (source-location identity), including the case where the measure name matches but
    the source location differs (the HOLD-02 / F-1 class);
  - the approval record is missing, unreadable, not JSON, or its raw SHA-256 does not match its pin;
  - the approval record's `approved_authority_raw_sha256` or `source_frozen_sha256` does not match the
    pinned authority, or does not match the authority file actually in force;
  - the approval record's scope does not cover every required scenario id (incl. HOLD-03);
  - the source-controlled DEV corpus does not match its manifest's canonical hash.

Limitation: this proves the two copies AGREE at check time. It does not make the corpus the authority;
if they ever disagree, the annotation wins and the check refuses the corpus.

The measure-name and source-location checks run only when the authority DECLARES the corresponding
field (`measure` / `evidence_description`). The genuine G2 authority declares both for every answerable
scenario; legacy/synthetic annotations that omit them are not subject to the check. A declared field
that the corpus cannot satisfy is a problem (fail closed). The source-location check compares the
authority's location against the scored description's LOCATION CLAUSE (the first sentence): a scored
description may carry an explanatory tail that NAMES another table (HOLD-02 negates the consolidated
statement), so comparing against the full text would let a wrong authority location pass.

R1′ (the measure-name check) is NOT sufficient on its own: it compares the short measure NAME, which
for HOLD-02 matches even though the source location differs. The source-location check is what catches
that class; the two checks are complementary and both are required.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .citation import _content_terms
from .model_gate import canonical_sha256

# The single, source-controlled authority: a byte-exact copy of the frozen operator annotations.
DEFAULT_ANNOTATIONS_PATH = Path("data/model_gate/g2_annotations.json")

# The source-controlled G2 corpora checked against the authority (byte-exact copies of the drafts).
DEFAULT_DEV_CORPUS_PATH = Path("data/model_gate/g2_corpus.dev.json")
DEFAULT_HELDOUT_CORPUS_PATH = Path("data/model_gate/g2_corpus.heldout.json")

# Source-controlled manifests that pin each corpus's canonical hash. The held-out manifest predates
# this module; the DEV manifest (added G2-C2c) closes the provenance gap between the two halves. Both
# are verified the same way so DEV and held-out provenance are independently verifiable.
DEFAULT_DEV_MANIFEST_PATH = Path("data/model_gate/g2_dev_manifest.json")
DEFAULT_HELDOUT_MANIFEST_PATH = Path("data/model_gate/g2_heldout_manifest.json")

# Genuine G2 scenarios carry a dedicated `question` field (renderer v2); synthetic/test-only fixtures
# carry the legacy `instruction` field. This marker distinguishes the two paths.
G2_SCENARIO_MARKER = "question"

# Raw-file SHA-256 of the source-controlled authority. Pinning the raw bytes (not a canonical
# re-serialization) means any byte change to the authority is detected.
# g2-pilot-draft-5 (2026-11-11, G2-C3): operator-approved revision adding one DEV-only
# forbidden-directive TRAP scenario (T-1) with its real documents[]. No `attack` block (a trap is
# not an injection). Supersedes 27f7569b….
AUTHORITATIVE_ANNOTATIONS_SHA256 = (
    "991f38c07dec8dbbb3369bd9c5c21d643299f0753740b35279958e7b63425a18"
)

# The separate, source-controlled OPERATOR-APPROVAL record. Option (b): the authority stays a
# byte-exact copy of the frozen annotations; the operator approval event is recorded here and binds
# that approval to the authority by hash. This record is lean: it references the authority by hash and
# does NOT duplicate any reference value.
DEFAULT_APPROVAL_PATH = Path("data/model_gate/g2_annotation_approval.json")

# Raw-file SHA-256 of the approval record. Pinned so the approval's own bytes are frozen too: a change
# to the approval record (or its disappearance) is detected.
APPROVAL_RECORD_SHA256 = (
    "0433c40c71e0cddc338609e9083e83f3c88e05509d0a402034fac93f2c5a9526"
)

# The approval scope must cover every reference scenario, including the HOLD-03 and INJ-H5 abstentions.
REQUIRED_SCOPE_SCENARIO_IDS = frozenset(
    {"DEV-01", "DEV-02", "DEV-03", "T-1", "HOLD-01", "HOLD-02", "HOLD-03",
     "INJ-D1", "INJ-D2", "INJ-H1", "INJ-H2", "INJ-H3", "INJ-H4", "INJ-H5"}
)

# The approved G2-C2d injection-scenario allocation (see the G2-C2d design report, rev 3). The gate
# enforces this split so a dropped, duplicated, or mis-placed injection scenario fails closed. Keys are
# the six approved attack classes (I-1..I-6). I-1 is the ONLY class in BOTH splits (with different
# construction/wording) — the anti-memorization anchor.
APPROVED_DEV_INJECTION_SCENARIOS = frozenset({"INJ-D1", "INJ-D2"})
APPROVED_HELDOUT_INJECTION_SCENARIOS = frozenset(
    {"INJ-H1", "INJ-H2", "INJ-H3", "INJ-H4", "INJ-H5"}
)
# Per-split class coverage (matches the approved allocation exactly):
#   DEV     = {I-1 (answerable), I-3 (forged-value)}
#   heldout = {I-1 (answerable, fresh construction), I-2, I-4, I-5, I-6}
# The union must cover all six classes I-1..I-6, and I-1 must appear in both splits.
APPROVED_DEV_INJECTION_CLASSES = frozenset({"I-1", "I-3"})
APPROVED_HELDOUT_INJECTION_CLASSES = frozenset({"I-1", "I-2", "I-4", "I-5", "I-6"})
ALL_INJECTION_CLASSES = frozenset({"I-1", "I-2", "I-3", "I-4", "I-5", "I-6"})
# Fields required (non-empty) in an `attack` block. `target_assertion` may be empty (M5); an empty
# `required_cited_doc_ids` list is valid (it means "no required citations").
REQUIRED_ATTACK_FIELDS = ("goal", "target_action", "injection_doc_sym")
# The marker key naming the supplied document that carries the hostile text.
INJECTION_DOC_SYM_KEY = "injection_doc_sym"


class CorpusConsistencyError(Exception):
    """Raised when a corpus disagrees with the authoritative operator annotations (fail closed)."""


def default_annotations_path() -> Path:
    """Absolute path to the source-controlled authority, resolved from the repo root (not CWD)."""
    return _repo_path(DEFAULT_ANNOTATIONS_PATH)


def _repo_path(rel: Path) -> Path:
    """Resolve a repo-relative path from this module's location, independent of the process CWD."""
    return (Path(__file__).resolve().parents[2] / rel).resolve()


def default_corpus_paths() -> tuple[Path, Path]:
    """Absolute paths to the source-controlled DEV and held-out G2 corpora."""
    return _repo_path(DEFAULT_DEV_CORPUS_PATH), _repo_path(DEFAULT_HELDOUT_CORPUS_PATH)


def default_manifest_paths() -> tuple[Path, Path]:
    """Absolute paths to the source-controlled DEV and held-out corpus manifests."""
    return _repo_path(DEFAULT_DEV_MANIFEST_PATH), _repo_path(DEFAULT_HELDOUT_MANIFEST_PATH)


def default_approval_path() -> Path:
    """Absolute path to the source-controlled operator-approval record (resolved from the repo root)."""
    return _repo_path(DEFAULT_APPROVAL_PATH)


def load_approval_record(path: Path | str, *,
                         expected_sha256: str = APPROVAL_RECORD_SHA256) -> dict:
    """Load the operator-approval record and verify its raw SHA-256 against the pin. Fail closed.

    A missing/unreadable/non-JSON record, or any byte change (hash mismatch), is refused.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise CorpusConsistencyError(f"approval record not readable: {p}: {exc}") from exc
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise CorpusConsistencyError(
            f"approval record hash mismatch: {actual} != pinned {expected_sha256} (approval changed)")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise CorpusConsistencyError(f"approval record is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CorpusConsistencyError("approval record must be a JSON object")
    return data


def check_approval_record(approval: dict, *,
                          authority_raw_sha256: str,
                          expected_authority_sha256: str =
                          AUTHORITATIVE_ANNOTATIONS_SHA256) -> list[str]:
    """Return a sorted list of approval-record problems (empty means valid). Fail closed on any issue.

    Checks, in addition to the raw-hash pin (verified at load time):
      - the record approves the authority actually in force (approved_authority_raw_sha256 matches
        both the pinned authority hash and the actual authority file hash);
      - the record's frozen-source hash matches the pinned authority hash;
      - the approval scope covers every required scenario id, including the HOLD-03 abstention.
    """
    if not isinstance(approval, dict):
        return ["approval record must be a JSON object"]
    problems: list[str] = []

    approved = approval.get("approved_authority_raw_sha256")
    if approved != expected_authority_sha256:
        problems.append(
            f"approval approves authority {approved!r} but the pinned authority is "
            f"{expected_authority_sha256}")
    if approved != authority_raw_sha256:
        problems.append(
            f"approval approves authority {approved!r} but the authority file is {authority_raw_sha256}")

    frozen = approval.get("source_frozen_sha256")
    if frozen != expected_authority_sha256:
        problems.append(
            f"approval frozen-source hash {frozen!r} != pinned authority {expected_authority_sha256}")

    scope = approval.get("scope")
    if not isinstance(scope, dict) or not isinstance(scope.get("scenario_ids"), list):
        problems.append("approval record has no scope.scenario_ids list")
    else:
        covered = {s for s in scope["scenario_ids"] if isinstance(s, str)}
        missing = sorted(REQUIRED_SCOPE_SCENARIO_IDS - covered)
        if missing:
            problems.append(f"approval scope does not cover all required scenarios (missing {missing})")
    return sorted(problems)


def assert_approval_record_valid(path: Path | str, *, authority_raw_sha256: str,
                                 expected_sha256: str = APPROVAL_RECORD_SHA256,
                                 expected_authority_sha256: str =
                                 AUTHORITATIVE_ANNOTATIONS_SHA256) -> dict:
    """Load + verify the approval record against the authority. Fail closed. Returns the record."""
    approval = load_approval_record(path, expected_sha256=expected_sha256)
    problems = check_approval_record(approval, authority_raw_sha256=authority_raw_sha256,
                                     expected_authority_sha256=expected_authority_sha256)
    if problems:
        raise CorpusConsistencyError(
            "approval-record check failed:\n  " + "\n  ".join(problems))
    return approval


def load_default_authoritative_annotations() -> dict:
    """Load the source-controlled authority and verify it against the pin. Fail closed."""
    return load_authoritative_annotations(default_annotations_path())


def verify_corpus_manifest(corpus: dict, manifest: dict, *, label: str) -> list[str]:
    """Return a sorted list of manifest problems (empty means the corpus matches its manifest).

    A corpus manifest pins the corpus's canonical hash (`canonical_sha256`, the same convention the
    held-out path already uses). This proves the corpus bytes actually checked are the ones pinned.
    """
    problems: list[str] = []
    if not isinstance(manifest, dict):
        return [f"{label}: manifest must be a JSON object"]
    path = manifest.get("path")
    if not isinstance(path, str) or not path:
        problems.append(f"{label}: manifest has no 'path'")
    expected = manifest.get("fixture_sha256") or manifest.get("heldout_fixture_sha256")
    if not isinstance(expected, str) or not expected:
        problems.append(f"{label}: manifest has no fixture sha256")
        return sorted(problems)
    actual = canonical_sha256(corpus)
    if actual != expected:
        problems.append(f"{label}: corpus canonical hash {actual} != manifest {expected}")
    return sorted(problems)


def assert_default_corpora_consistent() -> None:
    """Verify the source-controlled DEV+HOLD corpora against the source-controlled authority.

    Uses only repository artifacts, so it is reproducible from a clean checkout. Fail closed. Also
    verifies the source-controlled operator-approval record binds to the authority, and that each
    corpus matches its source-controlled manifest pin (DEV and held-out provenance independently
    verifiable).
    """
    dev_path, heldout_path = default_corpus_paths()
    corpora = [_read_json(dev_path, "dev corpus"), _read_json(heldout_path, "held-out corpus")]
    assert_corpora_consistent_with_annotation_file(corpora, default_annotations_path())
    assert_default_approval_valid()
    assert_default_manifests_valid(corpora)


def assert_default_manifests_valid(corpora: list[dict] | None = None) -> None:
    """Verify each source-controlled corpus against its manifest pin. Fail closed.

    `corpora` may be supplied (already-loaded DEV, held-out order) to avoid re-reading; when omitted
    the corpora are read from disk.
    """
    dev_manifest_path, heldout_manifest_path = default_manifest_paths()
    if corpora is None:
        dev_path, heldout_path = default_corpus_paths()
        corpora = [_read_json(dev_path, "dev corpus"), _read_json(heldout_path, "held-out corpus")]
    dev_corpus, heldout_corpus = corpora
    problems = verify_corpus_manifest(dev_corpus, _read_json(dev_manifest_path, "dev manifest"),
                                      label="dev")
    problems += verify_corpus_manifest(heldout_corpus,
                                       _read_json(heldout_manifest_path, "held-out manifest"),
                                       label="held-out")
    if problems:
        raise CorpusConsistencyError(
            "corpus manifest check failed:\n  " + "\n  ".join(sorted(problems)))


def assert_default_approval_valid() -> dict:
    """Verify the source-controlled approval record against the authority actually in force.

    Reads the authority's raw SHA-256 from disk (so a mismatch between the approval and the actual
    authority file is detected), then applies the approval-record checks. Fail closed.
    """
    authority_path = default_annotations_path()
    try:
        authority_raw = hashlib.sha256(authority_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise CorpusConsistencyError(f"authority not readable: {authority_path}: {exc}") from exc
    return assert_approval_record_valid(default_approval_path(), authority_raw_sha256=authority_raw)


def is_g2_corpus(corpus: object) -> bool:
    """True when a corpus is a genuine G2 corpus (its scenarios carry the `question` field).

    An empty/absent scenario list is not treated as G2. Used to gate the consistency check: genuine G2
    corpora must be verified against the pinned authority, while synthetic/test-only fixtures (legacy
    `instruction` field) take the unchanged path.
    """
    if not isinstance(corpus, dict):
        return False
    scenarios = corpus.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        return False
    return all(isinstance(s, dict) and G2_SCENARIO_MARKER in s for s in scenarios)


def assert_genuine_g2_corpora_consistent(dev_corpus: dict, heldout_corpus: dict, *,
                                         annotations_path: Path | str | None = None) -> None:
    """Fail closed unless the two corpora agree with the pinned authority.

    Called by the evaluation CLI before any candidate is scored. `annotations_path` defaults to the
    source-controlled authority; pass an explicit path only for tests. Requires the authority's raw
    SHA-256 to match the pin (a missing or altered authority is refused). When the default authority is
    used, the source-controlled operator-approval record is verified as well (both artifacts), and each
    corpus is verified against its source-controlled manifest pin (DEV and held-out).
    """
    if annotations_path is None:
        path = default_annotations_path()
        assert_default_approval_valid()
    else:
        path = annotations_path
    assert_corpora_consistent_with_annotation_file([dev_corpus, heldout_corpus], path)
    if annotations_path is None:
        assert_default_manifests_valid([dev_corpus, heldout_corpus])


def _read_json(path: Path, label: str) -> dict:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CorpusConsistencyError(f"{label} not readable: {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise CorpusConsistencyError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CorpusConsistencyError(f"{label} must be a JSON object")
    return data


def load_authoritative_annotations(path: Path | str, *,
                                   expected_sha256: str = AUTHORITATIVE_ANNOTATIONS_SHA256) -> dict:
    """Load the frozen annotations and verify their raw SHA-256 against the pin. Fail closed."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise CorpusConsistencyError(f"annotations not readable: {p}: {exc}") from exc
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise CorpusConsistencyError(
            f"annotations hash mismatch: {actual} != pinned {expected_sha256} (authority changed)")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise CorpusConsistencyError(f"annotations are not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("scenarios"), list):
        raise CorpusConsistencyError("annotations must be an object with a 'scenarios' list")
    return data


def _annotation_index(annotations: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for entry in annotations["scenarios"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("scenario_id"), str):
            raise CorpusConsistencyError("each annotation must be an object with a string scenario_id")
        sid = entry["scenario_id"]
        if sid in out:
            raise CorpusConsistencyError(f"duplicate annotation scenario_id: {sid}")
        out[sid] = entry
    return out


def _is_int_like(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return not isinstance(value, float) or value.is_integer()


def _location_clause(evidence_description: object) -> str:
    """The source-location clause of an `evidence_description`: the text up to the first sentence end.

    The scored descriptions carry an explanatory tail after the location (HOLD-02's tail negates the
    consolidated statement, HOLD-01's names a separate non-GAAP measure). The location is the first
    sentence, so comparing only that clause avoids a wrong authority location matching a tail word.
    """
    if not isinstance(evidence_description, str):
        return ""
    return evidence_description.split(".", 1)[0].strip()


def _supplied_doc_ids(scenario: dict) -> set[str]:
    """The set of `doc_id`s the scenario supplies, computed the same way `materialize_corpus` does.

    A document's `doc_id` is `make_doc_id(source_url, sha256(raw))` (see `docstore`), independent of
    any sanitized text. Computing it here lets the gate verify that an attack's `required_cited_doc_ids`
    and `injection_doc_sym` actually resolve to documents the scenario supplies, without a store.
    """
    from .docstore import make_doc_id

    out: set[str] = set()
    for d in scenario.get("documents", []) or []:
        if not isinstance(d, dict):
            continue
        url = d.get("source_url")
        raw = d.get("raw")
        if isinstance(url, str) and isinstance(raw, str):
            out.add(make_doc_id(url, hashlib.sha256(raw.encode("utf-8")).hexdigest()))
    return out


def _supplied_doc_syms(scenario: dict) -> set[str]:
    return {d.get("sym") for d in (scenario.get("documents", []) or [])
            if isinstance(d, dict) and isinstance(d.get("sym"), str)}


def _authority_injection_syms(annotations: dict) -> set[str]:
    """Every `injection_doc_sym` named by the authority, across all annotation entries."""
    out: set[str] = set()
    for entry in annotations.get("scenarios", []) or []:
        attack = entry.get("attack") if isinstance(entry, dict) else None
        if isinstance(attack, dict):
            sym = attack.get(INJECTION_DOC_SYM_KEY)
            if isinstance(sym, str) and sym:
                out.add(sym)
    return out


def _attack_shape_problems(sid: str, attack: object) -> list[str]:
    """G3/G6: an `attack` block must be a non-empty object with the required non-empty fields.

    `target_assertion` MAY be empty (M5) and `required_cited_doc_ids` MAY be an empty list; neither is
    malformed. A missing/wrong-typed required field, a non-object attack, or an empty attack fails
    closed.
    """
    problems: list[str] = []
    if not isinstance(attack, dict):
        return [f"{sid}: attack must be a JSON object (got {type(attack).__name__})"]
    if not attack:
        return [f"{sid}: attack is empty (fail closed)"]
    for field in REQUIRED_ATTACK_FIELDS:
        value = attack.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{sid}: attack.{field} must be a non-empty string (fail closed)")
    if "target_assertion" in attack and not isinstance(attack["target_assertion"], str):
        problems.append(f"{sid}: attack.target_assertion must be a string (may be empty)")
    rids = attack.get("required_cited_doc_ids")
    if not isinstance(rids, list) or any(not isinstance(r, str) for r in rids):
        problems.append(
            f"{sid}: attack.required_cited_doc_ids must be a list of strings (may be empty)")
    return problems


def check_injection_metadata(corpus: dict, annotations: dict) -> list[str]:
    """G1–G7: fail-closed checks on per-scenario injection metadata. Returns sorted problems.

    G1: a scenario that SUPPLIES a document named by any authority `injection_doc_sym` must declare a
        valid, non-empty `attack`.
    G2: `attack.injection_doc_sym` must resolve to a document the scenario actually supplies.
    G3: the required `attack` fields are present and non-empty (see `_attack_shape_problems`).
    G4: every `attack.required_cited_doc_ids` entry must resolve to a supplied document.
    G5: a scenario that supplies NO injection document acquires NO injection requirement (no false
        positives); an absent attack there is fine.
    G6: a malformed/incomplete `attack` fails closed (even when one is present without an injection
        document).
    G7: the corpus `attack` must equal the authority `attack` field-for-field.
    """
    if not isinstance(corpus, dict) or not isinstance(corpus.get("scenarios"), list):
        return ["corpus must be an object with a 'scenarios' list"]
    ann = _annotation_index(annotations)
    injection_syms = _authority_injection_syms(annotations)
    problems: list[str] = []
    for scenario in corpus["scenarios"]:
        sid = scenario.get("id")
        if not isinstance(sid, str):
            continue
        expectations = scenario.get("expectations")
        if not isinstance(expectations, dict):
            continue
        corpus_attack = expectations.get("attack")
        supplies_injection = bool(_supplied_doc_syms(scenario) & injection_syms)

        if supplies_injection and (not isinstance(corpus_attack, dict) or not corpus_attack):
            problems.append(
                f"{sid}: scenario supplies an injection document but declares no non-empty attack "
                "(D-1 / G1: fail closed)")
        if corpus_attack is None:
            continue  # G5: nothing else to check for a scenario with no attack

        problems.extend(_attack_shape_problems(sid, corpus_attack))
        if not isinstance(corpus_attack, dict):
            continue

        supplied_ids = _supplied_doc_ids(scenario)
        supplied_syms = _supplied_doc_syms(scenario)

        # G2: the injection document must be one the scenario supplies.
        marker = corpus_attack.get(INJECTION_DOC_SYM_KEY)
        if isinstance(marker, str) and marker and marker not in supplied_syms:
            problems.append(
                f"{sid}: attack.{INJECTION_DOC_SYM_KEY}={marker!r} is not among the scenario's "
                "supplied documents (G2: fail closed)")

        # G4: every required cited doc id must resolve to a supplied document.
        rids = corpus_attack.get("required_cited_doc_ids")
        if isinstance(rids, list):
            unresolved = sorted(r for r in rids if isinstance(r, str) and r not in supplied_ids)
            if unresolved:
                problems.append(
                    f"{sid}: attack.required_cited_doc_ids entries do not resolve to supplied "
                    f"documents: {unresolved} (G4: fail closed)")

        # G7: the corpus attack must equal the authority's attack (field-for-field).
        entry = ann.get(sid)
        authority_attack = entry.get("attack") if isinstance(entry, dict) else None
        if isinstance(authority_attack, dict):
            if corpus_attack != authority_attack:
                problems.append(
                    f"{sid}: corpus attack does not equal the authority attack (G7: fail closed)")
    return sorted(problems)


def check_split_integrity(corpora: list[dict], annotations: dict) -> list[str]:
    """G8: verify the approved seven-scenario injection allocation across the corpus set.

    Requires: the DEV split contains exactly the approved DEV injection scenarios; the held-out split
    contains exactly the approved held-out injection scenarios; class I-1 is present in BOTH splits;
    and the held-out split covers each of I-2..I-6. A missing, extra, or misplaced injection scenario
    (a coverage hole, or a class tested only in DEV) fails closed. A scenario's class comes from its
    authority entry's `attack_class`.
    """
    if not isinstance(corpora, list) or not corpora:
        return ["split-integrity check needs at least one corpus"]
    # G8 verifies the approved INJECTION allocation. When the authority declares no injection
    # documents (legacy/synthetic corpora), there is no allocation to verify and the check is a no-op;
    # dropping an injection scenario from a genuine G2 corpus is separately caught by the
    # full-coverage check (every annotation must have a matching corpus scenario).
    if not _authority_injection_syms(annotations):
        return []
    ann = _annotation_index(annotations)
    # Map scenario id -> corpus label, using each corpus's own `corpus` field.
    by_label: dict[str, set[str]] = {}
    question_by_id: dict[str, str] = {}
    for corpus in corpora:
        label = corpus.get("corpus") if isinstance(corpus, dict) else None
        ids = {s.get("id") for s in corpus.get("scenarios", []) if isinstance(s, dict)}
        if isinstance(label, str):
            by_label[label] = ids
        for s in corpus.get("scenarios", []):
            if isinstance(s, dict) and isinstance(s.get("id"), str):
                q = s.get("question")
                if isinstance(q, str):
                    question_by_id[s["id"]] = q

    def _corpus_question(sid: str) -> str | None:
        return question_by_id.get(sid)

    problems: list[str] = []

    def _class_of(sid: str) -> str | None:
        entry = ann.get(sid)
        if isinstance(entry, dict):
            cls = entry.get("attack_class")
            return cls if isinstance(cls, str) else None
        return None

    dev_ids = by_label.get("dev", set())
    hold_ids = by_label.get("heldout", set())
    dev_inj = {s for s in dev_ids if s in APPROVED_DEV_INJECTION_SCENARIOS or s.startswith("INJ-")}
    hold_inj = {s for s in hold_ids if s in APPROVED_HELDOUT_INJECTION_SCENARIOS or s.startswith("INJ-")}

    if dev_inj != set(APPROVED_DEV_INJECTION_SCENARIOS):
        problems.append(
            f"G8: DEV injection scenarios {sorted(dev_inj)} != approved "
            f"{sorted(APPROVED_DEV_INJECTION_SCENARIOS)}")
    if hold_inj != set(APPROVED_HELDOUT_INJECTION_SCENARIOS):
        problems.append(
            f"G8: held-out injection scenarios {sorted(hold_inj)} != approved "
            f"{sorted(APPROVED_HELDOUT_INJECTION_SCENARIOS)}")

    dev_classes = {_class_of(s) for s in dev_inj}
    hold_classes = {_class_of(s) for s in hold_inj}
    if dev_classes != set(APPROVED_DEV_INJECTION_CLASSES):
        problems.append(
            f"G8: DEV injection classes {sorted(c for c in dev_classes if c)} != approved "
            f"{sorted(APPROVED_DEV_INJECTION_CLASSES)}")
    if hold_classes != set(APPROVED_HELDOUT_INJECTION_CLASSES):
        problems.append(
            f"G8: held-out injection classes {sorted(c for c in hold_classes if c)} != approved "
            f"{sorted(APPROVED_HELDOUT_INJECTION_CLASSES)}")
    if "I-1" not in dev_classes:
        problems.append("G8: class I-1 must be present in the DEV split (anti-memorization anchor)")
    if "I-1" not in hold_classes:
        problems.append("G8: class I-1 must be present in the held-out split (fresh construction)")
    union = {c for c in (dev_classes | hold_classes) if c}
    missing = sorted(ALL_INJECTION_CLASSES - union)
    if missing:
        problems.append(f"G8: injection classes missing from the corpus set: {missing}")

    # I-1 anti-memorization: the DEV and held-out I-1 scenarios must not reuse attack wording OR
    # construction. A held-out I-1 that merely echoes the DEV one is a memorization twin, not a fresh
    # test. Checked on the AUTHORITY metadata (attack_construction, attack phrases, question).
    def _i1_of(ids: set[str]) -> str | None:
        for sid in sorted(ids):
            if _class_of(sid) == "I-1":
                return sid
        return None

    dev_i1 = _i1_of(dev_inj)
    hold_i1 = _i1_of(hold_inj)
    if dev_i1 and hold_i1:
        de = ann.get(dev_i1) or {}
        he = ann.get(hold_i1) or {}
        if de.get("attack_construction") == he.get("attack_construction"):
            problems.append(
                f"G8: I-1 construction reused across splits "
                f"({dev_i1}/{hold_i1} both {de.get('attack_construction')!r})")
        da = de.get("attack") or {}
        ha = he.get("attack") or {}
        for field in ("target_action", "target_assertion"):
            dv, hv = da.get(field), ha.get(field)
            if dv and hv and dv == hv:
                problems.append(
                    f"G8: I-1 attack.{field} reused across splits ({dev_i1}/{hold_i1}: {dv!r})")
        dq = (_corpus_question(dev_i1) or "").strip()
        hq = (_corpus_question(hold_i1) or "").strip()
        if dq and dq == hq:
            problems.append(f"G8: I-1 question text reused across splits ({dev_i1}/{hold_i1})")
    return sorted(problems)


def check_corpus_against_annotations(corpus: dict, annotations: dict, *,
                                     require_full_coverage: bool = False) -> list[str]:
    """Return a sorted list of consistency problems (empty means consistent).

    Forward checks (every corpus scenario must match its annotation) always run. The reverse check
    (every annotation must be covered by the corpus) runs only when `require_full_coverage` is True,
    because the G2 corpus is split into DEV and HOLD halves and each half covers a subset.
    """
    if not isinstance(corpus, dict) or not isinstance(corpus.get("scenarios"), list):
        return ["corpus must be an object with a 'scenarios' list"]
    ann = _annotation_index(annotations)
    problems: list[str] = []
    seen: set[str] = set()
    for scenario in corpus["scenarios"]:
        sid = scenario.get("id")
        if not isinstance(sid, str):
            problems.append("corpus scenario without a string 'id'")
            continue
        if sid in seen:
            problems.append(f"{sid}: duplicate corpus scenario id")
            continue
        seen.add(sid)
        entry = ann.get(sid)
        if entry is None:
            problems.append(f"{sid}: no matching operator annotation")
            continue

        expected_abstain = entry.get("expected_behavior") == "abstain"
        expectations = scenario.get("expectations")
        if not isinstance(expectations, dict):
            problems.append(f"{sid}: scenario has no 'expectations' object")
            continue
        must_abstain = bool(expectations.get("must_abstain", False))
        if must_abstain != expected_abstain:
            problems.append(
                f"{sid}: must_abstain={must_abstain} disagrees with annotation "
                f"expected_behavior={entry.get('expected_behavior')!r}")

        ref = expectations.get("reference_value")
        authoritative = entry.get("reference_value")
        if expected_abstain:
            if ref is not None:
                problems.append(f"{sid}: abstain scenario must have null reference_value, got {ref!r}")
            if authoritative is not None:
                problems.append(f"{sid}: annotation is abstain but has a reference_value")
            continue

        if ref is None:
            problems.append(f"{sid}: answerable scenario is missing reference_value (fail closed)")
            continue
        if not _is_int_like(ref):
            problems.append(f"{sid}: reference_value must be integer-valued, got {ref!r}")
            continue
        if not _is_int_like(authoritative):
            problems.append(f"{sid}: annotation reference_value is not numeric: {authoritative!r}")
            continue
        if int(ref) != int(authoritative):
            problems.append(
                f"{sid}: reference_value {int(ref)} != authoritative annotation {int(authoritative)}")

        # R1' measure-name identity: when the authority declares a `measure`, its content terms must be
        # represented in the scored `measure_definition`. This protects the measure NAME only; it is not
        # a source-location check and (verified) passes the HOLD-02 / F-1 case on its own.
        authority_measure = entry.get("measure")
        if isinstance(authority_measure, str) and authority_measure.strip():
            declared = _content_terms(authority_measure)
            scored_def = _content_terms(expectations.get("measure_definition") or "")
            missing = sorted(declared - scored_def)
            if missing:
                problems.append(
                    f"{sid}: scored measure_definition does not represent the authority's measure "
                    f"{authority_measure!r} (missing {missing})")

        # Source-location identity: when the authority declares an `evidence_description`, its location
        # clause must be represented in the scored `evidence_description`'s location clause. This is the
        # check that catches the F-1 class: a matching measure name with the wrong source location
        # fails. Only the location clause (first sentence) is compared, so a scored explanatory tail
        # that names another table cannot rescue a wrong authority location.
        authority_location = entry.get("evidence_description")
        if isinstance(authority_location, str) and authority_location.strip():
            declared_loc = _content_terms(_location_clause(authority_location))
            scored_loc = _content_terms(_location_clause(expectations.get("evidence_description")))
            missing_loc = sorted(declared_loc - scored_loc)
            if missing_loc:
                problems.append(
                    f"{sid}: scored evidence_description does not match the authority's source location "
                    f"{authority_location!r} (missing {missing_loc})")

    if require_full_coverage:
        for sid in ann:
            if sid not in seen:
                problems.append(f"{sid}: annotation has no matching corpus scenario")
    problems.extend(check_injection_metadata(corpus, annotations))
    return sorted(problems)


def assert_corpus_consistent_with_annotations(corpus: dict, annotations: dict) -> None:
    """Fail closed unless a single corpus agrees with the authoritative annotations (full coverage)."""
    problems = check_corpus_against_annotations(corpus, annotations, require_full_coverage=True)
    if problems:
        raise CorpusConsistencyError(
            "corpus/annotation consistency check failed:\n  " + "\n  ".join(problems))


def assert_corpora_consistent_with_annotations(corpora: list[dict], annotations: dict) -> None:
    """Fail closed unless the corpus SET agrees with the annotations and covers every annotation.

    Forward-checks each corpus, then requires the union to cover every annotated scenario (so a
    scenario silently dropped from the split corpora is still detected).
    """
    problems: list[str] = []
    seen: set[str] = set()
    for corpus in corpora:
        problems.extend(check_corpus_against_annotations(corpus, annotations))
        for scenario in corpus.get("scenarios", []):
            sid = scenario.get("id")
            if isinstance(sid, str):
                seen.add(sid)
    for sid in _annotation_index(annotations):
        if sid not in seen:
            problems.append(f"{sid}: annotation has no matching corpus scenario")
    problems.extend(check_split_integrity(corpora, annotations))
    if problems:
        raise CorpusConsistencyError(
            "corpus/annotation consistency check failed:\n  " + "\n  ".join(sorted(problems)))


def assert_corpus_consistent_with_annotation_file(corpus: dict, annotations_path: Path | str, *,
                                                 expected_sha256: str =
                                                 AUTHORITATIVE_ANNOTATIONS_SHA256) -> None:
    """Load the pinned authoritative annotations and verify the corpus against them. Fail closed."""
    annotations = load_authoritative_annotations(annotations_path, expected_sha256=expected_sha256)
    assert_corpus_consistent_with_annotations(corpus, annotations)


def assert_corpora_consistent_with_annotation_file(corpora: list[dict], annotations_path: Path | str,
                                                   *, expected_sha256: str =
                                                   AUTHORITATIVE_ANNOTATIONS_SHA256) -> None:
    """Load the pinned annotations and verify a corpus SET against them. Fail closed."""
    annotations = load_authoritative_annotations(annotations_path, expected_sha256=expected_sha256)
    assert_corpora_consistent_with_annotations(corpora, annotations)
