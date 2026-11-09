"""Evaluator identity and freeze-authorization verification (B1).

Self-contained and stdlib-only. This module:

  * defines the evaluator's scoring closure -- the modules whose code can change a score;
  * computes a deterministic closure hash over a ``{path: sha256}`` mapping;
  * verifies an authority -> inventory -> closure -> members -> environment chain, fail-closed at
    every step;
  * builds the execution-environment identity recorded in produced artifacts.

It never imports the scoring modules, never touches the network, the model, or the frozen
evidence. The trust anchor is supplied INDEPENDENTLY: the authority file's own SHA-256 comes from
out-of-band (the command line), and the file that is VERIFIED (the inventory) is never the file
that AUTHORIZES (the authority).
"""
from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path

SUPPORTED_FORMAT_VERSIONS = frozenset({"1"})

# The evaluator's scoring closure: the modules whose code can change a score. This tuple is the
# human-readable definition; the freeze authority pins this exact membership independently, and the
# verifier refuses any inventory whose membership differs.
SCORING_CLOSURE_MEMBERS: tuple[str, ...] = (
    "src/apex_investor/citation.py",
    "src/apex_investor/docstore.py",
    "src/apex_investor/model_gate.py",
    "src/apex_investor/sanitize.py",
)

DEFAULT_AUTHORITY_PATH = Path("data/model_gate/freeze_authority.v1.json")

# Environment fields that must be present and non-empty for a freeze-required execution. These are
# the fields that name the reproducibility boundary; other recorded fields are informational.
REQUIRED_ENV_FIELDS: tuple[str, ...] = (
    "python_implementation",
    "python_version",
    "os_system",
    "architecture",
    "libc",
)


class EvaluatorIdentityError(Exception):
    """Raised when the evaluator identity or freeze authorization cannot be trusted."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_sha256(obj: object) -> str:
    # Must match model_gate.canonical_sha256 exactly (the consumer recomputes with that function).
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(payload)


def compute_closure_sha256(members: dict[str, str]) -> str:
    """Deterministic closure hash: sha256 over the canonical, sorted ``{path: sha256}`` mapping."""
    return _canonical_sha256({str(k): str(v) for k, v in members.items()})


def environment_identity() -> dict:
    """The execution-environment identity recorded in produced artifacts.

    Discrete fields (no single vague platform string): implementation + version, OS, kernel release
    (informational), architecture, libc identity/version, and the dependency posture.
    """
    libc_name, libc_version = platform.libc_ver()
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "os_system": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "libc": [libc_name or "", libc_version or ""],
        "dependencies": "stdlib-only",
    }


def _read_bytes(path: Path | str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise EvaluatorIdentityError(f"cannot read {path}: {exc}") from exc


def _parse_object(raw: bytes, path: Path | str) -> dict:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluatorIdentityError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise EvaluatorIdentityError(f"{path} must contain a JSON object")
    return obj


def _require_format_version(obj: dict, path: Path | str) -> str:
    version = obj.get("format_version")
    if not isinstance(version, str) or not version:
        raise EvaluatorIdentityError(f"{path} is missing a non-empty format_version")
    if version not in SUPPORTED_FORMAT_VERSIONS:
        raise EvaluatorIdentityError(f"{path} format_version {version!r} is unsupported")
    return version


def _safe_member_path(rel: object) -> str:
    if not isinstance(rel, str) or not rel:
        raise EvaluatorIdentityError(f"closure member path must be a non-empty string: {rel!r}")
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise EvaluatorIdentityError(f"closure member path must be repository-relative: {rel!r}")
    return rel


def check_environment(env: dict, *, expected: dict | None = None) -> None:
    """Fail closed when a required environment field is missing, or mismatches ``expected``."""
    for field in REQUIRED_ENV_FIELDS:
        value = env.get(field)
        if field == "libc":
            ok = isinstance(value, (list, tuple)) and len(value) == 2 and all(str(v) for v in value)
        else:
            ok = isinstance(value, str) and bool(value)
        if not ok:
            raise EvaluatorIdentityError(f"environment identity is missing or malformed: {field!r}")
    if expected is not None:
        for field, want in expected.items():
            got = env.get(field)
            if json.dumps(got, sort_keys=True) != json.dumps(want, sort_keys=True):
                raise EvaluatorIdentityError(
                    f"environment identity mismatch for {field!r}: {got!r} != expected {want!r}")


def build_inventory(root: Path | str, members: tuple[str, ...] = SCORING_CLOSURE_MEMBERS) -> dict:
    """Build an inventory record from the current member files (regeneration helper)."""
    root = Path(root)
    files = {rel: _sha256_bytes(_read_bytes(root / rel)) for rel in sorted(members)}
    return {
        "format_version": "1",
        "closure_sha256": compute_closure_sha256(files),
        "members": files,
    }


def build_authority(
    *,
    inventory_relpath: str,
    inventory_bytes: bytes,
    expected_members: tuple[str, ...] = SCORING_CLOSURE_MEMBERS,
) -> dict:
    """Build an authority record that pins an inventory file's bytes and closure hash."""
    inventory = _parse_object(inventory_bytes, inventory_relpath)
    return {
        "format_version": "1",
        "inventory_path": inventory_relpath,
        "expected_inventory_sha256": _sha256_bytes(inventory_bytes),
        "expected_closure_sha256": inventory["closure_sha256"],
        "expected_members": sorted(expected_members),
    }


def verify_freeze_authorization(
    *,
    root: Path | str,
    authority_sha256: str,
    authority_path: Path | str = DEFAULT_AUTHORITY_PATH,
    inventory_path: Path | str | None = None,
) -> dict:
    """Verify authority -> inventory -> closure -> members -> environment, fail-closed.

    ``authority_sha256`` is the independently trusted anchor (supplied out-of-band, e.g. on the
    command line). The authority file pins the inventory file's SHA-256, the expected closure hash,
    and the exact member set. The file being verified (the inventory) is never its own authority.

    Returns the verified identity: ``closure_sha256``, ``members``, ``environment``, plus the
    ``authority_sha256`` and ``inventory_sha256`` actually used.
    """
    root = Path(root)
    if not isinstance(authority_sha256, str) or not authority_sha256:
        raise EvaluatorIdentityError(
            "a trusted authority SHA-256 must be supplied independently (--authority-sha256)")

    authority_raw = _read_bytes(authority_path)
    actual_authority_sha = _sha256_bytes(authority_raw)
    if actual_authority_sha != authority_sha256:
        raise EvaluatorIdentityError(
            f"freeze authority hash mismatch: {actual_authority_sha} != {authority_sha256}")

    authority = _parse_object(authority_raw, authority_path)
    _require_format_version(authority, authority_path)

    expected_members = authority.get("expected_members")
    if (not isinstance(expected_members, list) or not expected_members
            or not all(isinstance(m, str) and m for m in expected_members)):
        raise EvaluatorIdentityError("freeze authority is missing a valid expected_members list")
    if sorted(expected_members) != sorted(SCORING_CLOSURE_MEMBERS):
        raise EvaluatorIdentityError(
            "freeze authority expected_members does not match the defined scoring closure")

    expected_inventory_sha = authority.get("expected_inventory_sha256")
    if not isinstance(expected_inventory_sha, str) or not expected_inventory_sha:
        raise EvaluatorIdentityError("freeze authority is missing expected_inventory_sha256")
    expected_closure_sha = authority.get("expected_closure_sha256")
    if not isinstance(expected_closure_sha, str) or not expected_closure_sha:
        raise EvaluatorIdentityError("freeze authority is missing expected_closure_sha256")

    inventory_relpath = authority.get("inventory_path")
    if not isinstance(inventory_relpath, str) or not inventory_relpath:
        raise EvaluatorIdentityError("freeze authority is missing inventory_path")
    inv_path = Path(inventory_path) if inventory_path is not None else root / inventory_relpath
    inv_raw = _read_bytes(inv_path)
    actual_inventory_sha = _sha256_bytes(inv_raw)
    if actual_inventory_sha != expected_inventory_sha:
        raise EvaluatorIdentityError(
            f"inventory hash mismatch: {actual_inventory_sha} != {expected_inventory_sha}")

    inventory = _parse_object(inv_raw, inv_path)
    _require_format_version(inventory, inv_path)

    members = inventory.get("members")
    if not isinstance(members, dict) or not members:
        raise EvaluatorIdentityError("inventory is missing its members mapping")
    if set(members) != set(expected_members):
        missing = sorted(set(expected_members) - set(members))
        extra = sorted(set(members) - set(expected_members))
        raise EvaluatorIdentityError(
            f"inventory membership does not match the authority (missing={missing}, extra={extra})")

    for rel in sorted(members):
        rel = _safe_member_path(rel)
        want = members[rel]
        if not isinstance(want, str) or not want:
            raise EvaluatorIdentityError(f"inventory member {rel!r} must map to a sha256 string")
        actual = _sha256_bytes(_read_bytes(root / rel))
        if actual != want:
            raise EvaluatorIdentityError(f"closure member {rel} hash mismatch: {actual} != {want}")

    recomputed = compute_closure_sha256(members)
    declared = inventory.get("closure_sha256")
    if declared != recomputed:
        raise EvaluatorIdentityError(
            f"inventory closure_sha256 {declared!r} does not match the recomputed {recomputed}")
    if declared != expected_closure_sha:
        raise EvaluatorIdentityError(
            f"closure hash {declared!r} does not match the authority's expected {expected_closure_sha}")

    env = environment_identity()
    expected_env = authority.get("expected_environment")
    if expected_env is not None and not isinstance(expected_env, dict):
        raise EvaluatorIdentityError("freeze authority expected_environment must be an object")
    check_environment(env, expected=expected_env)

    return {
        "closure_sha256": recomputed,
        "members": dict(sorted(members.items())),
        "environment": env,
        "authority_sha256": actual_authority_sha,
        "inventory_sha256": actual_inventory_sha,
    }
