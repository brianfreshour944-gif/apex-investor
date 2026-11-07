"""Regression tests for the persistent-volume ownership repair + privilege drop.

These exercise the REAL guard block, sliced verbatim from `docker/entrypoint.sh` and run under
bash with stubbed `id`/`chown`/`mkdir`/`setpriv`, so the logic is covered without needing to be
root or to have Docker. The container-level behaviour (root-owned mounts actually becoming
writable, the process actually running as UID 10001) is proven separately in
`tests/test_container_persistence.py` when Docker is available.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
DOCKERFILE = ROOT / "Dockerfile"
APP_UID = "10001"


def _guard_block() -> str:
    """Slice the real ownership-repair + privilege-drop block out of the entrypoint."""
    text = ENTRYPOINT.read_text()
    start = text.index("APP_UID=")
    end = text.index('REPORT_TIME_UTC="${REPORT_TIME_UTC:-21:30}"')
    return text[start:end]


def _write_stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _stub_bin(tmp_path: Path, *, uid: int, setpriv: bool, chown_ok: bool,
              mkdir_ok: bool) -> Path:
    b = tmp_path / "bin"
    b.mkdir()
    _write_stub(b, "id", f'[ "$1" = "-u" ] && echo {uid}\n')
    _write_stub(b, "chown", "exit 0\n" if chown_ok else "exit 1\n")
    _write_stub(b, "mkdir", "exit 0\n" if mkdir_ok else "exit 1\n")
    if setpriv:
        _write_stub(b, "setpriv", 'echo "DROPPED"\nexit 0\n')
    return b


def _run_guard(tmp_path: Path, *, uid: int, setpriv: bool = True, chown_ok: bool = True,
               mkdir_ok: bool = True, marker: str | None = None) -> subprocess.CompletedProcess:
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    b = _stub_bin(tmp_path, uid=uid, setpriv=setpriv, chown_ok=chown_ok, mkdir_ok=mkdir_ok)
    # Redirect the container's persistent paths to the temp dir; the guard logic is unmodified.
    block = (_guard_block()
             .replace("/app/data", str(tmp_path / "data"))
             .replace("/app/reports", str(tmp_path / "reports")))
    script = tmp_path / "guard.sh"
    marker_env = "" if marker is None else f'export APEX_ENTRYPOINT_PRIVDROPPED="{marker}"\n'
    script.write_text(
        "set -uo pipefail\n"
        f'export PATH="{b}"\n'
        + marker_env
        + block
        + '\necho "REACHED_APP"\n'
    )
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, cwd=tmp_path)


# --- the drop itself -------------------------------------------------------

def test_root_repairs_then_drops_privileges(tmp_path):
    """As root: the guard repairs ownership and execs setpriv (never reaches the app as root)."""
    r = _run_guard(tmp_path, uid=0)
    assert r.returncode == 0, r.stderr
    assert "DROPPED" in r.stdout                       # privilege drop happened
    assert "REACHED_APP" not in r.stdout               # never continued as root
    assert "FATAL" not in r.stdout


def test_app_uid_with_marker_passes_through(tmp_path):
    """The legitimate post-drop run (UID 10001 + marker) proceeds to the app."""
    r = _run_guard(tmp_path, uid=10001, marker="1")
    assert r.returncode == 0, r.stderr
    assert "REACHED_APP" in r.stdout
    assert "FATAL" not in r.stdout


def test_direct_app_uid_without_marker_fails_closed(tmp_path):
    """A container started DIRECTLY as UID 10001 skips the repair, so it must fail closed."""
    r = _run_guard(tmp_path, uid=10001, marker=None)
    assert r.returncode == 1
    assert "without the root ownership-repair step" in r.stdout + r.stderr
    assert "REACHED_APP" not in r.stdout


def test_wrong_nonroot_uid_is_fatal(tmp_path):
    """A non-root UID other than 10001 (e.g. a stale compose `user:`) must stop startup."""
    r = _run_guard(tmp_path, uid=5)
    assert r.returncode == 1
    assert f"application must run as UID {APP_UID}" in r.stdout + r.stderr
    assert "REACHED_APP" not in r.stdout


def test_root_path_sets_marker_before_dropping(tmp_path):
    """The marker must be exported by the root path before setpriv re-executes the script."""
    block = _guard_block()
    marker_line = 'export APEX_ENTRYPOINT_PRIVDROPPED=1'
    assert marker_line in block, "root path does not export the privdrop marker"
    assert block.index(marker_line) < block.index("exec setpriv"), \
        "marker must be set before the privilege drop"


# --- the two safeguards the operator required ------------------------------

def test_missing_setpriv_stops_startup(tmp_path):
    """setpriv must be verified, not assumed; its absence is a clear fatal error."""
    r = _run_guard(tmp_path, uid=0, setpriv=False)
    assert r.returncode == 1
    assert "setpriv is required" in r.stdout + r.stderr
    assert "REACHED_APP" not in r.stdout


def test_chown_failure_stops_startup(tmp_path):
    """A failed ownership repair must NOT be silently ignored."""
    r = _run_guard(tmp_path, uid=0, chown_ok=False)
    assert r.returncode == 1
    assert "cannot repair persistent-volume ownership" in r.stdout + r.stderr
    assert "REACHED_APP" not in r.stdout


def test_mkdir_failure_stops_startup(tmp_path):
    """If the persistent directories cannot be created, startup stops with a clear error."""
    r = _run_guard(tmp_path, uid=0, mkdir_ok=False)
    assert r.returncode == 1
    assert "cannot create persistent directories" in r.stdout + r.stderr
    assert "REACHED_APP" not in r.stdout


# --- structural guards (cheap, catch a reverted Dockerfile) ----------------

def test_dockerfile_does_not_force_nonroot_user():
    """The entrypoint must start as root so it can repair a pre-existing volume."""
    lines = [ln.strip() for ln in DOCKERFILE.read_text().splitlines()]
    user_directives = [ln for ln in lines if ln.upper().startswith("USER ")]
    assert user_directives == [], f"unexpected USER directive(s): {user_directives}"


def test_dockerfile_still_creates_appuser():
    text = DOCKERFILE.read_text()
    assert "useradd" in text and APP_UID in text


def test_no_world_writable_workaround():
    """`chmod 777` must never be used as a workaround (a mention in a comment is fine)."""
    import re
    pattern = re.compile(r"chmod[^\n]*\b777\b")
    for f in (ENTRYPOINT, DOCKERFILE):
        assert not pattern.search(f.read_text()), f"chmod 777 found in {f.name}"
