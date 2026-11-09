"""M3 consumer tests: load and validate the pinned M3a selection artifact (all offline).

The compatibility tests use an artifact produced by the REAL `build_artifact` + `write_artifact`
(the producer's own canonicalization), never hand-built look-alikes alone, so consumer hash
validation is proven against the producer's exact rules.
"""
from __future__ import annotations

import json
import secrets
import tempfile
from pathlib import Path

import pytest

from apex_investor.m3_consumer import (
    M3ConsumerError,
    load_pin_config,
    load_selection,
)

# ---- fixtures: a REAL producer artifact ----------------------------------------------------------

ART = Path(__file__).parent / "fixtures" / "model_gate"


def _real_artifact(tmp_path: Path, *, evaluator=None):
    """Produce a REAL artifact via the producer's build_artifact + write_artifact.

    Returns (artifact_path, run_id, expected SelectedModel fields). No monkeypatching, no mocks —
    this exercises run_selection + build_artifact exactly as the CLI does.
    """
    from apex_investor.docstore import DocumentStore  # noqa: F401  (import parity with scorer tests)
    from apex_investor.model_gate import (
        Thresholds,
        build_artifact,
        load_detector_spec,
        materialize_corpus,
        run_selection,
        write_artifact,
    )

    dev = json.loads((ART / "corpus.dev.v1.json").read_text())
    heldout = json.loads((ART / "heldout_corpus.v1.json").read_text())
    dev_runs = json.loads((ART / "runs" / "dev.json").read_text())
    ho_runs = json.loads((ART / "runs" / "heldout.json").read_text())
    spec, spec_sha = load_detector_spec()
    with tempfile.TemporaryDirectory() as d:
        dev_store, _ = materialize_corpus(dev, Path(d) / "dev")
        ho_store, _ = materialize_corpus(heldout, Path(d) / "ho")
        selection, entries = run_selection(dev, dev_store, dev_runs, heldout, ho_store, ho_runs,
                                           spec=spec, thresholds=Thresholds())
        assert selection.selected_candidate_id == "cand-a"
        run_id = secrets.token_hex(4)
        art = build_artifact(
            run_id=run_id, recorded_at="2024-06-02T00:00:00Z",
            freeze={"frozen_at": "2024-06-02T00:00:00Z", "candidate_config_hash": "h",
                    "prompt_version": "m3-v1"},
            fixtures={"dev": {"version": "1", "sha256": "x"},
                      "heldout": {"version": "1", "sha256": "y"}},
            prompt_version="m3-v1", detector_spec=spec, detector_spec_sha256=spec_sha,
            thresholds=Thresholds(), candidate_entries=entries, selection=selection,
            known_limitations={"version": "1", "items": []}, evaluator=evaluator)
        path = write_artifact(art, tmp_path, run_id=run_id)
        return path, run_id, selection


# ---- happy path ---------------------------------------------------------------------------------

def test_load_selection_accepts_real_producer_artifact(tmp_path):
    # THE compatibility proof: an artifact produced by the real build_artifact/write_artifact passes
    # the consumer's hash + identity validation using the producer's own canonical_sha256.
    path, run_id, selection = _real_artifact(tmp_path)
    sel = load_selection(path, pinned_run_id=run_id)
    assert sel.candidate_id == "cand-a"
    assert sel.provider == selection.provider
    assert sel.model == selection.model
    assert sel.prompt_version == selection.prompt_version
    assert sel.run_id == run_id


# ---- pin config (AC7 source) --------------------------------------------------------------------

def _pin(tmp_path, **kwargs):
    cfg = {"pinned_run_id": "abc"}
    cfg.update(kwargs)
    p = tmp_path / "m3_consumer.json"
    p.write_text(json.dumps(cfg))
    return p


def test_pin_config_happy(tmp_path):
    assert load_pin_config(_pin(tmp_path)) == "abc"


def test_pin_config_missing_file(tmp_path):
    with pytest.raises(M3ConsumerError, match="pin config not found"):
        load_pin_config(tmp_path / "nope.json")


def test_pin_config_unreadable_json(tmp_path):
    p = tmp_path / "m3_consumer.json"
    p.write_text("{not json")
    with pytest.raises(M3ConsumerError, match="unreadable"):
        load_pin_config(p)


def test_pin_config_non_object(tmp_path):
    p = tmp_path / "m3_consumer.json"
    p.write_text("[1,2]")
    with pytest.raises(M3ConsumerError, match="must be a JSON object"):
        load_pin_config(p)


def test_pin_config_unknown_key(tmp_path):
    with pytest.raises(M3ConsumerError, match="unknown key"):
        load_pin_config(_pin(tmp_path, extra=True))


def test_pin_config_empty_or_whitespace_id(tmp_path):
    for bad in ("", "   ", None, 5):
        p = _pin(tmp_path, pinned_run_id=bad)
        with pytest.raises(M3ConsumerError, match="non-empty string"):
            load_pin_config(p)


def test_pin_config_missing_key(tmp_path):
    p = tmp_path / "m3_consumer.json"
    p.write_text("{}")
    with pytest.raises(M3ConsumerError, match="non-empty string"):
        load_pin_config(p)


# ---- AC1: fail-closed load ----------------------------------------------------------------------

def test_artifact_missing_refused(tmp_path):
    with pytest.raises(M3ConsumerError, match="not found"):
        load_selection(tmp_path / "nope.json", pinned_run_id="abc")


def test_artifact_unreadable_json_refused(tmp_path):
    p = tmp_path / "art.json"
    p.write_text("{not json")
    with pytest.raises(M3ConsumerError, match="unreadable"):
        load_selection(p, pinned_run_id="abc")


def test_artifact_non_object_refused(tmp_path):
    p = tmp_path / "art.json"
    p.write_text("[]")
    with pytest.raises(M3ConsumerError, match="JSON object"):
        load_selection(p, pinned_run_id="abc")


def _base_artifact_dict():
    # A hand-built baseline for NEGATIVE path testing; every mutation below is a refusal scenario.
    return {
        "schema_version": "1",
        "run_id": "abc",
        "detector_spec": {"version": "1",
                          "sha256": "4fe693c80be3858c22dbb741a0641e05f50fd09ee490864ea92d8b03c76924b2"},
        "directive_lexicon": {"version": "1",
                              "sha256": "0d084ee100000000000000000000000000000000000000000000000009354"},
        "selection": {"selected_candidate_id": "cand-a", "provider": "openrouter",
                      "model": "mistral-test-a", "prompt_version": "m3-v1", "reason": "ok"},
        "candidate_runs": [
            {"candidate_id": "cand-a", "provider": "openrouter", "model": "mistral-test-a",
             "is_baseline": False,
             "verdicts": {"dev": {"prompt_version": "m3-v1"}, "heldout": {"prompt_version": "m3-v1"}}},
        ],
    }


def _write(tmp_path, art):
    p = tmp_path / "art.json"
    p.write_text(json.dumps(art))
    return p


# ---- schema + AC7 run_id ------------------------------------------------------------------------

def test_schema_version_unsupported_refused(tmp_path):
    art = _base_artifact_dict()
    art["schema_version"] = "999"
    with pytest.raises(M3ConsumerError, match="unsupported"):
        load_selection(_write(tmp_path, art), pinned_run_id="abc")


def test_schema_version_missing_refused(tmp_path):
    art = _base_artifact_dict()
    del art["schema_version"]
    with pytest.raises(M3ConsumerError, match="schema_version"):
        load_selection(_write(tmp_path, art), pinned_run_id="abc")


def test_run_id_mismatch_refused(tmp_path):
    # AC7: the pin must match the artifact's own run_id.
    art = _base_artifact_dict()
    art["run_id"] = "other"
    with pytest.raises(M3ConsumerError, match="does not match the configured pin"):
        load_selection(_write(tmp_path, art), pinned_run_id="abc")


def test_run_id_missing_refused(tmp_path):
    art = _base_artifact_dict()
    del art["run_id"]
    with pytest.raises(M3ConsumerError, match="run_id"):
        load_selection(_write(tmp_path, art), pinned_run_id="abc")


# ---- hash + version compatibility (AC1) ---------------------------------------------------------

def _real_dict(tmp_path):
    path, run_id, _ = _real_artifact(tmp_path)
    return json.loads(path.read_text()), run_id


def test_detector_spec_hash_mismatch_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    art["detector_spec"]["sha256"] = "0" * 64
    with pytest.raises(M3ConsumerError, match="detector_spec sha256"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_detector_spec_version_mismatch_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    art["detector_spec"]["version"] = "999"
    with pytest.raises(M3ConsumerError, match="detector_spec version"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_lexicon_hash_mismatch_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    art["directive_lexicon"]["sha256"] = "1" * 64
    with pytest.raises(M3ConsumerError, match="directive_lexicon sha256"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_lexicon_version_mismatch_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    art["directive_lexicon"]["version"] = "999"
    with pytest.raises(M3ConsumerError, match="directive_lexicon version"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_missing_detector_spec_record_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    del art["detector_spec"]
    with pytest.raises(M3ConsumerError, match="detector_spec"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_missing_lexicon_record_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    del art["directive_lexicon"]
    with pytest.raises(M3ConsumerError, match="directive_lexicon"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


# ---- ruling 3: null selection -------------------------------------------------------------------

def test_null_selection_refused_surfaces_reason(tmp_path):
    art, run_id = _real_dict(tmp_path)
    art["selection"]["selected_candidate_id"] = None
    art["selection"]["reason"] = "tie between eligible candidates; operator decision required"
    with pytest.raises(M3ConsumerError,
                       match=r"NO selected model .*tie between eligible candidates"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_null_selection_surfaces_no_eligible_reason(tmp_path):
    art, run_id = _real_dict(tmp_path)
    art["selection"]["selected_candidate_id"] = None
    art["selection"]["reason"] = "no candidate met every threshold on dev and held-out"
    with pytest.raises(M3ConsumerError, match="no candidate met every threshold"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_missing_selection_record_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    del art["selection"]
    with pytest.raises(M3ConsumerError, match="selection"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


# ---- AC8: selection integrity -------------------------------------------------------------------

def test_selected_candidate_missing_from_candidate_runs_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    art["candidate_runs"] = [c for c in art["candidate_runs"] if c["candidate_id"] != "cand-a"]
    with pytest.raises(M3ConsumerError, match="candidate_runs has 0 entries"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_selected_candidate_duplicated_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    art["candidate_runs"] = [art["candidate_runs"][0], art["candidate_runs"][0]]
    with pytest.raises(M3ConsumerError, match="candidate_runs has 2 entries"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_provider_mismatch_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    art["selection"]["provider"] = "other-provider"
    with pytest.raises(M3ConsumerError, match="provider"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_model_mismatch_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    art["selection"]["model"] = "other-model"
    with pytest.raises(M3ConsumerError, match="model"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_prompt_version_mismatch_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    art["selection"]["prompt_version"] = "m3-v999"
    with pytest.raises(M3ConsumerError, match="prompt_version"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_baseline_selected_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    art["selection"]["selected_candidate_id"] = "abstain-always"
    base = next(c for c in art["candidate_runs"] if c["is_baseline"])
    art["selection"]["provider"], art["selection"]["model"] = base["provider"], base["model"]
    art["selection"]["prompt_version"] = base["verdicts"]["dev"]["prompt_version"]
    with pytest.raises(M3ConsumerError, match="baseline"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_candidate_runs_not_a_list_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    art["candidate_runs"] = {}
    with pytest.raises(M3ConsumerError, match="candidate_runs"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_missing_dev_verdict_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    art["candidate_runs"][0]["verdicts"] = {}
    with pytest.raises(M3ConsumerError, match="no dev verdict"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_missing_dev_prompt_version_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    del art["candidate_runs"][0]["verdicts"]["dev"]["prompt_version"]
    with pytest.raises(M3ConsumerError, match="no valid dev prompt_version"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


# ---- no raw KeyError/TypeError ever leaks from malformed nested records ---------------------------

def test_selection_missing_identity_fields_refused_not_keyerror(tmp_path):
    # A selection record missing provider/model/prompt_version entirely must be refused with
    # M3ConsumerError — never None == None passing and then a raw KeyError at construction.
    for field in ("provider", "model", "prompt_version"):
        art, run_id = _real_dict(tmp_path)
        del art["selection"][field]
        with pytest.raises(M3ConsumerError, match=f"missing a valid {field}"):
            load_selection(_write(tmp_path, art), pinned_run_id=run_id)


@pytest.mark.parametrize("field", ["provider", "model", "prompt_version"])
def test_selection_identity_fields_non_string_refused(field, tmp_path):
    for bad in (None, "", "   ", 5, ["x"]):
        art, run_id = _real_dict(tmp_path)
        art["selection"][field] = bad
        with pytest.raises(M3ConsumerError, match=f"missing a valid {field}"):
            load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_candidate_runs_entry_not_dict_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    art["candidate_runs"] = ["not-a-dict"]
    # No match for the selected id -> the cardinality check refuses (never an attribute error).
    with pytest.raises(M3ConsumerError, match="candidate_runs has 0 entries"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_candidate_runs_entry_with_wrong_shape_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    art["candidate_runs"][0]["verdicts"] = "flat-string"
    with pytest.raises(M3ConsumerError, match="no verdicts record"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_selected_candidate_id_non_string_refused(tmp_path):
    for bad in (None, "", 5, ["cand-a"]):
        art, run_id = _real_dict(tmp_path)
        art["selection"]["selected_candidate_id"] = bad
        with pytest.raises(M3ConsumerError, match="NO selected model"):
            load_selection(_write(tmp_path, art), pinned_run_id=run_id)


def test_verdicts_dev_not_dict_refused(tmp_path):
    art, run_id = _real_dict(tmp_path)
    art["candidate_runs"][0]["verdicts"] = {"dev": "oops"}
    with pytest.raises(M3ConsumerError, match="no dev verdict"):
        load_selection(_write(tmp_path, art), pinned_run_id=run_id)


# ---- AC9: no fallback is structural (single pinned path, no search) ------------------------------

def test_consumer_does_not_search_directory(tmp_path):
    # The consumer takes exactly ONE artifact path. Even with a valid artifact elsewhere in the same
    # directory, a wrong pin must not discover or substitute it (structural no-fallback).
    _real_artifact(tmp_path)
    with pytest.raises(M3ConsumerError, match="not found"):
        load_selection(tmp_path / "definitely-not-here.json", pinned_run_id="whatever")


# ---- Step 2: artifact-path resolution by pinned run_id -------------------------------------------

from apex_investor.m3_consumer import (  # noqa: E402
    DEFAULT_OPENROUTER_ENDPOINT,
    OfflineAdapter,
    build_adapter,
    resolve_artifact_path,
)
from apex_investor.model_adapter import AdapterError, LiveAdapter  # noqa: E402


def test_resolve_finds_the_single_pinned_artifact(tmp_path):
    path, run_id, _ = _real_artifact(tmp_path)
    assert resolve_artifact_path(run_id, artifact_dir=tmp_path) == path


def test_resolve_refuses_zero_matches(tmp_path):
    _real_artifact(tmp_path)
    with pytest.raises(M3ConsumerError, match="no artifact for the pinned run_id"):
        resolve_artifact_path("nonexistent", artifact_dir=tmp_path)


def test_resolve_refuses_multiple_matches_as_ambiguous(tmp_path):
    # Two artifacts sharing one run_id must refuse — the consumer never picks the newest.
    path1, run_id, _ = _real_artifact(tmp_path)
    (tmp_path / f"model_selection_20990101T000000Z_{run_id}.json").write_text(path1.read_text())
    with pytest.raises(M3ConsumerError, match=r"2 artifacts match .* refusing to choose"):
        resolve_artifact_path(run_id, artifact_dir=tmp_path)


@pytest.mark.parametrize("bad_pin", ["", "   ", None, 5, "*", "a[b", "a?b", "../etc/passwd",
                                     "with/slash", r"with\backslash", "a]b", "x[!*]y"])
def test_resolve_refuses_unsafe_or_empty_pin(bad_pin, tmp_path):
    with pytest.raises(M3ConsumerError, match="pinned_run_id"):
        resolve_artifact_path(bad_pin, artifact_dir=tmp_path)


def test_resolve_never_matches_a_different_run_id(tmp_path):
    # A DIFFERENT artifact in the same directory must not be discovered for this pin (no fallback).
    _real_artifact(tmp_path)
    with pytest.raises(M3ConsumerError, match="no artifact"):
        resolve_artifact_path("c0000000", artifact_dir=tmp_path)


def test_resolve_missing_directory_refused(tmp_path):
    with pytest.raises(M3ConsumerError, match="artifact directory not found"):
        resolve_artifact_path("abc", artifact_dir=tmp_path / "nope")


def test_resolved_path_still_requires_full_validation(tmp_path):
    # A matching FILENAME is not proof of a valid artifact: a same-named junk file must be refused
    # by load_selection even though resolve_artifact_path found it.
    (tmp_path / "model_selection_20240602T000000Z_abc.json").write_text("{not json")
    p = resolve_artifact_path("abc", artifact_dir=tmp_path)
    assert p.name == "model_selection_20240602T000000Z_abc.json"
    with pytest.raises(M3ConsumerError, match="unreadable"):
        load_selection(p, pinned_run_id="abc")


# ---- Step 2: OfflineAdapter and build_adapter ----------------------------------------------------

def _sel(**overrides):
    from apex_investor.m3_consumer import SelectedModel
    fields = {"candidate_id": "cand-a", "provider": "openrouter", "model": "mistral-x",
              "prompt_version": "m3-v1", "run_id": "abc"}
    fields.update(overrides)
    return SelectedModel(**fields)


def test_build_adapter_default_is_offline():
    a = build_adapter(_sel())
    assert isinstance(a, OfflineAdapter)


def test_build_adapter_rejects_unknown_mode():
    for bad in ("", "LIVE", "offline ", None, 1):
        with pytest.raises(M3ConsumerError, match="mode"):
            build_adapter(_sel(), mode=bad)


def test_build_adapter_requires_selected_model():
    with pytest.raises(M3ConsumerError, match="validated SelectedModel"):
        build_adapter({"model": "m"})


def test_offline_adapter_matches_live_interface_shape():
    offline = build_adapter(_sel())
    live = build_adapter(_sel(), mode="live")
    # Same constructor surface, so downstream code cannot distinguish by shape.
    for attr in ("model", "endpoint", "api_key_env", "timeout", "temperature"):
        assert getattr(offline, attr) == getattr(live, attr)


def test_offline_adapter_raises_on_every_call():
    a = build_adapter(_sel())
    for kwargs in ({"key": "s1"}, {"system": "s", "user": "u"}, {}):
        with pytest.raises(AdapterError, match="not authorized"):
            a.complete(**kwargs)


def test_live_construction_is_network_free_and_carries_identity():
    # Constructor performs NO I/O: assert stored fields only. complete() is NEVER called in tests.
    a = build_adapter(_sel(), mode="live", endpoint="https://example.invalid/v1",
                      api_key_env="MY_KEY_ENV", timeout=7, temperature=0.3)
    assert isinstance(a, LiveAdapter)
    assert a.model == "mistral-x"
    assert a.endpoint == "https://example.invalid/v1"
    assert a.api_key_env == "MY_KEY_ENV"
    assert a.timeout == 7
    assert a.temperature == 0.3


def test_live_construction_default_endpoint_and_env():
    a = build_adapter(_sel(), mode="live")
    assert a.endpoint == DEFAULT_OPENROUTER_ENDPOINT
    assert a.api_key_env == "OPENROUTER_API_KEY"


def test_live_construction_refuses_non_openrouter_provider():
    # Reject rather than substitute: an unapproved provider never gets an endpoint rewrite.
    with pytest.raises(M3ConsumerError, match="not approved for live"):
        build_adapter(_sel(provider="anthropic"), mode="live")
    with pytest.raises(M3ConsumerError, match="not approved for live"):
        build_adapter(_sel(provider=""), mode="live")


def test_full_chain_pin_to_offline_adapter(tmp_path):
    # pin config -> resolve -> validate -> build (offline). The end-to-end path the operator uses.
    from apex_investor.m3_consumer import load_pin_config
    path, run_id, _ = _real_artifact(tmp_path)
    (tmp_path / "m3_consumer.json").write_text(json.dumps({"pinned_run_id": run_id}))
    sel = load_selection(path, pinned_run_id=load_pin_config(tmp_path / "m3_consumer.json"))
    adapter = build_adapter(sel)
    assert isinstance(adapter, OfflineAdapter)
    with pytest.raises(AdapterError, match="not authorized"):
        adapter.complete(key="s1")


def test_m3_consumer_module_contract_guards():
    # Static guard: the consumer imports nothing from report/ledger/risk (isolation invariant), and
    # the binding brain contract is documented in the module itself.
    import apex_investor.m3_consumer as mod
    source = Path(mod.__file__).read_text()
    for banned in ("from .report", "from .ledger", "from .risk", "import report", "import ledger",
                   "import risk"):
        assert banned not in source
    assert "EXPLICIT parameter" in source
    assert "must not select a model" in source
    assert "separate gates" in source




# ---- B1 evaluator provenance (schema "2") -------------------------------------------------------

def _evaluator_record():
    from apex_investor.evaluator_identity import compute_closure_sha256, environment_identity
    members = {"src/apex_investor/model_gate.py": "a" * 64, "src/apex_investor/citation.py": "b" * 64}
    return {"closure_sha256": compute_closure_sha256(members), "members": members,
            "environment": environment_identity(), "data_artifacts": {"prompt_sha256": None}}


def test_schema_2_artifact_with_evaluator_is_accepted(tmp_path):
    path, run_id, _ = _real_artifact(tmp_path, evaluator=_evaluator_record())
    art = json.loads(path.read_text())
    assert art["schema_version"] == "2"
    sel = load_selection(path, pinned_run_id=run_id)
    assert sel.candidate_id == "cand-a"


def test_schema_1_artifact_without_evaluator_still_accepted(tmp_path):
    # Backward compatibility: a schema "1" artifact carries no evaluator record and still loads.
    path, run_id, _ = _real_artifact(tmp_path)
    art = json.loads(path.read_text())
    assert art["schema_version"] == "1"
    assert art["evaluator"] is None
    assert load_selection(path, pinned_run_id=run_id).candidate_id == "cand-a"


def test_schema_2_artifact_missing_evaluator_is_refused(tmp_path):
    path, run_id, _ = _real_artifact(tmp_path, evaluator=_evaluator_record())
    art = json.loads(path.read_text())
    art["evaluator"] = None
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(art))
    with pytest.raises(M3ConsumerError, match="evaluator provenance"):
        load_selection(bad, pinned_run_id=run_id)


def test_schema_2_artifact_with_bad_closure_hash_is_refused(tmp_path):
    path, run_id, _ = _real_artifact(tmp_path, evaluator=_evaluator_record())
    art = json.loads(path.read_text())
    art["evaluator"]["closure_sha256"] = "0" * 64  # does not match the members
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(art))
    with pytest.raises(M3ConsumerError, match="closure_sha256"):
        load_selection(bad, pinned_run_id=run_id)


def test_schema_2_artifact_with_incomplete_environment_is_refused(tmp_path):
    path, run_id, _ = _real_artifact(tmp_path, evaluator=_evaluator_record())
    art = json.loads(path.read_text())
    art["evaluator"]["environment"].pop("architecture")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(art))
    with pytest.raises(M3ConsumerError, match="environment identity"):
        load_selection(bad, pinned_run_id=run_id)
