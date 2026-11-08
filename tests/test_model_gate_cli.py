"""M3a CLI / freeze-contract tests (offline; the CLI scores recorded runs, no live model)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from apex_investor.model_gate import canonical_sha256, load_detector_spec

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures" / "model_gate"


def _load_cli():
    spec = importlib.util.spec_from_file_location("model_gate_cli", ROOT / "scripts" / "model_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLI = _load_cli()
_, DETECTOR_SHA = load_detector_spec(ROOT / "data" / "model_gate" / "detector_spec.v1.json")


def _config():
    return json.loads((FIX / "config.example.json").read_text())


def _write_freeze(tmp_path: Path, config: dict, *, prompt_version=None, detector_sha=None) -> Path:
    freeze = {
        "schema_version": "1",
        "prompt_version": config["prompt_version"] if prompt_version is None else prompt_version,
        "candidate_config_hash": canonical_sha256(config),
        "detector_spec_version": "1",
        "detector_spec_sha256": DETECTOR_SHA if detector_sha is None else detector_sha,
        "frozen_at": "2024-06-02T00:00:00Z",
    }
    p = tmp_path / "freeze.json"
    p.write_text(json.dumps(freeze))
    return p


def _heldout_manifest() -> Path:
    return ROOT / "data" / "model_gate" / "heldout_manifest.example.json"


def _args(tmp_path, freeze, config):
    return [
        "--config", str(config),
        "--freeze-manifest", str(freeze),
        "--dev-corpus", str(FIX / "corpus.dev.v1.json"),
        "--dev-runs", str(FIX / "runs" / "dev.json"),
        "--heldout-manifest", str(_heldout_manifest()),
        "--heldout-runs", str(FIX / "runs" / "heldout.json"),
        "--out-dir", str(tmp_path / "out"),
    ]


def test_cli_writes_artifact(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    freeze = _write_freeze(tmp_path, _config())
    rc = CLI.main(_args(tmp_path, freeze, config_path))
    assert rc == 0
    artifacts = list((tmp_path / "out").glob("model_selection_*.json"))
    assert len(artifacts) == 1
    art = json.loads(artifacts[0].read_text())
    assert art["selection"]["selected_candidate_id"] == "cand-a"


def test_cli_refuses_on_config_hash_mismatch(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    # freeze computed over a DIFFERENT config
    freeze = _write_freeze(tmp_path, {"prompt_version": "m3-v1", "candidates": []})
    rc = CLI.main(_args(tmp_path, freeze, config_path))
    assert rc == 2


def test_cli_refuses_on_prompt_version_mismatch(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    freeze = _write_freeze(tmp_path, _config(), prompt_version="m3-v999")
    rc = CLI.main(_args(tmp_path, freeze, config_path))
    assert rc == 2


def test_cli_refuses_on_detector_hash_mismatch(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    freeze = _write_freeze(tmp_path, _config(), detector_sha="0" * 64)
    rc = CLI.main(_args(tmp_path, freeze, config_path))
    assert rc == 2


def test_cli_refuses_when_heldout_hash_mismatches(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    freeze = _write_freeze(tmp_path, _config())
    bad_manifest = tmp_path / "heldout_manifest.json"
    bad_manifest.write_text(json.dumps({
        "schema_version": "1", "heldout_fixture_version": "1",
        "heldout_fixture_sha256": "0" * 64,
        "path": str(FIX / "heldout_corpus.v1.json"),
    }))
    args = _args(tmp_path, freeze, config_path)
    args[args.index("--heldout-manifest") + 1] = str(bad_manifest)
    assert CLI.main(args) == 2


# ---- P4: freeze manifest version validation -----------------------------------------------------

_DEL = object()


def _write_freeze_raw(tmp_path, config, **overrides):
    freeze = {
        "schema_version": "1",
        "prompt_version": config["prompt_version"],
        "candidate_config_hash": canonical_sha256(config),
        "detector_spec_version": "1",
        "detector_spec_sha256": DETECTOR_SHA,
        "frozen_at": "2024-06-02T00:00:00Z",
    }
    for key, value in overrides.items():
        if value is _DEL:
            freeze.pop(key, None)
        else:
            freeze[key] = value
    p = tmp_path / "freeze.json"
    p.write_text(json.dumps(freeze))
    return p


def _p4_config(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    return config_path


def test_p4_valid_manifest_passes(tmp_path):
    config_path = _p4_config(tmp_path)
    freeze = _write_freeze_raw(tmp_path, _config())
    assert CLI.main(_args(tmp_path, freeze, config_path)) == 0


def test_p4_missing_schema_version_refused(tmp_path):
    config_path = _p4_config(tmp_path)
    freeze = _write_freeze_raw(tmp_path, _config(), schema_version=_DEL)
    assert CLI.main(_args(tmp_path, freeze, config_path)) == 2


def test_p4_missing_detector_spec_version_refused(tmp_path):
    config_path = _p4_config(tmp_path)
    freeze = _write_freeze_raw(tmp_path, _config(), detector_spec_version=_DEL)
    assert CLI.main(_args(tmp_path, freeze, config_path)) == 2


def test_p4_unsupported_schema_version_refused(tmp_path):
    config_path = _p4_config(tmp_path)
    freeze = _write_freeze_raw(tmp_path, _config(), schema_version="999")
    assert CLI.main(_args(tmp_path, freeze, config_path)) == 2


def test_p4_mismatched_detector_spec_version_refused(tmp_path):
    config_path = _p4_config(tmp_path)
    freeze = _write_freeze_raw(tmp_path, _config(), detector_spec_version="999")
    assert CLI.main(_args(tmp_path, freeze, config_path)) == 2


def test_p4_malformed_schema_version_refused(tmp_path):
    config_path = _p4_config(tmp_path)
    freeze = _write_freeze_raw(tmp_path, _config(), schema_version=1)  # non-string
    assert CLI.main(_args(tmp_path, freeze, config_path)) == 2


def test_p4_malformed_detector_spec_version_refused(tmp_path):
    config_path = _p4_config(tmp_path)
    freeze = _write_freeze_raw(tmp_path, _config(), detector_spec_version=["1"])  # non-string
    assert CLI.main(_args(tmp_path, freeze, config_path)) == 2


def test_p4_malformed_manifest_json_refused(tmp_path):
    config_path = _p4_config(tmp_path)
    bad = tmp_path / "freeze.json"
    bad.write_text("{not valid json")
    assert CLI.main(_args(tmp_path, bad, config_path)) == 2


def test_p4_missing_manifest_file_refused(tmp_path):
    config_path = _p4_config(tmp_path)
    missing = tmp_path / "does_not_exist.json"
    assert CLI.main(_args(tmp_path, missing, config_path)) == 2


_SPEC_PATH = ROOT / "data" / "model_gate" / "detector_spec.v1.json"


def _spec_without_or_with_version(tmp_path, *, delete=False, value=_DEL):
    spec = json.loads(_SPEC_PATH.read_text())
    if delete:
        spec.pop("detector_spec_version", None)
    else:
        spec["detector_spec_version"] = value
    p = tmp_path / "detector_spec.json"
    p.write_text(json.dumps(spec))
    return p


def _args_with_spec(tmp_path, freeze, config, spec_path):
    args = _args(tmp_path, freeze, config)
    args += ["--detector-spec", str(spec_path)]
    return args


def test_p4_detector_spec_missing_version_refused(tmp_path):
    # The manifest must NOT be able to compensate for an invalid detector spec. Even a manifest whose
    # detector_spec_version is the literal string "None" cannot pass when the spec omits the field.
    config_path = _p4_config(tmp_path)
    freeze = _write_freeze_raw(tmp_path, _config(), detector_spec_version="None")
    bad_spec = _spec_without_or_with_version(tmp_path, delete=True)
    assert CLI.main(_args_with_spec(tmp_path, freeze, config_path, bad_spec)) == 2


def test_p4_detector_spec_malformed_version_refused(tmp_path):
    config_path = _p4_config(tmp_path)
    for bad in ("", "   ", ["1"], 1.5):
        freeze = _write_freeze_raw(tmp_path, _config(), detector_spec_version="None")
        bad_spec = _spec_without_or_with_version(tmp_path, value=bad)
        assert CLI.main(_args_with_spec(tmp_path, freeze, config_path, bad_spec)) == 2, bad


def test_p4_detector_spec_unsupported_integer_version_refused(tmp_path):
    # An integer version is well-formed, but agreement with the manifest is not compatibility: an
    # unsupported integer (2) must be refused even if the manifest could be made to match it.
    config_path = _p4_config(tmp_path)
    freeze = _write_freeze_raw(tmp_path, _config(), detector_spec_version="2")
    bad_spec = _spec_without_or_with_version(tmp_path, value=2)  # int, unsupported
    assert CLI.main(_args_with_spec(tmp_path, freeze, config_path, bad_spec)) == 2


# ---- P3: the --lexicon argument is authoritative -------------------------------------------------

LEX = ROOT / "data" / "model_gate" / "lexicon.v1.json"


def _with_lexicon(tmp_path, freeze, config, lex_path):
    args = _args(tmp_path, freeze, config)
    args += ["--lexicon", str(lex_path)]
    return args


def test_p3_default_lexicon_consumed_and_matches(tmp_path):
    # The default lexicon file is loaded and must match the spec's embedded lexicon -> success.
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    freeze = _write_freeze(tmp_path, _config())
    rc = CLI.main(_with_lexicon(tmp_path, freeze, config_path, LEX))
    assert rc == 0
    assert len(list((tmp_path / "out").glob("model_selection_*.json"))) == 1


def test_p3_supplied_lexicon_with_matching_content_passes(tmp_path):
    # A different PATH with canonically-equal content (re-serialized) still passes.
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    freeze = _write_freeze(tmp_path, _config())
    reordered = json.loads(LEX.read_text())
    alt = tmp_path / "lexicon_alt.json"
    alt.write_text(json.dumps(reordered, indent=4))  # different bytes, same canonical content
    assert CLI.main(_with_lexicon(tmp_path, freeze, config_path, alt)) == 0


def test_p3_mismatched_lexicon_refused(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    freeze = _write_freeze(tmp_path, _config())
    bad = json.loads(LEX.read_text())
    bad["verbs"]["buy"] = ["buy", "buys", "buying", "bought", "purchase"]  # behavior-affecting change
    bad_path = tmp_path / "lexicon_bad.json"
    bad_path.write_text(json.dumps(bad))
    assert CLI.main(_with_lexicon(tmp_path, freeze, config_path, bad_path)) == 2


def test_p3_malformed_lexicon_refused(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    freeze = _write_freeze(tmp_path, _config())
    malformed = tmp_path / "lexicon_malformed.json"
    malformed.write_text(json.dumps({"version": "1", "verbs": {}}))  # empty verbs -> invalid structure
    assert CLI.main(_with_lexicon(tmp_path, freeze, config_path, malformed)) == 2


def test_p3_missing_lexicon_file_refused(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    freeze = _write_freeze(tmp_path, _config())
    missing = tmp_path / "does_not_exist.json"
    assert CLI.main(_with_lexicon(tmp_path, freeze, config_path, missing)) == 2


def test_p3_non_json_lexicon_refused(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    freeze = _write_freeze(tmp_path, _config())
    notjson = tmp_path / "lexicon.txt"
    notjson.write_text("not json at all")
    assert CLI.main(_with_lexicon(tmp_path, freeze, config_path, notjson)) == 2


def test_p3_unexpected_key_lexicon_refused(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    freeze = _write_freeze(tmp_path, _config())
    bad = json.loads(LEX.read_text())
    bad["extra_field"] = "not in the approved schema"
    bad_path = tmp_path / "lexicon_extra.json"
    bad_path.write_text(json.dumps(bad))
    assert CLI.main(_with_lexicon(tmp_path, freeze, config_path, bad_path)) == 2


def test_p3_invalid_utf8_lexicon_refused(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    freeze = _write_freeze(tmp_path, _config())
    bad_bytes = tmp_path / "lexicon_badbytes.json"
    bad_bytes.write_bytes(b"\xff\xfe")  # not valid UTF-8 -> UnicodeDecodeError, must not escape
    assert CLI.main(_with_lexicon(tmp_path, freeze, config_path, bad_bytes)) == 2
