#!/usr/bin/env python3
"""Operator CLI for the M3a model quality gate (the ONLY live-network surface).

Offline by default: it scores pre-recorded candidate runs. Live model capture is a separate,
explicitly-operator action; this CLI does not itself call a model.

Held-out gating (fail closed, in this order):
  1. load + verify the frozen-run manifest (candidate_config_hash, prompt_version, detector hash);
  2. refuse held-out scoring if the manifest is absent, the config hash differs, the prompt version
     differs, or the detector-spec hash differs;
  3. load the held-out corpus from the operator path and verify its sha256 against the manifest.

It never overwrites an artifact (unique, timestamped filename).
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apex_investor.model_gate import (  # noqa: E402
    DEFAULT_ARTIFACT_DIR,
    DEFAULT_DETECTOR_SPEC_PATH,
    DEFAULT_LEXICON_PATH,
    ModelGateError,
    Thresholds,
    build_artifact,
    canonical_sha256,
    load_detector_spec,
    load_lexicon,
    materialize_corpus,
    normalize_version,
    run_selection,
    verify_lexicon_matches_spec,
    write_artifact,
)

ROOT = Path(__file__).resolve().parent.parent

KNOWN_LIMITATIONS = {
    "version": "1",
    "items": [
        "P1 lexical rule cannot separate trading 'cover' from journalistic 'cover' "
        "(cover-journalistic-limitation is an accepted false positive).",
        "Directive detection is lexical, not semantic: unlisted phrasings are false negatives.",
        "Instrument proximity does not prove a verb is a trading instruction (false-positive risk).",
        "Passing the fixture set demonstrates coverage of tested cases only, not general "
        "recommendation or prompt-injection resistance.",
    ],
}


def _load_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelGateError(f"cannot read {path}: {exc}") from exc


_SUPPORTED_SCHEMA_VERSIONS = frozenset({"1"})


def _verify_freeze(freeze_manifest: Path, config: dict, detector_sha: str, spec_version) -> dict:
    expected_version = normalize_version(spec_version)
    if expected_version is None:
        raise ModelGateError("detector spec is missing a valid detector_spec_version")
    freeze = _load_json(freeze_manifest)
    if not isinstance(freeze, dict):
        raise ModelGateError("frozen-run manifest must be a JSON object")
    schema_version = freeze.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version:
        raise ModelGateError("frozen-run schema_version must be a non-empty string")
    if schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
        raise ModelGateError(f"frozen-run schema_version {schema_version!r} is unsupported")
    manifest_version = normalize_version(freeze.get("detector_spec_version"))
    if manifest_version is None:
        raise ModelGateError("frozen-run detector_spec_version must be a non-empty string")
    if manifest_version != expected_version:
        raise ModelGateError(
            f"frozen-run detector_spec_version {freeze.get('detector_spec_version')!r} does not match "
            f"the detector spec {expected_version!r}")
    if freeze.get("candidate_config_hash") != canonical_sha256(config):
        raise ModelGateError("frozen-run config hash does not match the config being scored")
    if freeze.get("prompt_version") != config.get("prompt_version"):
        raise ModelGateError("frozen-run prompt version does not match the config being scored")
    if freeze.get("detector_spec_sha256") != detector_sha:
        raise ModelGateError("frozen-run detector hash does not match the detector spec")
    return freeze


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M3a model quality gate (offline scoring).")
    ap.add_argument("--detector-spec", type=Path, default=DEFAULT_DETECTOR_SPEC_PATH)
    ap.add_argument("--lexicon", type=Path, default=DEFAULT_LEXICON_PATH)
    ap.add_argument("--expected-detector-sha256", default=None)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--freeze-manifest", type=Path, required=True)
    ap.add_argument("--dev-corpus", type=Path, required=True)
    ap.add_argument("--dev-runs", type=Path, required=True)
    ap.add_argument("--heldout-manifest", type=Path, required=True)
    ap.add_argument("--heldout-runs", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    args = ap.parse_args(argv)

    try:
        spec, detector_sha = load_detector_spec(args.detector_spec,
                                               expected_sha256=args.expected_detector_sha256)
        # The --lexicon argument is authoritative: load it, validate its structure, and require it to
        # be byte-identical (canonically) to the lexicon embedded in the detector spec. The detector
        # always reads spec["lexicon"]; this guarantees a run can never be labelled with a lexicon the
        # detector did not actually use.
        lexicon, _lexicon_sha = load_lexicon(args.lexicon)
        verify_lexicon_matches_spec(lexicon, spec)
        config = _load_json(args.config)
        freeze = _verify_freeze(args.freeze_manifest, config, detector_sha,
                                spec.get("detector_spec_version"))

        dev_corpus = _load_json(args.dev_corpus)
        dev_runs = _load_json(args.dev_runs)
        if not isinstance(dev_runs, list):
            raise ModelGateError("--dev-runs must be a JSON list")

        heldout_manifest = _load_json(args.heldout_manifest)
        heldout_corpus = _load_json(Path(heldout_manifest["path"]))
        if canonical_sha256(heldout_corpus) != heldout_manifest.get("heldout_fixture_sha256"):
            raise ModelGateError("held-out corpus hash does not match its manifest")
        heldout_runs = _load_json(args.heldout_runs)

        dev_store, _ = materialize_corpus(dev_corpus, ROOT / "data" / "docs_model_gate_dev")
        ho_store, _ = materialize_corpus(heldout_corpus, ROOT / "data" / "docs_model_gate_heldout")

        thresholds = Thresholds()
        selection, entries = run_selection(
            dev_corpus, dev_store, dev_runs, heldout_corpus, ho_store, heldout_runs,
            spec=spec, thresholds=thresholds)

        artifact = build_artifact(
            run_id=secrets.token_hex(4),
            recorded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            freeze={"frozen_at": freeze.get("frozen_at"),
                    "candidate_config_hash": freeze.get("candidate_config_hash"),
                    "prompt_version": freeze.get("prompt_version")},
            fixtures={"dev": {"version": dev_corpus.get("fixture_version"),
                              "sha256": canonical_sha256(dev_corpus)},
                      "heldout": {"version": heldout_corpus.get("fixture_version"),
                                  "sha256": canonical_sha256(heldout_corpus)}},
            prompt_version=config.get("prompt_version", ""), detector_spec=spec,
            detector_spec_sha256=detector_sha, thresholds=thresholds,
            candidate_entries=entries, selection=selection, known_limitations=KNOWN_LIMITATIONS,
        )
        path = write_artifact(artifact, args.out_dir, run_id=artifact["run_id"])
        print(f"selection: {selection.selected_candidate_id} ({selection.reason})")
        print(f"artifact:  {path}")
        return 0
    except ModelGateError as exc:
        print(f"model gate refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
