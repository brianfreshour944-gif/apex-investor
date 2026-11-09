"""B1 evaluator-identity and freeze-authorization tests (offline; no model, no network)."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from apex_investor import evaluator_identity as ei
from apex_investor.evaluator_identity import EvaluatorIdentityError

ROOT = Path(__file__).resolve().parent.parent


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def frozen_root(tmp_path: Path) -> Path:
    """A temp root containing byte-identical copies of the four scoring-closure members."""
    for rel in ei.SCORING_CLOSURE_MEMBERS:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, dst)
    return tmp_path


def _write_pair(root: Path, *, members=ei.SCORING_CLOSURE_MEMBERS):
    """Build an inventory + authority over `root`; return (inventory_path, authority_path)."""
    inv = ei.build_inventory(root, members)
    inv_path = root / "data/model_gate/evaluator_closure.v1.json"
    inv_path.parent.mkdir(parents=True, exist_ok=True)
    inv_path.write_text(json.dumps(inv, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    auth = ei.build_authority(inventory_relpath="data/model_gate/evaluator_closure.v1.json",
                              inventory_bytes=inv_path.read_bytes(), expected_members=members)
    auth_path = root / "data/model_gate/freeze_authority.v1.json"
    auth_path.write_text(json.dumps(auth, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return inv_path, auth_path


# ---- closure definition ------------------------------------------------------------------------

def test_closure_members_are_the_four_scoring_modules():
    assert set(ei.SCORING_CLOSURE_MEMBERS) == {
        "src/apex_investor/model_gate.py",
        "src/apex_investor/citation.py",
        "src/apex_investor/docstore.py",
        "src/apex_investor/sanitize.py",
    }


def test_closure_hash_is_deterministic_and_order_independent():
    members = {rel: _sha256(ROOT / rel) for rel in ei.SCORING_CLOSURE_MEMBERS}
    shuffled = dict(reversed(list(members.items())))
    assert ei.compute_closure_sha256(members) == ei.compute_closure_sha256(shuffled)


def test_committed_inventory_matches_the_working_tree():
    inv = json.loads((ROOT / "data/model_gate/evaluator_closure.v1.json").read_text())
    assert set(inv["members"]) == set(ei.SCORING_CLOSURE_MEMBERS)
    for rel, want in inv["members"].items():
        assert _sha256(ROOT / rel) == want, rel
    assert ei.compute_closure_sha256(inv["members"]) == inv["closure_sha256"]


def test_committed_authority_pins_the_committed_inventory():
    auth = json.loads((ROOT / "data/model_gate/freeze_authority.v1.json").read_text())
    inv_path = ROOT / auth["inventory_path"]
    assert _sha256(inv_path) == auth["expected_inventory_sha256"]
    inv = json.loads(inv_path.read_text())
    assert inv["closure_sha256"] == auth["expected_closure_sha256"]


# ---- environment identity ----------------------------------------------------------------------

def test_environment_has_required_discrete_fields():
    env = ei.environment_identity()
    for field in ei.REQUIRED_ENV_FIELDS:
        assert field in env
    assert env["python_implementation"] == "CPython"
    assert isinstance(env["libc"], list) and len(env["libc"]) == 2
    assert env["dependencies"] == "stdlib-only"
    ei.check_environment(env)  # does not raise


def test_check_environment_refuses_missing_field():
    env = ei.environment_identity()
    env.pop("architecture")
    with pytest.raises(EvaluatorIdentityError):
        ei.check_environment(env)


def test_check_environment_refuses_mismatch():
    env = ei.environment_identity()
    with pytest.raises(EvaluatorIdentityError):
        ei.check_environment(env, expected={"python_version": "0.0.0"})


# ---- verification happy path -------------------------------------------------------------------

def test_verify_happy_path(frozen_root: Path):
    _, auth_path = _write_pair(frozen_root)
    identity = ei.verify_freeze_authorization(
        root=frozen_root, authority_sha256=_sha256(auth_path), authority_path=auth_path)
    assert identity["closure_sha256"] == ei.compute_closure_sha256(identity["members"])
    assert set(identity["members"]) == set(ei.SCORING_CLOSURE_MEMBERS)
    assert identity["environment"]["python_implementation"] == "CPython"


# ---- fail-closed conditions --------------------------------------------------------------------

def test_missing_authority_sha_is_refused(frozen_root: Path):
    _, auth_path = _write_pair(frozen_root)
    with pytest.raises(EvaluatorIdentityError):
        ei.verify_freeze_authorization(root=frozen_root, authority_sha256="", authority_path=auth_path)


def test_authority_sha_mismatch_is_refused(frozen_root: Path):
    _, auth_path = _write_pair(frozen_root)
    with pytest.raises(EvaluatorIdentityError):
        ei.verify_freeze_authorization(root=frozen_root, authority_sha256="0" * 64,
                                       authority_path=auth_path)


def test_missing_authority_file_is_refused(frozen_root: Path):
    with pytest.raises(EvaluatorIdentityError):
        ei.verify_freeze_authorization(root=frozen_root, authority_sha256="a" * 64,
                                       authority_path=frozen_root / "nope.json")


def test_tampered_member_is_refused(frozen_root: Path):
    _, auth_path = _write_pair(frozen_root)
    target = frozen_root / "src/apex_investor/citation.py"
    target.write_text(target.read_text() + "\n# tampered\n")
    with pytest.raises(EvaluatorIdentityError):
        ei.verify_freeze_authorization(root=frozen_root, authority_sha256=_sha256(auth_path),
                                       authority_path=auth_path)


def test_membership_added_member_is_refused(frozen_root: Path):
    inv_path, auth_path = _write_pair(frozen_root)
    inv = json.loads(inv_path.read_text())
    inv["members"]["src/apex_investor/render.py"] = _sha256(ROOT / "src/apex_investor/render.py")
    inv["closure_sha256"] = ei.compute_closure_sha256(inv["members"])
    inv_path.write_text(json.dumps(inv, sort_keys=True))
    # authority pins the OLD inventory bytes -> refuse (external anchor)
    with pytest.raises(EvaluatorIdentityError):
        ei.verify_freeze_authorization(root=frozen_root, authority_sha256=_sha256(auth_path),
                                       authority_path=auth_path)


def test_inventory_internally_consistent_but_repinned_is_refused(frozen_root: Path):
    """Operator-required test: edit the inventory AND recompute its hashes so it is self-consistent;
    the independent authority pin (inventory_sha256) must still cause refusal."""
    inv_path, auth_path = _write_pair(frozen_root)
    inv = json.loads(inv_path.read_text())
    inv["members"]["src/apex_investor/citation.py"] = "b" * 64          # attacker edit
    inv["closure_sha256"] = ei.compute_closure_sha256(inv["members"])    # recomputed -> consistent
    inv_path.write_text(json.dumps(inv, sort_keys=True) + "\n")
    with pytest.raises(EvaluatorIdentityError, match="inventory hash mismatch"):
        ei.verify_freeze_authorization(root=frozen_root, authority_sha256=_sha256(auth_path),
                                       authority_path=auth_path)


def test_authority_members_must_match_defined_closure(frozen_root: Path):
    inv_path, auth_path = _write_pair(frozen_root)
    auth = json.loads(auth_path.read_text())
    auth["expected_members"] = ["src/apex_investor/model_gate.py"]  # incomplete
    auth_path.write_text(json.dumps(auth, sort_keys=True))
    with pytest.raises(EvaluatorIdentityError, match="expected_members"):
        ei.verify_freeze_authorization(root=frozen_root, authority_sha256=_sha256(auth_path),
                                       authority_path=auth_path)


def test_unsupported_format_version_is_refused(frozen_root: Path):
    inv_path, auth_path = _write_pair(frozen_root)
    inv = json.loads(inv_path.read_text())
    inv["format_version"] = "999"
    inv_path.write_text(json.dumps(inv, sort_keys=True))
    auth = json.loads(auth_path.read_text())
    auth["expected_inventory_sha256"] = _sha256(inv_path)  # keep the anchor consistent
    auth_path.write_text(json.dumps(auth, sort_keys=True))
    with pytest.raises(EvaluatorIdentityError, match="format_version"):
        ei.verify_freeze_authorization(root=frozen_root, authority_sha256=_sha256(auth_path),
                                       authority_path=auth_path)


def test_environment_mismatch_against_authority_is_refused(frozen_root: Path):
    _, auth_path = _write_pair(frozen_root)
    auth = json.loads(auth_path.read_text())
    auth["expected_environment"] = {"python_version": "0.0.0"}
    auth_path.write_text(json.dumps(auth, sort_keys=True))
    with pytest.raises(EvaluatorIdentityError, match="environment identity mismatch"):
        ei.verify_freeze_authorization(root=frozen_root, authority_sha256=_sha256(auth_path),
                                       authority_path=auth_path)


def test_member_path_traversal_is_refused(frozen_root: Path):
    inv_path, auth_path = _write_pair(frozen_root)
    inv = json.loads(inv_path.read_text())
    inv["members"] = {"../escape.py": "c" * 64}
    inv_path.write_text(json.dumps(inv, sort_keys=True))
    auth = json.loads(auth_path.read_text())
    auth["expected_inventory_sha256"] = _sha256(inv_path)
    auth_path.write_text(json.dumps(auth, sort_keys=True))
    with pytest.raises(EvaluatorIdentityError):
        ei.verify_freeze_authorization(root=frozen_root, authority_sha256=_sha256(auth_path),
                                       authority_path=auth_path)
