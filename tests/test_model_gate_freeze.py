"""B1 CLI subcommand tests: freeze-verify / frozen-run / backward-compatible ordinary path."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from apex_investor.evaluator_identity import SCORING_CLOSURE_MEMBERS

ROOT = Path(__file__).resolve().parent.parent
AUTHORITY = ROOT / "data/model_gate/freeze_authority.v1.json"


def _load_cli():
    spec = importlib.util.spec_from_file_location("model_gate_cli_freeze",
                                                  ROOT / "scripts" / "model_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLI = _load_cli()
AUTHORITY_SHA = hashlib.sha256(AUTHORITY.read_bytes()).hexdigest()


def test_freeze_verify_accepts_the_trusted_authority(capsys):
    rc = CLI.main(["freeze-verify", "--authority-sha256", AUTHORITY_SHA])
    out = capsys.readouterr().out
    assert rc == 0
    assert "closure_sha256:" in out
    assert "environment:" in out


def test_freeze_verify_refuses_wrong_authority_sha(capsys):
    rc = CLI.main(["freeze-verify", "--authority-sha256", "0" * 64])
    assert rc == 2
    assert "refused" in capsys.readouterr().err


def test_freeze_verify_requires_authority_sha():
    with pytest.raises(SystemExit):
        CLI.main(["freeze-verify"])


def test_frozen_run_requires_authority_sha():
    with pytest.raises(SystemExit):
        CLI.main(["frozen-run"])


def test_frozen_run_refuses_before_scoring_on_bad_authority(capsys):
    # A bad authority must refuse BEFORE any scoring argument is parsed or any artifact is written.
    rc = CLI.main(["frozen-run", "--authority-sha256", "0" * 64])
    assert rc == 2
    assert "freeze authorization" in capsys.readouterr().err


def test_ordinary_bare_command_still_requires_its_args():
    # Backward compatibility: the ordinary path is unchanged; missing required args still exits 2.
    with pytest.raises(SystemExit):
        CLI.main([])


def test_ordinary_run_subcommand_alias_is_dispatched():
    with pytest.raises(SystemExit):
        CLI.main(["run"])


# ---- frozen-run: end-to-end schema "2" provenance ----------------------------------------------

FIX = ROOT / "tests" / "fixtures" / "model_gate"


def _frozen_run_args(tmp_path):
    from apex_investor.model_gate import canonical_sha256, load_detector_spec
    _, det_sha = load_detector_spec(ROOT / "data" / "model_gate" / "detector_spec.v1.json")
    config = json.loads((FIX / "config.example.json").read_text())
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(config))
    freeze = {"schema_version": "1", "prompt_version": config["prompt_version"],
              "candidate_config_hash": canonical_sha256(config), "detector_spec_version": "1",
              "detector_spec_sha256": det_sha, "frozen_at": "2024-06-02T00:00:00Z"}
    frz_path = tmp_path / "freeze.json"
    frz_path.write_text(json.dumps(freeze))
    return [
        "--authority-sha256", AUTHORITY_SHA,
        "--config", str(cfg_path), "--freeze-manifest", str(frz_path),
        "--dev-corpus", str(FIX / "corpus.dev.v1.json"),
        "--dev-runs", str(FIX / "runs" / "dev.json"),
        "--heldout-manifest", str(ROOT / "data" / "model_gate" / "heldout_manifest.example.json"),
        "--heldout-runs", str(FIX / "runs" / "heldout.json"),
        "--out-dir", str(tmp_path / "out"),
    ]


def test_frozen_run_writes_schema_2_artifact_with_provenance(tmp_path):
    rc = CLI.main(["frozen-run"] + _frozen_run_args(tmp_path))
    assert rc == 0
    arts = list((tmp_path / "out").glob("model_selection_*.json"))
    assert len(arts) == 1
    art = json.loads(arts[0].read_text())
    assert art["schema_version"] == "2"
    ev = art["evaluator"]
    assert set(ev["members"]) == set(SCORING_CLOSURE_MEMBERS)
    assert ev["closure_sha256"] == json.loads(
        (ROOT / "data/model_gate/evaluator_closure.v1.json").read_text())["closure_sha256"]
    assert ev["data_artifacts"]["annotation_authority_sha256"] == \
        "57d53b3c715c1ac030628f425cedc0561046866079ba2f9e2e4453a4a5ebd46c"
    assert ev["data_artifacts"]["prompt_sha256"] is None


def test_ordinary_run_still_writes_schema_1(tmp_path):
    rc = CLI.main(["run"] + _frozen_run_args(tmp_path)[2:])  # drop the freeze-verify-only flags
    assert rc == 0
    art = json.loads(next((tmp_path / "out").glob("model_selection_*.json")).read_text())
    assert art["schema_version"] == "1"
    assert art["evaluator"] is None
