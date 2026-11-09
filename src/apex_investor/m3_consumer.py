"""M3 consumer: load and validate the M3a model-selection artifact (Phase 2 / M3).

This module CONSUMES the artifact written by the M3a producer (`model_gate.build_artifact` /
`write_artifact`) and resolves the operator-approved advisory model. It performs NO model calls and
touches NO network; handing the resolved model to `interpret.py`/`LiveAdapter` is a later, separately
authorized step.

Fail closed, no fallback (AC9): a missing, malformed, incompatible, or unapproved artifact is an
error — the consumer never picks a default model, another artifact, or a "latest" file. The operator
pins ONE artifact by its run_id in an operator-edited config (NO latest-wins, ruling 2).

Validation order (each step refuses with a distinct reason):
  1. pin config: exactly `{"pinned_run_id": "<non-empty string>"}` (AC7 source);
  2. artifact file exists and parses as JSON;
  3. `schema_version` is a supported version;
  4. the artifact's `run_id` EQUALS the pinned run_id (AC7);
  5. the on-disk detector spec (loaded through the producer's own validating loader, which enforces
     `SUPPORTED_DETECTOR_SPEC_VERSIONS`) hashes — with the PRODUCER'S canonical_sha256, reused here —
     to exactly the artifact's recorded `detector_spec.sha256`, and its version matches;
  6. the spec's embedded lexicon hashes to the artifact's `directive_lexicon.sha256`, its version
     matches, and the standalone operator lexicon file still matches the spec;
  7. the selection is non-null (a null selection REFUSES and surfaces the artifact's own
     `selection.reason` to the operator — ruling 3, no override);
  8. the selected candidate exists in `candidate_runs` and its recorded provider/model/prompt_version
     agree with the selection (AC8).

Hash compatibility with the producer is proven by tests that consume an artifact produced by the REAL
`build_artifact()` — never by hand-built look-alikes alone.

CONTRACT FOR THE FUTURE INTERPRETATION BRAIN (binding, ruling 1): the brain receives its adapter as
an EXPLICIT parameter. It must not select a model, read credentials, or construct its own adapter.
Holding a `SelectedModel` — or even a constructed adapter — is NOT permission to run a live model:
artifact approval and live-use authorization remain separate gates. `build_adapter(mode="offline")`
returns an `OfflineAdapter` whose every call raises until a separately authorized live gate is
opened; `mode="live"` only CONSTRUCTS a `LiveAdapter` (network-free) and is not, by itself,
authorization to call it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .evaluator_identity import (
    REQUIRED_ENV_FIELDS,
    compute_closure_sha256,
)
from .model_adapter import AdapterError, LiveAdapter
from .model_gate import (
    DEFAULT_ARTIFACT_DIR,
    DEFAULT_DETECTOR_SPEC_PATH,
    DEFAULT_LEXICON_PATH,
    ModelGateError,
    canonical_sha256,
    load_detector_spec,
    load_lexicon,
    verify_lexicon_matches_spec,
)

DEFAULT_PIN_CONFIG_PATH = Path("data/m3_consumer.json")
_SUPPORTED_ARTIFACT_SCHEMA_VERSIONS = frozenset({"1", "2"})
_PIN_CONFIG_KEYS = frozenset({"pinned_run_id"})

# A run_id is used inside a glob pattern, so it must never carry glob metacharacters or path
# separators. The producer stamps artifacts as model_selection_<stamp>_<run_id>.json with
# alphanumeric run_ids (secrets.token_hex), so this whitelist accepts every real artifact while
# making injection structurally impossible.
_SAFE_RUN_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
DEFAULT_OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
_LIVE_PROVIDERS = frozenset({"openrouter"})


class M3ConsumerError(Exception):
    """Raised when the pinned artifact is missing, malformed, incompatible, or unapproved."""


@dataclass(frozen=True)
class SelectedModel:
    """The operator-approved advisory model resolved from a validated M3a artifact."""

    candidate_id: str
    provider: str
    model: str
    prompt_version: str
    run_id: str


def load_pin_config(path: Path | str = DEFAULT_PIN_CONFIG_PATH) -> str:
    """Read the operator's pin config and return the pinned run_id (fail closed).

    The file must be a JSON object with EXACTLY one key, `pinned_run_id`, holding a non-empty
    string. Extra keys, other types, an empty/whitespace id, unreadable files, and invalid JSON are
    all refusals — a half-understood config must never degrade into "any artifact will do".
    """
    p = Path(path)
    if not p.exists():
        raise M3ConsumerError(f"pin config not found: {p} (create it from the .example.json)")
    try:
        config = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M3ConsumerError(f"pin config is unreadable: {exc}") from exc
    if not isinstance(config, dict):
        raise M3ConsumerError("pin config must be a JSON object")
    unknown = sorted(set(config) - _PIN_CONFIG_KEYS)
    if unknown:
        raise M3ConsumerError(f"pin config has unknown key(s): {unknown}")
    pinned = config.get("pinned_run_id")
    if not isinstance(pinned, str) or not pinned.strip():
        raise M3ConsumerError("pin config must contain a non-empty string pinned_run_id")
    return pinned.strip()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise M3ConsumerError(message)


def load_selection(artifact_path: Path | str, *, pinned_run_id: str,
                   detector_spec_path: Path | str = DEFAULT_DETECTOR_SPEC_PATH,
                   lexicon_path: Path | str = DEFAULT_LEXICON_PATH) -> SelectedModel:
    """Validate the pinned M3a artifact and return the selected model (fail closed, AC1–AC9).

    Every refusal is a distinct, operator-readable error. NO fallback of any kind: this function
    either returns the recorded selection or raises.
    """
    if not isinstance(pinned_run_id, str) or not pinned_run_id:
        raise M3ConsumerError("pinned_run_id must be a non-empty string")

    # (2) the artifact must exist and parse
    p = Path(artifact_path)
    if not p.exists():
        raise M3ConsumerError(f"pinned artifact not found: {p}")
    try:
        artifact = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M3ConsumerError(f"pinned artifact is unreadable: {exc}") from exc
    _require(isinstance(artifact, dict), "pinned artifact must be a JSON object")

    # (3) schema version
    schema_version = artifact.get("schema_version")
    _require(isinstance(schema_version, str) and bool(schema_version),
             "pinned artifact schema_version must be a non-empty string")
    _require(schema_version in _SUPPORTED_ARTIFACT_SCHEMA_VERSIONS,
             f"pinned artifact schema_version {schema_version!r} is unsupported")

    # (4) AC7: the pinned run_id must match the artifact's own run_id
    run_id = artifact.get("run_id")
    _require(isinstance(run_id, str) and bool(run_id), "pinned artifact is missing its run_id")
    _require(run_id == pinned_run_id,
             f"pinned artifact run_id {run_id!r} does not match the configured pin {pinned_run_id!r}")

    # (5) detector spec: load through the producer's validating loader, hash with the producer's
    # canonical_sha256, and compare against what the artifact recorded.
    recorded = artifact.get("detector_spec")
    _require(isinstance(recorded, dict), "pinned artifact is missing its detector_spec record")
    try:
        spec, spec_sha = load_detector_spec(detector_spec_path)
    except ModelGateError as exc:
        raise M3ConsumerError(f"detector spec failed validation: {exc}") from exc
    _require(recorded.get("sha256") == spec_sha,
             f"pinned artifact detector_spec sha256 {recorded.get('sha256')!r} does not match the "
             f"on-disk detector spec ({spec_sha})")
    _require(recorded.get("version") == str(spec.get("detector_spec_version")),
             f"pinned artifact detector_spec version {recorded.get('version')!r} does not match the "
             f"on-disk detector spec ({spec.get('detector_spec_version')!r})")

    # (6) lexicon: artifact's recorded sha/version must match the spec's embedded lexicon ...
    lex_record = artifact.get("directive_lexicon")
    _require(isinstance(lex_record, dict), "pinned artifact is missing its directive_lexicon record")
    embedded = spec.get("lexicon")
    _require(isinstance(embedded, dict), "detector spec is missing its lexicon")
    _require(lex_record.get("sha256") == canonical_sha256(embedded),
             f"pinned artifact directive_lexicon sha256 {lex_record.get('sha256')!r} does not match "
             f"the on-disk detector spec's lexicon ({canonical_sha256(embedded)})")
    _require(lex_record.get("version") == str(embedded.get("version")),
             f"pinned artifact directive_lexicon version {lex_record.get('version')!r} does not "
             f"match the on-disk detector spec's lexicon ({embedded.get('version')!r})")
    # ... and the standalone operator lexicon file must still agree with the spec (P3 parity).
    try:
        lexicon, _lex_sha = load_lexicon(lexicon_path)
        verify_lexicon_matches_spec(lexicon, spec)
    except ModelGateError as exc:
        raise M3ConsumerError(f"operator lexicon failed validation: {exc}") from exc

    # (7) ruling 3: a null/absent selection REFUSES, surfacing the artifact's own reason.
    selection = artifact.get("selection")
    _require(isinstance(selection, dict), "pinned artifact is missing its selection record")
    reason = selection.get("reason")
    selected = selection.get("selected_candidate_id")
    if not isinstance(selected, str) or not selected:
        raise M3ConsumerError(
            f"the pinned artifact records NO selected model ({reason!r}); the model gate refused "
            "selection, so the interpretation brain must not run")

    # (8) AC8: the selected candidate must exist in candidate_runs and agree on identity fields.
    candidates = artifact.get("candidate_runs")
    _require(isinstance(candidates, list), "pinned artifact candidate_runs must be a list")
    matches = [c for c in candidates
               if isinstance(c, dict) and c.get("candidate_id") == selected]
    _require(len(matches) == 1,
             f"pinned artifact candidate_runs has {len(matches)} entries for the selected candidate "
             f"{selected!r} (expected exactly 1)")
    entry = matches[0]
    _require(entry.get("is_baseline") is False,
             f"the selected candidate {selected!r} is recorded as a baseline; baselines are never "
             "selected models")
    # Identity fields must be present, non-empty, non-whitespace strings on BOTH sides. A missing key
    # must never sneak through as None == None and then raise a raw KeyError at construction time.
    for field_name in ("provider", "model", "prompt_version"):
        recorded_value = selection.get(field_name)
        _require(isinstance(recorded_value, str) and recorded_value.strip(),
                 f"pinned artifact selection is missing a valid {field_name}")
    _require(entry.get("provider") == selection.get("provider"),
             f"pinned artifact selection provider {selection.get('provider')!r} does not match the "
             f"selected candidate's recorded provider {entry.get('provider')!r}")
    _require(entry.get("model") == selection.get("model"),
             f"pinned artifact selection model {selection.get('model')!r} does not match the "
             f"selected candidate's recorded model {entry.get('model')!r}")
    verdicts = entry.get("verdicts")
    _require(isinstance(verdicts, dict), f"selected candidate {selected!r} has no verdicts record")
    dev = verdicts.get("dev")
    _require(isinstance(dev, dict), f"selected candidate {selected!r} has no dev verdict")
    _require(isinstance(dev.get("prompt_version"), str) and bool(dev.get("prompt_version")),
             f"selected candidate {selected!r} has no valid dev prompt_version")
    _require(dev.get("prompt_version") == selection.get("prompt_version"),
             f"pinned artifact selection prompt_version {selection.get('prompt_version')!r} does not "
             f"match the selected candidate's recorded dev prompt_version {dev.get('prompt_version')!r}")

    # (9) schema "2" requires the evaluator provenance record; schema "1" is legacy (no record).
    _validate_evaluator(artifact, schema_version)

    return SelectedModel(candidate_id=selected, provider=selection["provider"],
                         model=selection["model"], prompt_version=selection["prompt_version"],
                         run_id=run_id)


def _validate_evaluator(artifact: dict, schema_version: str) -> None:
    """Fail closed on a schema-"2" artifact's evaluator provenance; ignore it for schema "1".

    A "2" artifact must carry a well-formed evaluator record whose closure hash matches the recomputed
    hash of its own member hashes, with a complete environment identity. This binds the artifact to
    the evaluator closure and execution environment that produced it.
    """
    if schema_version != "2":
        return
    evaluator = artifact.get("evaluator")
    _require(isinstance(evaluator, dict),
             'schema "2" artifact is missing its evaluator provenance record')
    members = evaluator.get("members")
    _require(isinstance(members, dict) and bool(members),
             "evaluator record is missing its members mapping")
    for rel, digest in members.items():
        _require(isinstance(rel, str) and bool(rel),
                 "evaluator member path must be a non-empty string")
        _require(isinstance(digest, str) and bool(digest),
                 f"evaluator member {rel!r} must map to a sha256 string")
    closure = evaluator.get("closure_sha256")
    _require(isinstance(closure, str) and bool(closure),
             "evaluator record is missing its closure_sha256")
    _require(closure == compute_closure_sha256(members),
             "evaluator closure_sha256 does not match the recomputed hash of its members")
    env = evaluator.get("environment")
    _require(isinstance(env, dict), "evaluator record is missing its environment identity")
    for field_name in REQUIRED_ENV_FIELDS:
        value = env.get(field_name)
        if field_name == "libc":
            ok = isinstance(value, (list, tuple)) and len(value) == 2 and all(str(v) for v in value)
        else:
            ok = isinstance(value, str) and bool(value)
        _require(ok, f"evaluator environment identity is missing or malformed: {field_name!r}")
    _require(isinstance(evaluator.get("data_artifacts"), dict),
             "evaluator record is missing its data_artifacts identities")


def _validate_pinned_run_id(pinned_run_id: str) -> str:
    """Reject a pinned run_id that is absent, empty, whitespace, or unsafe inside a glob."""
    if not isinstance(pinned_run_id, str) or not pinned_run_id.strip():
        raise M3ConsumerError("pinned_run_id must be a non-empty string")
    run_id = pinned_run_id.strip()
    if not _SAFE_RUN_ID_RE.fullmatch(run_id):
        raise M3ConsumerError(
            f"pinned_run_id {run_id!r} contains characters that are not allowed "
            "(only letters, digits, '.', '_', '-' are permitted)")
    return run_id


def resolve_artifact_path(pinned_run_id: str,
                          artifact_dir: Path | str = DEFAULT_ARTIFACT_DIR) -> Path:
    """Locate the ONE artifact matching the pinned run_id (AC7/AC9; exactly-one-match rule).

    The pin is a run_id, never a filename pattern: it is validated against a whitelist (no glob
    metacharacters, no path separators), matched ONLY against model_selection_*_<run_id>.json names,
    and must resolve to EXACTLY ONE usable file. Zero matches refuse; multiple matches refuse as
    AMBIGUOUS — the consumer never picks the newest file and never falls back. Resolving the path
    proves nothing about validity: the caller must still run the full `load_selection` validation.
    """
    run_id = _validate_pinned_run_id(pinned_run_id)
    d = Path(artifact_dir)
    if not d.is_dir():
        raise M3ConsumerError(f"artifact directory not found: {d}")
    matches = sorted(d.glob(f"model_selection_*_{run_id}.json"))
    if not matches:
        raise M3ConsumerError(f"no artifact for the pinned run_id {run_id!r} in {d}")
    if len(matches) > 1:
        names = "\n  ".join(str(m.name) for m in matches)
        raise M3ConsumerError(
            f"{len(matches)} artifacts match the pinned run_id {run_id!r}; refusing to choose "
            f"(archive or remove duplicates):\n  {names}")
    return matches[0]


class OfflineAdapter:
    """An inert adapter: EVERY call raises until live use is separately authorized.

    Deliberately implements the same interface as `LiveAdapter`/`RecordedAdapter` so downstream code
    cannot distinguish it by shape — the boundary is behavioral, not structural. Construction never
    touches the network or credentials; the refusal happens at call time with an unambiguous message.
    """

    def __init__(self, *, model: str, endpoint: str, api_key_env: str = "OPENROUTER_API_KEY",
                 timeout: int = 60, temperature: float = 0.0) -> None:
        self.model = model
        self.endpoint = endpoint
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.temperature = temperature

    def complete(self, *, key: str = "", system: str = "", user: str = "") -> str:
        raise AdapterError("live model execution is not authorized (offline mode); the pinned "
                           "artifact approves the model, but a separate operator gate opens live use")


def build_adapter(selection: SelectedModel, *, mode: str = "offline",
                  endpoint: str = DEFAULT_OPENROUTER_ENDPOINT,
                  api_key_env: str = "OPENROUTER_API_KEY",
                  timeout: int = 60, temperature: float = 0.0) -> OfflineAdapter | LiveAdapter:
    """Build the adapter for the validated selection. Construction NEVER touches the network.

    mode="offline" (the default) -> `OfflineAdapter`, whose every call raises: the artifact-approved
    selection is NOT permission to run a live model. mode="live" -> `LiveAdapter`, constructed
    network-free; calling it remains a separate, later authorization. Any other mode is refused (no
    silent default), and live construction is permitted ONLY for providers on the live allowlist
    (`openrouter`) — another provider is refused rather than silently rewritten to fit.
    """
    if not isinstance(mode, str) or mode not in ("offline", "live"):
        raise M3ConsumerError(f"adapter mode {mode!r} is not supported (use 'offline' or 'live')")
    if not isinstance(selection, SelectedModel):
        raise M3ConsumerError("build_adapter requires a validated SelectedModel")
    kwargs = dict(model=selection.model, endpoint=endpoint, api_key_env=api_key_env,
                  timeout=timeout, temperature=temperature)
    if mode == "offline":
        return OfflineAdapter(**kwargs)
    if selection.provider not in _LIVE_PROVIDERS:
        raise M3ConsumerError(
            f"provider {selection.provider!r} is not approved for live adapter construction "
            f"(allowed: {sorted(_LIVE_PROVIDERS)}); refusing rather than substituting an endpoint")
    return LiveAdapter(**kwargs)
