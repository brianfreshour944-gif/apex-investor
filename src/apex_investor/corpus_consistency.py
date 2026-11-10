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
# g2-pilot-draft-3 (2026-11-09): operator-approved revision adding `evidence_description` to every
# scenario and correcting HOLD-02's source description under adjudication A. Supersedes 0bc0257b….
AUTHORITATIVE_ANNOTATIONS_SHA256 = (
    "3495633a12497a583517e39f06d4df7df6b6ed71d5a1ccfbaf2c8e0157f25016"
)

# The separate, source-controlled OPERATOR-APPROVAL record. Option (b): the authority stays a
# byte-exact copy of the frozen annotations; the operator approval event is recorded here and binds
# that approval to the authority by hash. This record is lean: it references the authority by hash and
# does NOT duplicate any reference value.
DEFAULT_APPROVAL_PATH = Path("data/model_gate/g2_annotation_approval.json")

# Raw-file SHA-256 of the approval record. Pinned so the approval's own bytes are frozen too: a change
# to the approval record (or its disappearance) is detected.
APPROVAL_RECORD_SHA256 = (
    "808f551febc25d1c16d63a34ecf9b81533a737a82c80dbbb4e7d35b5ce6999e3"
)

# The approval scope must cover every reference scenario, including the HOLD-03 abstention.
REQUIRED_SCOPE_SCENARIO_IDS = frozenset(
    {"DEV-01", "DEV-02", "DEV-03", "HOLD-01", "HOLD-02", "HOLD-03"}
)


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
