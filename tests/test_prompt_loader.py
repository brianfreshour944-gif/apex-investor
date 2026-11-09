"""Milestone 1 tests: byte-exact prompt loader — positive load, mutations, CRLF, missing newline,
empty/missing files, and artifact immutability. Offline and deterministic; no model, no network.
"""
from __future__ import annotations

import hashlib

import pytest

from apex_investor.prompt_loader import (
    APPROVED_PROMPT_BYTES,
    APPROVED_PROMPT_SHA256,
    DEFAULT_PROMPT_PATH,
    PromptIntegrityError,
    load_approved_prompt,
)


def test_positive_load_exact_bytes_and_hash():
    raw = load_approved_prompt()
    assert isinstance(raw, bytes)
    assert len(raw) == APPROVED_PROMPT_BYTES == 2652
    assert hashlib.sha256(raw).hexdigest() == APPROVED_PROMPT_SHA256


def test_trailing_newline_preserved_and_no_cr():
    raw = load_approved_prompt()
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\r\n")
    assert b"\r" not in raw


def test_load_is_byte_identical_to_on_disk_artifact():
    # The loader must return the file's exact bytes, unmodified.
    assert load_approved_prompt() == DEFAULT_PROMPT_PATH.read_bytes()


def test_crlf_conversion_refused(tmp_path):
    artifact = tmp_path / "crlf.txt"
    artifact.write_bytes(DEFAULT_PROMPT_PATH.read_bytes().replace(b"\n", b"\r\n"))
    with pytest.raises(PromptIntegrityError):
        load_approved_prompt(path=artifact)


def test_missing_trailing_newline_refused(tmp_path):
    artifact = tmp_path / "no_newline.txt"
    artifact.write_bytes(DEFAULT_PROMPT_PATH.read_bytes()[:-1])
    with pytest.raises(PromptIntegrityError):
        load_approved_prompt(path=artifact)


def test_one_byte_mutation_refused(tmp_path):
    mutated = bytearray(DEFAULT_PROMPT_PATH.read_bytes())
    mutated[100] ^= 0x01
    artifact = tmp_path / "mutated.txt"
    artifact.write_bytes(bytes(mutated))
    with pytest.raises(PromptIntegrityError):
        load_approved_prompt(path=artifact)


def test_empty_file_refused(tmp_path):
    artifact = tmp_path / "empty.txt"
    artifact.write_bytes(b"")
    with pytest.raises(PromptIntegrityError):
        load_approved_prompt(path=artifact)


def test_missing_file_refused(tmp_path):
    with pytest.raises(PromptIntegrityError):
        load_approved_prompt(path=tmp_path / "does_not_exist.txt")


def test_wrong_expected_hash_refused():
    # A caller-supplied expected hash that does not match the artifact is refused, even for the
    # real artifact path.
    with pytest.raises(PromptIntegrityError):
        load_approved_prompt(expected_sha256="0" * 64)


def test_blank_expected_hash_refused():
    with pytest.raises(PromptIntegrityError):
        load_approved_prompt(expected_sha256="")


def test_artifact_not_modified_by_loading():
    before = DEFAULT_PROMPT_PATH.read_bytes()
    load_approved_prompt()
    assert DEFAULT_PROMPT_PATH.read_bytes() == before
