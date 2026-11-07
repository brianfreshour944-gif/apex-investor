"""Regression tests for docker/entrypoint.sh's same-day scheduler guard (F5).

These run the REAL `run_report` function and the REAL `STATE_FILE` assignment, both extracted
verbatim from the script, under bash with a stub `python` on PATH. Only the `/app/data` base
directory is substituted for a temp dir (the container path is not writable here); the
`${PORTFOLIO_MODE}` suffix logic is exercised unmodified.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"


def _extract_assignment(prefix: str) -> str:
    for line in ENTRYPOINT.read_text().splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"{prefix!r} not found in entrypoint")


def _extract_run_report() -> str:
    text = ENTRYPOINT.read_text()
    start = text.index("run_report() {")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _state_template() -> str:
    """The STATE_FILE line with the container data dir replaced by a placeholder."""
    return _extract_assignment("STATE_FILE=").replace("/app/data", "__DATA__")


def _marker_name(mode: str, tmp_path: Path) -> str:
    """Resolve the marker filename for `mode` by evaluating the real STATE_FILE line."""
    line = _state_template().replace("__DATA__", str(tmp_path))
    r = subprocess.run(["bash", "-c", f'PORTFOLIO_MODE="{mode}"; {line}; printf "%s" "$STATE_FILE"'],
                       capture_output=True, text=True)
    return Path(r.stdout).name


def _run(tmp_path: Path, *, mode: str, exit_code: int = 0, calls: int = 1,
         create_portfolio: bool = True):
    """Run the real run_report() `calls` times; return (output, marker_path)."""
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "python").write_text(f"#!/bin/sh\nexit {exit_code}\n")
    (stub / "python").chmod(0o755)
    (tmp_path / "data").mkdir(exist_ok=True)
    if create_portfolio:
        (tmp_path / "data" / "portfolio.json").write_text("{}")

    state_line = _state_template().replace("__DATA__", str(tmp_path))
    funcs = tmp_path / f"defs_{mode}.sh"
    funcs.write_text(state_line + "\n" + _extract_run_report())
    script = tmp_path / f"harness_{mode}.sh"
    script.write_text(
        "set -uo pipefail\n"
        f'PORTFOLIO_MODE="{mode}"\n'
        f'export PATH="{stub}:$PATH"\n'
        f'source "{funcs}"\n'
        + "run_report\n" * calls
        + "exit 0\n"          # the real entrypoint loops forever; run_report's own exit status
    )                         # is discarded there, so mirror that instead of asserting on it
    r = subprocess.run(["bash", str(script)], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout + r.stderr, tmp_path / _marker_name(mode, tmp_path)


def test_marker_written_with_real_newline(tmp_path):
    """Regression: the marker must end in a real newline, not a literal backslash-n."""
    _, marker = _run(tmp_path, mode="example", exit_code=0)
    assert marker.exists()
    assert marker.read_bytes().endswith(b"\n")
    assert b"\\n" not in marker.read_bytes()            # would be the old bug


def test_second_same_day_run_is_skipped(tmp_path):
    """After a successful run the same day, a second invocation must skip."""
    out, marker = _run(tmp_path, mode="example", exit_code=0, calls=2)
    assert marker.exists()
    assert out.count("=== apex-investor report run @") == 1     # second call skipped
    assert "skipping duplicate run" in out


def test_failed_run_does_not_mark_day_done(tmp_path):
    """A non-zero report exit must NOT write the marker, so the day can be retried."""
    out, marker = _run(tmp_path, mode="example", exit_code=1)
    assert not marker.exists()
    assert "day not marked done" in out


def test_live_missing_portfolio_fails_closed_and_does_not_mark(tmp_path):
    """live mode with no portfolio.json must not run python nor mark the day done."""
    out, marker = _run(tmp_path, mode="live", exit_code=0, create_portfolio=False)
    assert not marker.exists()
    assert "FAILING CLOSED" in out
