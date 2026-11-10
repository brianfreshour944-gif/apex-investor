"""Tests for corpus/annotation consistency enforcement (offline, deterministic, fail-closed).

The authority and corpora are source-controlled repo artifacts (data/model_gate/g2_*.json), so every
test here is reproducible from a clean checkout: no /tmp, no network, no model.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from apex_investor.corpus_consistency import (
    APPROVAL_RECORD_SHA256,
    AUTHORITATIVE_ANNOTATIONS_SHA256,
    DEFAULT_ANNOTATIONS_PATH,
    DEFAULT_APPROVAL_PATH,
    DEFAULT_DEV_MANIFEST_PATH,
    REQUIRED_SCOPE_SCENARIO_IDS,
    CorpusConsistencyError,
    assert_approval_record_valid,
    assert_corpora_consistent_with_annotation_file,
    assert_corpora_consistent_with_annotations,
    assert_corpus_consistent_with_annotations,
    assert_default_approval_valid,
    assert_default_corpora_consistent,
    assert_default_manifests_valid,
    check_approval_record,
    check_corpus_against_annotations,
    default_annotations_path,
    default_approval_path,
    default_corpus_paths,
    default_manifest_paths,
    load_approval_record,
    load_authoritative_annotations,
    load_default_authoritative_annotations,
    verify_corpus_manifest,
)
from apex_investor.model_gate import canonical_sha256

EXPECTED_SCENARIO_IDS = {"DEV-01", "DEV-02", "DEV-03", "HOLD-01", "HOLD-02", "HOLD-03"}


# ---- in-memory fixtures (pure logic) --------------------------------------------------------------

def corpus(scenarios):
    return {"fixture_version": "t", "corpus": "t", "scenarios": scenarios}


def scen(sid, must_abstain, reference_value=None, measure_definition=None, evidence_description=None):
    expectations = {"must_abstain": must_abstain, "reference_value": reference_value}
    if measure_definition is not None:
        expectations["measure_definition"] = measure_definition
    if evidence_description is not None:
        expectations["evidence_description"] = evidence_description
    return {"id": sid, "ticker": "X", "as_of": "2026-11-09", "question": "q",
            "documents": [], "expectations": expectations}


def annot(scenarios):
    return {"annotation_version": "t", "scenarios": scenarios}


def ascen(sid, behavior, reference_value=None, measure=None, evidence_description=None):
    entry = {"scenario_id": sid, "expected_behavior": behavior, "reference_value": reference_value}
    if measure is not None:
        entry["measure"] = measure
    if evidence_description is not None:
        entry["evidence_description"] = evidence_description
    return entry


# ---- happy path (pure) ----------------------------------------------------------------------------

def test_consistent_corpus_has_no_problems():
    c = corpus([scen("A", False, 100), scen("B", True, None)])
    a = annot([ascen("A", "answer", 100), ascen("B", "abstain", None)])
    assert check_corpus_against_annotations(c, a) == []
    assert_corpus_consistent_with_annotations(c, a)  # does not raise


# ---- drift detection (fail closed) ----------------------------------------------------------------

def test_mismatched_reference_value_flagged_and_raises():
    c = corpus([scen("A", False, 101)])
    a = annot([ascen("A", "answer", 100)])
    assert any("101" in p and "100" in p for p in check_corpus_against_annotations(c, a))
    with pytest.raises(CorpusConsistencyError):
        assert_corpus_consistent_with_annotations(c, a)


def test_abstain_scenario_with_non_null_reference_flagged():
    c = corpus([scen("B", True, 5)])
    a = annot([ascen("B", "abstain", None)])
    assert any("null reference_value" in p for p in check_corpus_against_annotations(c, a))
    with pytest.raises(CorpusConsistencyError):
        assert_corpus_consistent_with_annotations(c, a)


def test_missing_reference_for_answerable_flagged():
    c = corpus([scen("A", False, None)])
    a = annot([ascen("A", "answer", 100)])
    assert any("missing reference_value" in p for p in check_corpus_against_annotations(c, a))


def test_must_abstain_disagreement_flagged():
    c = corpus([scen("A", True, None)])
    a = annot([ascen("A", "answer", 100)])
    assert any("disagrees" in p for p in check_corpus_against_annotations(c, a))


# ---- R1' measure-name identity (pure) -------------------------------------------------------------

def test_measure_name_represented_passes():
    c = corpus([scen("A", False, 100,
                     measure_definition="GAAP total net sales for the fiscal year",
                     evidence_description="Segment Operating Performance; Total net sales row, 2026")])
    a = annot([ascen("A", "answer", 100, measure="total net sales",
                     evidence_description="Segment Operating Performance; Total net sales row, 2026")])
    assert check_corpus_against_annotations(c, a) == []


def test_measure_name_dropped_fails():
    c = corpus([scen("A", False, 100,
                     measure_definition="GAAP net income for the fiscal year",
                     evidence_description="Segment Operating Performance; Total net sales row, 2026")])
    a = annot([ascen("A", "answer", 100, measure="total net sales",
                     evidence_description="Segment Operating Performance; Total net sales row, 2026")])
    problems = check_corpus_against_annotations(c, a)
    assert any("measure_definition does not represent" in p and "sales" in p for p in problems)
    with pytest.raises(CorpusConsistencyError):
        assert_corpus_consistent_with_annotations(c, a)


def test_measure_check_skipped_when_authority_omits_measure():
    # Legacy/synthetic annotations without a `measure` are not subject to R1'.
    c = corpus([scen("A", False, 100)])
    a = annot([ascen("A", "answer", 100)])
    assert check_corpus_against_annotations(c, a) == []


def test_measure_check_skipped_for_abstain():
    c = corpus([scen("B", True, None)])
    a = annot([ascen("B", "abstain", None, measure="FY2027 first-quarter revenue")])
    assert check_corpus_against_annotations(c, a) == []


# ---- source-location identity (pure) --------------------------------------------------------------

def test_source_location_mismatch_same_measure_name_fails():
    # THE ACCEPTANCE CASE: the measure name matches, but the source location differs -> must fail.
    c = corpus([scen("A", False, 100,
                     measure_definition="GAAP total net sales for the fiscal year",
                     evidence_description="Consolidated Statements of Operations; Total net sales row, 2026")])
    a = annot([ascen("A", "answer", 100, measure="total net sales",
                     evidence_description="Segment Operating Performance; Total net sales row, 2026")])
    problems = check_corpus_against_annotations(c, a)
    assert any("source location" in p for p in problems)
    with pytest.raises(CorpusConsistencyError):
        assert_corpus_consistent_with_annotations(c, a)


def test_source_location_tail_naming_other_table_does_not_rescue_wrong_location():
    # A scored description whose TAIL names the consolidated statement must not match a wrong
    # authority location, because only the location clause (first sentence) is compared.
    wrong_scored = ("Segment Operating Performance; Total net sales row, 2026 column. "
                    "This is a segment-performance presentation, not the consolidated statement of operations.")
    c = corpus([scen("A", False, 100,
                     measure_definition="GAAP total net sales for the fiscal year",
                     evidence_description=wrong_scored)])
    a = annot([ascen("A", "answer", 100, measure="total net sales",
                     evidence_description="Consolidated Statements of Operations; Total net sales row, 2026")])
    problems = check_corpus_against_annotations(c, a)
    assert any("source location" in p for p in problems)


def test_source_location_correct_with_negating_tail_passes():
    # The genuine HOLD-02 shape: location clause matches; the tail negates the consolidated statement.
    scored = ("Segment Operating Performance; Total net sales row, 2026 column. "
              "This is a segment-performance presentation, not the consolidated statement of operations.")
    c = corpus([scen("A", False, 100,
                     measure_definition="GAAP total net sales for the fiscal year, segment operating performance",
                     evidence_description=scored)])
    a = annot([ascen("A", "answer", 100, measure="total net sales",
                     evidence_description="Segment Operating Performance; Total net sales row, 2026 column")])
    assert check_corpus_against_annotations(c, a) == []


def test_source_location_check_skipped_when_authority_omits_field():
    c = corpus([scen("A", False, 100)])
    a = annot([ascen("A", "answer", 100)])
    assert check_corpus_against_annotations(c, a) == []


def test_source_location_missing_in_corpus_fails():
    c = corpus([scen("A", False, 100, measure_definition="GAAP total net sales for the fiscal year")])
    a = annot([ascen("A", "answer", 100, measure="total net sales",
                     evidence_description="Segment Operating Performance; Total net sales row, 2026")])
    assert any("source location" in p for p in check_corpus_against_annotations(c, a))


def test_non_numeric_reference_flagged():
    c = corpus([scen("A", False, "100")])
    a = annot([ascen("A", "answer", 100)])
    assert any("integer-valued" in p for p in check_corpus_against_annotations(c, a))


def test_scenario_without_annotation_flagged():
    c = corpus([scen("A", False, 100), scen("Z", False, 1)])
    a = annot([ascen("A", "answer", 100)])
    assert any("Z: no matching operator annotation" in p
               for p in check_corpus_against_annotations(c, a))


def test_annotation_without_scenario_flagged():
    c = corpus([scen("A", False, 100)])
    a = annot([ascen("A", "answer", 100), ascen("Q", "answer", 9)])
    assert any("Q: annotation has no matching corpus scenario" in p
               for p in check_corpus_against_annotations(c, a, require_full_coverage=True))


def test_duplicate_corpus_scenario_id_flagged():
    c = corpus([scen("A", False, 100), scen("A", False, 100)])
    a = annot([ascen("A", "answer", 100)])
    assert any("duplicate corpus scenario id" in p for p in check_corpus_against_annotations(c, a))


# ---- corpus SET (DEV + HOLD split) ----------------------------------------------------------------

def test_split_corpora_cover_all_annotations():
    dev = corpus([scen("A", False, 100)])
    hold = corpus([scen("B", True, None)])
    a = annot([ascen("A", "answer", 100), ascen("B", "abstain", None)])
    assert_corpora_consistent_with_annotations([dev, hold], a)  # union covers all -> no raise


def test_split_corpora_missing_annotation_scenario_flagged():
    dev = corpus([scen("A", False, 100)])
    a = annot([ascen("A", "answer", 100), ascen("B", "abstain", None)])
    with pytest.raises(CorpusConsistencyError):
        assert_corpora_consistent_with_annotations([dev], a)  # B uncovered


# ---- authority pin: missing / modified / wrong hash (fail closed) ---------------------------------

def test_authority_artifact_exists_and_hash_matches_pin():
    p = default_annotations_path()
    assert p.exists(), f"authority artifact missing: {p}"
    assert hashlib.sha256(p.read_bytes()).hexdigest() == AUTHORITATIVE_ANNOTATIONS_SHA256


def test_missing_annotation_file_fails_closed(tmp_path):
    with pytest.raises(CorpusConsistencyError):
        load_authoritative_annotations(tmp_path / "does_not_exist.json")


def test_modified_annotation_bytes_fails_closed(tmp_path):
    src = default_annotations_path()
    data = json.loads(src.read_text())
    data["scenarios"][0]["reference_value"] = 1  # tamper
    f = tmp_path / "g2_annotations.json"
    f.write_text(json.dumps(data, indent=1))
    # content differs from the pin -> refuse even though it is valid JSON
    with pytest.raises(CorpusConsistencyError):
        load_authoritative_annotations(f)


def test_wrong_pinned_hash_fails_closed():
    with pytest.raises(CorpusConsistencyError):
        load_authoritative_annotations(default_annotations_path(),
                                       expected_sha256="0" * 64)


def test_annotation_without_scenarios_list_fails_closed(tmp_path):
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"annotation_version": "t"}))
    good = hashlib.sha256(f.read_bytes()).hexdigest()
    with pytest.raises(CorpusConsistencyError):
        load_authoritative_annotations(f, expected_sha256=good)


# ---- real, source-controlled artifacts (reproducible from a clean checkout) -----------------------

def test_default_authority_loads_and_covers_all_six_scenarios():
    a = load_default_authoritative_annotations()
    ids = {s["scenario_id"] for s in a["scenarios"]}
    assert ids == EXPECTED_SCENARIO_IDS


def test_real_corpora_cover_all_six_scenarios():
    dev_path, heldout_path = default_corpus_paths()
    dev = json.loads(dev_path.read_text())
    heldout = json.loads(heldout_path.read_text())
    ids = {s["id"] for s in dev["scenarios"]} | {s["id"] for s in heldout["scenarios"]}
    assert ids == EXPECTED_SCENARIO_IDS


def test_real_corpora_are_consistent_with_authority():
    # The reproducible, clean-checkout replacement for the old /tmp integration test.
    assert_default_corpora_consistent()  # does not raise


def test_real_authority_matches_its_declared_source_path():
    # The repo authority is the pinned copy; the path constant must point at it.
    assert default_annotations_path().name == DEFAULT_ANNOTATIONS_PATH.name


def test_tampered_real_corpus_is_refused(tmp_path):
    # Prove the real-artifact path rejects drift (not merely accepts the current drafts).
    dev_path, heldout_path = default_corpus_paths()
    dev = json.loads(dev_path.read_text())
    for s in dev["scenarios"]:
        if s["id"] == "DEV-01":
            s["expectations"]["reference_value"] = 999999
    heldout = json.loads(heldout_path.read_text())
    with pytest.raises(CorpusConsistencyError):
        assert_corpora_consistent_with_annotation_file(
            [dev, heldout], default_annotations_path())


def test_third_copy_guard_corpus_and_authority_agree():
    # Guards the two-copy invariant: the corpus copies of reference values must equal the authority's,
    # so no third independent copy can silently disagree.
    a = load_default_authoritative_annotations()
    authority = {s["scenario_id"]: s.get("reference_value") for s in a["scenarios"]}
    seen: dict[str, object] = {}
    for path in default_corpus_paths():
        for s in json.loads(path.read_text())["scenarios"]:
            seen[s["id"]] = s["expectations"].get("reference_value")
    assert seen == authority


# ---- real-artifact measure/location + manifest (clean checkout) ------------------------------------

def test_real_authority_declares_measure_and_location_for_every_answerable():
    a = load_default_authoritative_annotations()
    for s in a["scenarios"]:
        if s["expected_behavior"] == "answer":
            assert isinstance(s.get("measure"), str) and s["measure"].strip()
            assert isinstance(s.get("evidence_description"), str) and s["evidence_description"].strip()


def test_real_corpus_passes_measure_and_location_checks():
    a = load_default_authoritative_annotations()
    dev_path, heldout_path = default_corpus_paths()
    dev = json.loads(dev_path.read_text())
    heldout = json.loads(heldout_path.read_text())
    assert check_corpus_against_annotations(dev, a) == []
    assert check_corpus_against_annotations(heldout, a) == []


def test_real_hold02_location_mismatch_is_refused():
    # The acceptance case against the REAL artifacts: same measure name, wrong source location.
    a = load_default_authoritative_annotations()
    dev_path, heldout_path = default_corpus_paths()
    dev = json.loads(dev_path.read_text())
    heldout = json.loads(heldout_path.read_text())
    for s in heldout["scenarios"]:
        if s["id"] == "HOLD-02":
            s["expectations"]["evidence_description"] = (
                "Consolidated Statements of Operations; Total net sales row, 2026 column")
    with pytest.raises(CorpusConsistencyError):
        assert_corpora_consistent_with_annotations([dev, heldout], a)


def test_real_dev_manifest_exists_and_matches_corpus():
    dev_manifest_path, heldout_manifest_path = default_manifest_paths()
    assert dev_manifest_path.exists(), f"DEV manifest missing: {dev_manifest_path}"
    assert heldout_manifest_path.exists()
    assert_default_manifests_valid()  # does not raise


def test_real_dev_manifest_pins_canonical_hash():
    dev_manifest_path, _ = default_manifest_paths()
    manifest = json.loads(dev_manifest_path.read_text())
    dev_path, _ = default_corpus_paths()
    dev = json.loads(dev_path.read_text())
    assert manifest["fixture_sha256"] == canonical_sha256(dev)
    assert manifest["path"] == "data/model_gate/g2_corpus.dev.json"
    assert default_manifest_paths()[0].name == DEFAULT_DEV_MANIFEST_PATH.name


def test_dev_and_heldout_manifests_are_independently_verifiable():
    dev_manifest_path, heldout_manifest_path = default_manifest_paths()
    dev_manifest = json.loads(dev_manifest_path.read_text())
    heldout_manifest = json.loads(heldout_manifest_path.read_text())
    # Distinct pins; each names its own corpus path.
    assert dev_manifest["fixture_sha256"] != heldout_manifest["heldout_fixture_sha256"]
    assert dev_manifest["path"] != heldout_manifest["path"]


def test_tampered_dev_corpus_fails_manifest_check():
    dev_path, heldout_path = default_corpus_paths()
    dev = json.loads(dev_path.read_text())
    heldout = json.loads(heldout_path.read_text())
    dev["scenarios"][0]["expectations"]["reference_value"] = 999999
    with pytest.raises(CorpusConsistencyError):
        assert_default_manifests_valid([dev, heldout])


def test_default_corpora_consistent_also_verifies_dev_manifest(monkeypatch):
    monkeypatch.setattr("apex_investor.corpus_consistency.default_manifest_paths",
                        lambda: (Path("/nonexistent/dev.json"), Path("/nonexistent/held.json")))
    with pytest.raises(CorpusConsistencyError):
        assert_default_corpora_consistent()


def test_verify_corpus_manifest_flags_hash_mismatch():
    c = corpus([scen("A", False, 100)])
    problems = verify_corpus_manifest(c, {"path": "x", "fixture_sha256": "0" * 64}, label="dev")
    assert any("dev" in p and "canonical hash" in p for p in problems)


def test_verify_corpus_manifest_accepts_matching_hash():
    c = corpus([scen("A", False, 100)])
    manifest = {"path": "x", "fixture_sha256": canonical_sha256(c)}
    assert verify_corpus_manifest(c, manifest, label="dev") == []


# ---- operator-approval record: pin + bind + scope (fail closed) -----------------------------------

def _approval(**overrides):
    base = {
        "record_type": "g2-annotation-approval",
        "approved_by": "operator",
        "approved_at": "2026-11-09",
        "approved_authority_raw_sha256": AUTHORITATIVE_ANNOTATIONS_SHA256,
        "source_frozen_sha256": AUTHORITATIVE_ANNOTATIONS_SHA256,
        "scope": {"scenario_ids": sorted(REQUIRED_SCOPE_SCENARIO_IDS)},
    }
    base.update(overrides)
    return base


def _write_approval(tmp_path, data):
    p = tmp_path / "g2_annotation_approval.json"
    p.write_text(json.dumps(data))
    return p, hashlib.sha256(p.read_bytes()).hexdigest()


def test_real_approval_record_exists_and_matches_pin():
    p = default_approval_path()
    assert p.exists(), f"approval record missing: {p}"
    assert hashlib.sha256(p.read_bytes()).hexdigest() == APPROVAL_RECORD_SHA256
    # The declared path constant must point at the same source-controlled file.
    assert default_approval_path().name == DEFAULT_APPROVAL_PATH.name


def test_real_approval_record_binds_to_authority_and_covers_scope():
    rec = assert_default_approval_valid()  # does not raise
    assert rec["approved_authority_raw_sha256"] == AUTHORITATIVE_ANNOTATIONS_SHA256
    assert set(rec["scope"]["scenario_ids"]) >= REQUIRED_SCOPE_SCENARIO_IDS


def test_real_approval_record_is_lean_and_duplicates_no_reference_values():
    # Option (b): the approval record must NOT duplicate any reference value; the authority is the
    # single source of truth.
    raw = default_approval_path().read_text()
    rec = json.loads(raw)
    assert "scenarios" not in rec and "reference_value" not in raw
    authority_values = {s.get("reference_value")
                        for s in load_default_authoritative_annotations()["scenarios"]}
    for v in authority_values:
        if v is not None:
            assert str(v) not in raw


def test_default_corpora_consistent_also_verifies_approval(monkeypatch):
    # The default consistency entry point must verify BOTH artifacts: break the approval -> refuse.
    monkeypatch.setattr("apex_investor.corpus_consistency.default_approval_path",
                        lambda: Path("/nonexistent/approval.json"))
    with pytest.raises(CorpusConsistencyError):
        assert_default_corpora_consistent()


def test_approval_missing_fails_closed(tmp_path):
    with pytest.raises(CorpusConsistencyError):
        load_approval_record(tmp_path / "missing.json")


def test_approval_hash_mismatch_fails_closed(tmp_path):
    p, _ = _write_approval(tmp_path, _approval())
    with pytest.raises(CorpusConsistencyError):
        load_approval_record(p, expected_sha256="0" * 64)


def test_approval_valid_passes(tmp_path):
    p, sha = _write_approval(tmp_path, _approval())
    rec = assert_approval_record_valid(p, authority_raw_sha256=AUTHORITATIVE_ANNOTATIONS_SHA256,
                                       expected_sha256=sha)
    assert rec["approved_by"] == "operator"


def test_approval_authority_hash_mismatch_fails_closed(tmp_path):
    p, sha = _write_approval(tmp_path, _approval(approved_authority_raw_sha256="a" * 64))
    with pytest.raises(CorpusConsistencyError):
        assert_approval_record_valid(p, authority_raw_sha256=AUTHORITATIVE_ANNOTATIONS_SHA256,
                                     expected_sha256=sha)


def test_approval_authority_hash_mismatch_vs_actual_file_fails_closed(tmp_path):
    # Approval pins the correct authority, but the authority file on disk differs -> refuse.
    p, sha = _write_approval(tmp_path, _approval())
    with pytest.raises(CorpusConsistencyError):
        assert_approval_record_valid(p, authority_raw_sha256="b" * 64, expected_sha256=sha)


def test_approval_frozen_source_mismatch_fails_closed(tmp_path):
    p, sha = _write_approval(tmp_path, _approval(source_frozen_sha256="c" * 64))
    with pytest.raises(CorpusConsistencyError):
        assert_approval_record_valid(p, authority_raw_sha256=AUTHORITATIVE_ANNOTATIONS_SHA256,
                                     expected_sha256=sha)


def test_approval_incomplete_scope_fails_closed(tmp_path):
    p, sha = _write_approval(tmp_path, _approval(scope={"scenario_ids": ["DEV-01", "DEV-02"]}))
    problems = check_approval_record(json.loads(p.read_text()),
                                     authority_raw_sha256=AUTHORITATIVE_ANNOTATIONS_SHA256)
    assert any("scope" in x and "HOLD-03" in x for x in problems)
    with pytest.raises(CorpusConsistencyError):
        assert_approval_record_valid(p, authority_raw_sha256=AUTHORITATIVE_ANNOTATIONS_SHA256,
                                     expected_sha256=sha)


def test_approval_missing_hold03_abstention_flagged(tmp_path):
    scope = sorted(REQUIRED_SCOPE_SCENARIO_IDS - {"HOLD-03"})
    p, sha = _write_approval(tmp_path, _approval(scope={"scenario_ids": scope}))
    with pytest.raises(CorpusConsistencyError):
        assert_approval_record_valid(p, authority_raw_sha256=AUTHORITATIVE_ANNOTATIONS_SHA256,
                                     expected_sha256=sha)


def test_approval_no_scope_list_fails_closed(tmp_path):
    p, sha = _write_approval(tmp_path, _approval(scope={"description": "x"}))
    with pytest.raises(CorpusConsistencyError):
        assert_approval_record_valid(p, authority_raw_sha256=AUTHORITATIVE_ANNOTATIONS_SHA256,
                                     expected_sha256=sha)


def test_approval_non_object_fails_closed(tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps([1, 2, 3]))
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    with pytest.raises(CorpusConsistencyError):
        load_approval_record(p, expected_sha256=sha)


def test_approval_not_json_fails_closed(tmp_path):
    p = tmp_path / "a.json"
    p.write_text("{not json")
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    with pytest.raises(CorpusConsistencyError):
        load_approval_record(p, expected_sha256=sha)

