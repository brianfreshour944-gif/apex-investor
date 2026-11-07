"""Container-level tests for the persistent-volume fix (require Docker; skipped otherwise).

These build the REAL image and run the REAL container against root-owned mounts, proving the
end-to-end behaviour that unit tests cannot:
  - a root-owned mounted /app/reports becomes writable by UID 10001 (report saved);
  - a root-owned mounted /app/data becomes writable (fresh price cache written);
  - the scheduler + Python process actually run as UID 10001, never root;
  - persisted state survives destroying and recreating the container on the same volume.

They are deliberately opt-in via APEX_DOCKER_TESTS=1 because they need Docker, network access
and a base-image pull. They are NOT a substitute for verifying the real Coolify deployment.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "apex-investor:test"
APP_UID = "10001"

pytestmark = pytest.mark.skipif(
    os.environ.get("APEX_DOCKER_TESTS") != "1",
    reason="set APEX_DOCKER_TESTS=1 to run container tests (needs Docker + network)",
)


def _docker_prefix() -> list[str]:
    """Use plain `docker`, or `sudo docker` when the daemon socket needs elevation."""
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    if subprocess.run(["docker", "info"], capture_output=True).returncode == 0:
        return ["docker"]
    if subprocess.run(["sudo", "-n", "docker", "info"], capture_output=True).returncode == 0:
        return ["sudo", "docker"]
    pytest.skip("docker daemon not reachable")


def _docker(*args: str, check: bool = True, timeout: int = 180) -> subprocess.CompletedProcess:
    r = subprocess.run([*_docker_prefix(), *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {r.stderr}")
    return r


@pytest.fixture(scope="module")
def image() -> str:
    _docker("build", "-t", IMAGE, str(ROOT))
    return IMAGE


def _run_detached(name: str, volumes: list[tuple[Path, str]], env: dict[str, str]) -> str:
    """Run the image detached, bind-mounting each (host_path, container_path) pair."""
    _docker("rm", "-f", name, check=False)
    args = ["run", "-d", "--name", name]
    for host, container in volumes:
        args += ["-v", f"{host}:{container}"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    args.append(IMAGE)
    _docker(*args)
    return name


def _logs_until(name: str, needle: str, *, timeout: int = 60) -> str:
    """Poll logs until `needle` appears or the timeout elapses."""
    deadline = time.time() + timeout
    out = ""
    while time.time() < deadline:
        out = _docker("logs", name).stdout + _docker("logs", name).stderr
        if needle in out:
            return out
        time.sleep(2)
    return out


@pytest.fixture()
def root_owned(tmp_path_factory):
    """A host dir tree owned by root:root, mimicking a Coolify bind mount / pre-existing volume."""
    base = tmp_path_factory.mktemp("vol")
    reports = base / "reports"
    data = base / "data"
    reports.mkdir()
    data.mkdir()
    # Seed the example portfolio so an example-mode run has something to report.
    shutil.copy(ROOT / "data" / "portfolio.example.json", data / "portfolio.example.json")
    # Own the MOUNTED dirs as root:root (mimics a Coolify bind mount). The parent is left alone
    # so the test process can still traverse in and inspect the results.
    subprocess.run(["sudo", "chown", "-R", "root:root", str(reports), str(data)], check=True)
    yield {"reports": reports, "data": data}
    # The container writes files owned by UID 10001; pytest's own cleanup cannot remove those, so
    # remove them here to avoid noisy teardown warnings (a no-op if nothing was created).
    subprocess.run(["sudo", "rm", "-rf", str(base)], check=False)


def test_root_owned_reports_becomes_writable(image, root_owned):
    """The exact reported failure: report generation works, persistence must now succeed."""
    name = "apex-t-reports"
    try:
        _run_detached(name, [(root_owned["reports"], "/app/reports")], {"PORTFOLIO_MODE": "example"})
        out = _logs_until(name, "written:")
        assert "PermissionError" not in out, out
        assert "written: reports/daily/" in out, out
        # The report file exists on the host mount and is owned by UID 10001.
        written = list(root_owned["reports"].rglob("*.md"))
        assert written, "no report file on the mounted volume"
        owner = subprocess.run(["stat", "-c", "%u", str(written[0])],
                               capture_output=True, text=True).stdout.strip()
        assert owner == APP_UID, f"report owned by {owner}, expected {APP_UID}"
    finally:
        _docker("rm", "-f", name, check=False)


def test_process_runs_as_app_uid(image, root_owned):
    """The scheduler and Python process must run as UID 10001, never root.

    Checked via /proc/1/status (the REAL init/app process), not `docker exec id`, which reports
    the image USER for an exec'd shell and would be misleading.
    """
    name = "apex-t-uid"
    try:
        _run_detached(name, [(root_owned["reports"], "/app/reports")], {"PORTFOLIO_MODE": "example"})
        _logs_until(name, "next scheduled run")
        status = _docker("exec", name, "grep", "-E", "^Uid", "/proc/1/status").stdout
        # e.g. "Uid:\t10001\t10001\t10001\t10001"
        assert status.split()[1] == APP_UID, f"PID 1 not running as {APP_UID}: {status!r}"
    finally:
        _docker("rm", "-f", name, check=False)


def test_fresh_price_cache_written_to_root_owned_data(image, root_owned):
    """A root-owned /app/data must not force cache-fallback: a fresh cache must be written."""
    name = "apex-t-cache"
    try:
        _run_detached(name, [(root_owned["reports"], "/app/reports"), (root_owned["data"], "/app/data")],
                      {"PORTFOLIO_MODE": "example"})
        out = _logs_until(name, "written:")
        assert "PermissionError" not in out, out
        # A live run refreshes from the network, so provenance must include at least one 'network'.
        assert "network" in out, f"no network provenance — cache write likely failed:\n{out}"
        cache_files = list(root_owned["data"].rglob("*.json"))
        assert any("cache" in str(p) for p in cache_files), "no cache files written"
    finally:
        _docker("rm", "-f", name, check=False)


def test_direct_app_uid_start_fails_closed(image, root_owned):
    """A container started directly as UID 10001 skips the repair and must fail closed."""
    name = "apex-t-directuid"
    try:
        _docker("rm", "-f", name, check=False)
        # `--user 10001` mimics a runtime `user:` override that bypasses the root repair step.
        _docker("run", "-d", "--name", name, "--user", APP_UID,
                "-v", f"{root_owned['reports']}:/app/reports",
                "-e", "PORTFOLIO_MODE=example", IMAGE)
        out = _logs_until(name, "ownership-repair")
        assert "without the root ownership-repair step" in out, out
        # No report should have been produced on the still-root-owned volume.
        assert not list(root_owned["reports"].rglob("*.md")), "report written despite bypass"
    finally:
        _docker("rm", "-f", name, check=False)


def test_persistence_survives_container_recreation(image, root_owned):
    """State on the same named volume must survive destroying and recreating the container."""
    name = "apex-t-persist"
    try:
        _run_detached(name, [(root_owned["reports"], "/app/reports"), (root_owned["data"], "/app/data")],
                      {"PORTFOLIO_MODE": "example"})
        _logs_until(name, "written:")
        before = sorted(p.name for p in root_owned["reports"].rglob("*.md"))
        assert before, "nothing written to persist"
        _docker("rm", "-f", name)
        # Recreate against the SAME mounts.
        _run_detached(name, [(root_owned["reports"], "/app/reports"), (root_owned["data"], "/app/data")],
                      {"PORTFOLIO_MODE": "example"})
        _logs_until(name, "next scheduled run")
        after = sorted(p.name for p in root_owned["reports"].rglob("*.md"))
        assert set(before).issubset(set(after)), "reports did not survive recreation"
    finally:
        _docker("rm", "-f", name, check=False)
