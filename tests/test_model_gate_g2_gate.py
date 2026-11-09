"""CLI consistency-gate tests for genuine G2 evaluation (offline; no model, no network).

The gate must require the pinned annotation authority and verify BOTH corpora before any candidate is
scored, while leaving the synthetic/test-only fixture path unchanged.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from apex_investor import corpus_consistency
from apex_investor.corpus_consistency import is_g2_corpus
from apex_investor.model_gate import canonical_sha256, load_detector_spec

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures" / "model_gate"
G2_DEV = ROOT / "data" / "model_gate" / "g2_corpus.dev.json"
G2_HELDOUT = ROOT / "data" / "model_gate" / "g2_corpus.heldout.json"
G2_HELDOUT_MANIFEST = ROOT / "data" / "model_gate" / "g2_heldout_manifest.json"


def _load_cli():
    spec = importlib.util.spec_from_file_location("model_gate_cli_g2", ROOT / "scripts" / "model_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLI = _load_cli()
_, DETECTOR_SHA = load_detector_spec(ROOT / "data" / "model_gate" / "detector_spec.v1.json")


def _config():
    return json.loads((FIX / "config.example.json").read_text())


def _write_freeze(tmp_path: Path, config: dict) -> Path:
    freeze = {
        "schema_version": "1", "prompt_version": config["prompt_version"],
        "candidate_config_hash": canonical_sha256(config),
        "detector_spec_version": "1", "detector_spec_sha256": DETECTOR_SHA,
        "frozen_at": "2024-06-02T00:00:00Z",
    }
    p = tmp_path / "freeze.json"
    p.write_text(json.dumps(freeze))
    return p


def _minimal_runs(corpus_label: str, fixture_version: str) -> list[dict]:
    # Structurally valid (one baseline, no dupes); scoring yields no selection but must not raise.
    return [{
        "run_id": f"baseline-{corpus_label}", "candidate_id": "abstain-always",
        "provider": "test", "model": "abstain", "prompt_version": "m3-v1",
        "fixture_version": fixture_version, "corpus": corpus_label,
        "is_baseline": True, "responses": {},
    }]


def _g2_setup(tmp_path: Path, *, dev=None, heldout=None):
    """Write G2 dev/heldout corpora + manifest + runs; return CLI argv."""
    dev_corpus = json.loads(G2_DEV.read_text()) if dev is None else dev
    heldout_corpus = json.loads(G2_HELDOUT.read_text()) if heldout is None else heldout
    dev_path = tmp_path / "g2.dev.json"
    dev_path.write_text(json.dumps(dev_corpus))
    ho_path = tmp_path / "g2.heldout.json"
    ho_path.write_text(json.dumps(heldout_corpus))
    manifest = tmp_path / "g2.heldout.manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": "1",
        "heldout_fixture_version": heldout_corpus.get("fixture_version"),
        "heldout_fixture_sha256": canonical_sha256(heldout_corpus),
        "path": str(ho_path),
    }))
    dev_runs = tmp_path / "runs.dev.json"
    dev_runs.write_text(json.dumps(_minimal_runs("dev", dev_corpus.get("fixture_version", "1"))))
    ho_runs = tmp_path / "runs.heldout.json"
    ho_runs.write_text(json.dumps(_minimal_runs("heldout", heldout_corpus.get("fixture_version", "1"))))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    freeze = _write_freeze(tmp_path, _config())
    return [
        "--config", str(config_path), "--freeze-manifest", str(freeze),
        "--dev-corpus", str(dev_path), "--dev-runs", str(dev_runs),
        "--heldout-manifest", str(manifest), "--heldout-runs", str(ho_runs),
        "--out-dir", str(tmp_path / "out"),
    ]


def _artifacts(tmp_path: Path) -> list[Path]:
    return list((tmp_path / "out").glob("model_selection_*.json"))


# ---- gate passes on consistent G2 corpora ---------------------------------------------------------

def test_g2_gate_passes_and_writes_artifact(tmp_path):
    rc = CLI.main(_g2_setup(tmp_path))
    assert rc == 0
    assert len(_artifacts(tmp_path)) == 1


# ---- gate refuses on inconsistency (before any scoring) -------------------------------------------

def test_g2_gate_refuses_on_reference_value_drift(tmp_path):
    dev = json.loads(G2_DEV.read_text())
    dev["scenarios"][0]["expectations"]["reference_value"] = 999999
    rc = CLI.main(_g2_setup(tmp_path, dev=dev))
    assert rc == 2
    assert _artifacts(tmp_path) == []


def test_g2_gate_refuses_on_abstain_value(tmp_path):
    heldout = json.loads(G2_HELDOUT.read_text())
    for s in heldout["scenarios"]:
        if s["id"] == "HOLD-03":
            s["expectations"]["reference_value"] = 0
    rc = CLI.main(_g2_setup(tmp_path, heldout=heldout))
    assert rc == 2
    assert _artifacts(tmp_path) == []


def test_g2_gate_refuses_on_missing_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus_consistency, "default_annotations_path",
                        lambda: tmp_path / "missing_authority.json")
    rc = CLI.main(_g2_setup(tmp_path))
    assert rc == 2
    assert _artifacts(tmp_path) == []


def test_g2_gate_refuses_on_wrong_pinned_hash(tmp_path, monkeypatch):
    # A valid JSON authority with different bytes -> hash pin fails.
    other = tmp_path / "other_annotations.json"
    other.write_text(json.dumps({"annotation_version": "x", "scenarios": []}))
    monkeypatch.setattr(corpus_consistency, "default_annotations_path", lambda: other)
    rc = CLI.main(_g2_setup(tmp_path))
    assert rc == 2
    assert _artifacts(tmp_path) == []


def test_g2_gate_refuses_when_only_one_corpus_is_g2(tmp_path):
    # dev = genuine G2, heldout = synthetic (legacy instruction) -> refuse (mixed paths).
    synthetic = json.loads((FIX / "heldout_corpus.v1.json").read_text())
    rc = CLI.main(_g2_setup(tmp_path, heldout=synthetic))
    assert rc == 2
    assert _artifacts(tmp_path) == []


def test_g2_gate_refuses_on_missing_approval_record(tmp_path, monkeypatch):
    # Both corpora and the authority are intact, but the operator-approval record is missing -> refuse.
    monkeypatch.setattr(corpus_consistency, "default_approval_path",
                        lambda: tmp_path / "missing_approval.json")
    rc = CLI.main(_g2_setup(tmp_path))
    assert rc == 2
    assert _artifacts(tmp_path) == []


def test_g2_gate_refuses_on_tampered_approval_record(tmp_path, monkeypatch):
    # A valid-JSON approval record with altered bytes -> its raw-hash pin fails -> refuse.
    tampered = tmp_path / "approval.json"
    tampered.write_text(json.dumps({"approved_authority_raw_sha256": "0" * 64}))
    monkeypatch.setattr(corpus_consistency, "default_approval_path", lambda: tampered)
    rc = CLI.main(_g2_setup(tmp_path))
    assert rc == 2
    assert _artifacts(tmp_path) == []


# ---- synthetic/test-only path unchanged -----------------------------------------------------------

def test_synthetic_path_not_gated_and_unchanged(tmp_path):
    # The synthetic fixtures (legacy `instruction`) must NOT be treated as G2 and must still score.
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    freeze = _write_freeze(tmp_path, _config())
    manifest = ROOT / "data" / "model_gate" / "heldout_manifest.example.json"
    args = [
        "--config", str(config_path), "--freeze-manifest", str(freeze),
        "--dev-corpus", str(FIX / "corpus.dev.v1.json"), "--dev-runs", str(FIX / "runs" / "dev.json"),
        "--heldout-manifest", str(manifest), "--heldout-runs", str(FIX / "runs" / "heldout.json"),
        "--out-dir", str(tmp_path / "out"),
    ]
    rc = CLI.main(args)
    assert rc == 0
    assert len(_artifacts(tmp_path)) == 1


# ---- is_g2_corpus unit checks ---------------------------------------------------------------------

def test_is_g2_corpus_true_for_real_g2():
    assert is_g2_corpus(json.loads(G2_DEV.read_text()))
    assert is_g2_corpus(json.loads(G2_HELDOUT.read_text()))


def test_is_g2_corpus_false_for_synthetic():
    assert not is_g2_corpus(json.loads((FIX / "corpus.dev.v1.json").read_text()))


@pytest.mark.parametrize("value", [None, {}, {"scenarios": []}, {"scenarios": "x"}, 42])
def test_is_g2_corpus_false_for_non_g2_shapes(value):
    assert not is_g2_corpus(value)


# ---- source-controlled v2 held-out manifest (D5) --------------------------------------------------

def test_g2_heldout_manifest_is_source_controlled_and_self_consistent():
    manifest = json.loads(G2_HELDOUT_MANIFEST.read_text())
    hold = json.loads(G2_HELDOUT.read_text())
    assert manifest["schema_version"] == "1"
    assert manifest["heldout_fixture_version"] == hold["fixture_version"] == "g2-pilot-2-heldout"
    # The pinned hash is the APPROVED held-out corpus canonical hash.
    assert manifest["heldout_fixture_sha256"] == canonical_sha256(hold)
    # Points at the source-controlled corpus, not a synthetic fixture or a /tmp artifact.
    assert manifest["path"] == "data/model_gate/g2_corpus.heldout.json"
    assert "fixtures/model_gate" not in manifest["path"]
    assert not Path(manifest["path"]).is_absolute()


def test_g2_heldout_manifest_resolves_from_repo_root():
    manifest = json.loads(G2_HELDOUT_MANIFEST.read_text())
    resolved = ROOT / manifest["path"]
    assert resolved.is_file()
    corpus = json.loads(resolved.read_text())
    assert canonical_sha256(corpus) == manifest["heldout_fixture_sha256"]
    assert [s["id"] for s in corpus["scenarios"]] == ["HOLD-01", "HOLD-02", "HOLD-03"]


def test_g2_heldout_manifest_gate_passes_via_cli(tmp_path, monkeypatch):
    # End-to-end: the source-controlled manifest drives a genuine G2 run through the CLI gate.
    monkeypatch.chdir(ROOT)  # the CLI resolves the manifest `path` relative to CWD
    dev_corpus = json.loads(G2_DEV.read_text())
    dev_path = tmp_path / "g2.dev.json"
    dev_path.write_text(json.dumps(dev_corpus))
    dev_runs = tmp_path / "runs.dev.json"
    dev_runs.write_text(json.dumps(_minimal_runs("dev", dev_corpus["fixture_version"])))
    ho_runs = tmp_path / "runs.heldout.json"
    ho_runs.write_text(json.dumps(_minimal_runs("heldout", "g2-pilot-2-heldout")))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    freeze = _write_freeze(tmp_path, _config())
    args = [
        "--config", str(config_path), "--freeze-manifest", str(freeze),
        "--dev-corpus", str(dev_path), "--dev-runs", str(dev_runs),
        "--heldout-manifest", str(G2_HELDOUT_MANIFEST), "--heldout-runs", str(ho_runs),
        "--out-dir", str(tmp_path / "out"),
    ]
    rc = CLI.main(args)
    assert rc == 0
    assert len(_artifacts(tmp_path)) == 1
