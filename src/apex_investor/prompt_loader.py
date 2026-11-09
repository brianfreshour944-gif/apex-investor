"""Byte-exact loader for the approved G2 system prompt (Milestone 1).

Purpose: give the approved prompt a verifiable byte identity so no downstream component can accept
an arbitrary system prompt and treat it as approved. The renderer (a later, separately authorized
milestone) must consume the bytes returned here and must not re-derive or re-encode them.

Rules (fail closed):
  - The artifact is read as BYTES (`Path.read_bytes`), never as text.
  - The full SHA-256 is computed over the raw bytes and compared to the expected value.
  - A missing, unreadable, or empty artifact, or any hash mismatch, raises `PromptIntegrityError`.
  - The bytes are returned UNCHANGED: never stripped, decoded, re-encoded, or newline-normalized.

The approved prompt includes its exact bytes, including a single trailing newline. Any change in
those bytes (including an LF-to-CRLF conversion on checkout) changes the hash and is refused.

Known limitation (packaging): the project currently has no build/wheel-distribution process, so the
prompt artifact is resolved from the source tree. If a packaging process is introduced later, the
artifact must be added to package data so it ships inside the installed package; otherwise this
loader will not find it in an installed (non-editable) deployment.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

# The one approved prompt identity. The full SHA-256 is authoritative.
APPROVED_PROMPT_SHA256 = "5497995a5df96bb3f7cca3a2e216556c5019ef97dc0da6b730a65082f337b1b7"
APPROVED_PROMPT_BYTES = 2652
APPROVED_PROMPT_VERSION_LABEL = "g2-v1-5497995a"

# Resolved relative to THIS module, never the current working directory, so the loader works
# regardless of where the process is started.
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "g2_system_prompt.txt"


class PromptIntegrityError(Exception):
    """Raised when the prompt artifact is missing, unreadable, empty, or fails hash verification."""


def load_approved_prompt(*, expected_sha256: str = APPROVED_PROMPT_SHA256,
                         path: Path | str | None = None) -> bytes:
    """Return the approved prompt bytes, or raise `PromptIntegrityError`.

    `expected_sha256` is required (a default is provided for the approved prompt only). There is no
    "latest" behavior and no fallback: the artifact either matches the expected hash or the call
    refuses.
    """
    if not isinstance(expected_sha256, str) or not expected_sha256:
        raise PromptIntegrityError("expected_sha256 must be a non-empty string")
    artifact = Path(path) if path is not None else DEFAULT_PROMPT_PATH
    try:
        raw = artifact.read_bytes()
    except OSError as exc:
        raise PromptIntegrityError(f"cannot read prompt artifact {artifact}: {exc}") from exc
    if not raw:
        raise PromptIntegrityError(f"prompt artifact {artifact} is empty")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise PromptIntegrityError(
            f"prompt hash mismatch for {artifact}: file {actual} != approved {expected_sha256}")
    return raw
