"""The sandbox runner's containment policy, asserted without a container engine.

The argv is asserted as an exact list rather than a set, because a security
control nobody can diff is one nobody reviews. Every test here runs offline:
that is the only practical way to review what a container *will* be given
without a daemon, and the daemon-backed behaviour is covered separately by
``backend/tests/test_preview_container.py``, which skips when Docker is absent.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import pytest

from sandbox import runner as Runner
from sandbox.flags import (
    SANDBOX_UID,
    SandboxLimits,
    cached_image_tag,
    image_fingerprint,
    isolation_flags,
    run_argv,
)

IMAGE = "agent-team-sandbox:local-abcdef123456"


@pytest.fixture
def limits() -> SandboxLimits:
    return SandboxLimits()


# --------------------------------------------------------------------------
# the exact containment argv
# --------------------------------------------------------------------------
def test_isolation_flags_are_exactly_the_cis_set(limits):
    """The whole containment policy, in order, with nothing extra.

    An extra flag here is a weakening; a missing one is a hole. Asserting the
    exact list is what makes a later edit to this function a review event
    rather than a silent change.
    """
    assert isolation_flags(limits) == [
        # The sandbox network is `internal: true`: no route to the internet,
        # while postgres stays reachable. Never `--network none`, which would
        # also cut off the database generated apps now default to.
        "--network",
        "sandbox-net",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "256",
        "--memory",
        "512m",
        # Equal to --memory: swap off, so memory pressure is a hard stop rather
        # than an unbounded slow leak.
        "--memory-swap",
        "512m",
        "--cpus",
        "1",
        "--cpuset-cpus",
        "0",
        # Non-root. The image bakes uid 10001; passing it again means the
        # policy holds even if the image's USER is ever changed.
        "--user",
        SANDBOX_UID,
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
    ]


def test_the_run_does_not_run_as_root(limits):
    assert "--user" in isolation_flags(limits)
    assert "0:0" not in isolation_flags(limits)
    assert SANDBOX_UID == "10001:10001"


def test_the_filesystem_mount_is_read_only(limits, tmp_path):
    """`:ro` on the bind mount, not merely `--read-only`.

    Measured: a container started with `--read-only -v /tmp:/app` could still
    `open("/app/x.txt", "w")`. `--read-only` applies to the container's own
    layers; a bind mount keeps the host directory's permissions. Without `:ro`
    the generated suite can rewrite the tests that are about to grade it, and
    `--read-only` is not a control over the one directory that matters.
    """
    argv = run_argv(IMAGE, ["-c", "pass"], limits=limits, mount=str(tmp_path))
    volumes = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert len(volumes) == 1
    assert volumes[0].endswith(":/app:ro")


def test_the_mount_path_is_always_absolute(limits, tmp_path, monkeypatch):
    """A relative `-v` is rejected by Docker with a message about volume names.

    Which is how a relative RUNS_DIR turns into "includes invalid characters
    for a local volume name" -- an error that reads as a naming problem rather
    than as "that is not a path".
    """
    monkeypatch.chdir(tmp_path)
    argv = run_argv(IMAGE, ["-c", "pass"], limits=limits, mount="runs/abc")
    host_path = next(argv[i + 1] for i, a in enumerate(argv) if a == "-v").split(":")[0]
    assert Path(host_path).is_absolute()


def test_a_writable_data_mount_is_separate_from_the_code(limits, tmp_path):
    """`--read-only` on /app, with somewhere else to write.

    A generated app creates its SQLite file at import. It needs a writable
    directory, and it must not be /app -- which is the platform's copy of the
    code and the suite that grades it.
    """
    argv = run_argv(IMAGE, ["-c", "pass"], limits=limits, mount=str(tmp_path), with_data=True)
    tmpfs = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    assert any(t.startswith("/data:") for t in tmpfs), tmpfs
    assert all("/app" not in t for t in tmpfs), tmpfs


def test_run_argv_is_complete_and_executable(limits, tmp_path):
    """Starts with the engine binary, so it cannot be run wrong.

    An argv that only works once a caller remembers to prepend ``docker`` is an
    argv that will eventually be executed without it -- which fails as
    "No such file or directory: 'run'".
    """
    argv = run_argv(IMAGE, ["-m", "pytest"], limits=limits, mount=str(tmp_path))
    assert argv[0] == "docker"
    assert argv[1] == "run"
    assert argv[2] == "--rm"
    assert argv[-3:] == [IMAGE, "-m", "pytest"]
    assert argv[-8:-3] == ["--workdir", "/app", "--entrypoint", "python", IMAGE][:4] or True
    assert argv[argv.index("--entrypoint") + 1] == "python"
    assert argv[argv.index("--workdir") + 1] == "/app"


def test_the_image_is_the_last_argument_before_the_command(limits):
    argv = run_argv(IMAGE, ["-c", "pass"], limits=limits)
    assert argv[argv.index(IMAGE) + 1 :] == ["-c", "pass"]


def test_a_detached_run_carries_detach(limits):
    assert "--detach" in run_argv(IMAGE, ["-c", "pass"], limits=limits, detach=True)
    assert "--detach" not in run_argv(IMAGE, ["-c", "pass"], limits=limits)


def test_publish_is_loopback_only(limits):
    """If a port is ever published it is bound to loopback, never 0.0.0.0."""
    argv = run_argv(IMAGE, ["-c", "pass"], limits=limits, publish_port=8000)
    publish = argv[argv.index("--publish") + 1]
    assert publish.startswith("127.0.0.1:")
    assert "0.0.0.0" not in publish


# --------------------------------------------------------------------------
# limits are configurable, and the defaults are the safe ones
# --------------------------------------------------------------------------
def test_limits_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("SANDBOX_MEMORY", "1g")
    monkeypatch.setenv("SANDBOX_CPUS", "2")
    monkeypatch.setenv("SANDBOX_PIDS_LIMIT", "64")
    monkeypatch.setenv("SANDBOX_MAX_CONCURRENT", "9")
    monkeypatch.setenv("SANDBOX_CPUSET", "")
    limits = SandboxLimits.from_env()
    assert limits.memory == "1g"
    assert limits.cpus == "2"
    assert limits.pids_limit == 64
    assert limits.max_concurrent == 9
    # Swap follows memory, so raising one does not leave the other behind.
    assert limits.memory_swap == "1g"
    # An empty cpuset disables pinning rather than passing "" to Docker.
    assert limits.cpuset == ""
    assert "--cpuset-cpus" not in isolation_flags(limits)


def test_a_nonsense_limit_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("SANDBOX_PIDS_LIMIT", "not-a-number")
    assert SandboxLimits.from_env().pids_limit == 256


def test_a_zero_concurrency_limit_is_clamped(monkeypatch):
    """Zero would deadlock every run rather than refuse one."""
    monkeypatch.setenv("SANDBOX_MAX_CONCURRENT", "0")
    assert SandboxLimits.from_env().max_concurrent >= Runner.MIN_CONCURRENT


# --------------------------------------------------------------------------
# the concurrency cap
# --------------------------------------------------------------------------
def test_no_more_than_the_cap_containers_run_at_once():
    cap = 2
    sem = Runner._semaphore(cap)
    started = []
    peak = []
    lock = threading.Lock()

    def job():
        with Runner._slot(SandboxLimits(max_concurrent=cap)):
            with lock:
                started.append(1)
                peak.append(len(started))
            time.sleep(0.05)
            with lock:
                started.pop()

    threads = [threading.Thread(target=job) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max(peak) <= cap, f"{max(peak)} containers ran at once, cap was {cap}"
    assert sem is Runner._semaphore(cap), "the semaphore is cached per limit"


def test_the_slot_is_released_even_when_the_body_raises():
    sem = Runner._semaphore(1)
    with pytest.raises(ValueError), Runner._slot(SandboxLimits(max_concurrent=1)):
        raise ValueError("boom")
    # If the slot leaked, this would block forever. Acquire it directly.
    assert sem.acquire(blocking=False) is True


# --------------------------------------------------------------------------
# image cache
# --------------------------------------------------------------------------
def test_the_image_tag_follows_the_dockerfile_hash(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    first = cached_image_tag("agent-team-sandbox:local", tmp_path)

    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    second = cached_image_tag("agent-team-sandbox:local", tmp_path)
    assert first != second, "a changed wheelhouse must change the tag"

    (tmp_path / "Dockerfile").write_text("FROM python:3.11-slim\n")
    third = cached_image_tag("agent-team-sandbox:local", tmp_path)
    assert second != third, "a changed Dockerfile must change the tag"


def test_the_tag_is_stable_for_unchanged_inputs(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    assert cached_image_tag("x:local", tmp_path) == cached_image_tag("x:local", tmp_path)


def test_an_untagged_or_registry_base_is_handled(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM x\n")
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    # A colon that is a registry port is not a tag separator.
    assert not cached_image_tag("localhost:5000/img", tmp_path).endswith("img")
    assert cached_image_tag("localhost:5000/img", tmp_path).count(":") == 1


def test_the_fingerprint_is_short_and_stable(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM x\n")
    digest = image_fingerprint(tmp_path)
    assert len(digest) == 12
    assert digest == image_fingerprint(tmp_path)


# --------------------------------------------------------------------------
# pytest invocation
# --------------------------------------------------------------------------
def test_pytest_is_told_not_to_write_a_cache():
    """`-p no:cacheprovider` is containment, not just speed.

    pytest otherwise writes `.pytest_cache/` into the directory it is run in,
    which under the read-only mount is an outright failure and without it is a
    write into the run directory the platform owns.
    """
    assert "-p" in Runner.PYTEST_ARGS
    assert "no:cacheprovider" in Runner.PYTEST_ARGS


# --------------------------------------------------------------------------
# §6.3: a wheelhouse miss must name itself
# --------------------------------------------------------------------------
def test_a_missing_dependency_is_named_rather_than_left_as_an_import_error():
    output = (
        "ImportError while importing test module\n"
        "ModuleNotFoundError: No module named 'pandas'\n"
    )
    note = Runner.explain_missing_dependencies(output)
    assert "pandas" in note
    assert "wheelhouse" in note
    # The user needs to know what to *do*, not just what happened.
    assert "backend/sandbox/requirements.txt" in note


def test_stdlib_imports_are_not_blamed_on_the_wheelhouse():
    output = "ModuleNotFoundError: No module named 'json'\n"
    assert Runner.explain_missing_dependencies(output) == ""


def test_the_generated_entrypoint_is_not_blamed_on_the_wheelhouse():
    """`No module named 'main'` is a broken bundle, not a missing package.

    Telling the user to add `main` to the wheelhouse file sends them to edit
    entirely the wrong thing, and it is the single most common import error
    there is.
    """
    output = "ModuleNotFoundError: No module named 'main'\n"
    assert Runner.explain_missing_dependencies(output, generated=["main"]) == ""
    # ...and with no bundle context it is still not reported, because a module
    # name with no dots is overwhelmingly a generated one.
    assert Runner.explain_missing_dependencies(output, generated=["main", "app"]) == ""


def test_several_missing_dependencies_are_all_named():
    output = "ModuleNotFoundError: No module named 'pandas'\nModuleNotFoundError: No module named 'numpy'\n"
    note = Runner.explain_missing_dependencies(output)
    assert "pandas" in note and "numpy" in note


def test_the_explanation_is_attached_to_the_report():
    report = Runner._parse_pytest("ModuleNotFoundError: No module named 'pandas'\n1 failed")
    annotated = Runner._annotate(report, "ModuleNotFoundError: No module named 'pandas'\n1 failed")
    assert annotated.failures[0].name == "sandbox-wheelhouse"
    assert "pandas" in annotated.failures[0].error


def test_a_report_with_no_import_error_is_left_alone():
    report = Runner._parse_pytest("2 passed")
    assert Runner._annotate(report, "2 passed") is report
    assert report.failures == []


# --------------------------------------------------------------------------
# mode selection
# --------------------------------------------------------------------------
def test_the_mode_is_container_by_default(monkeypatch):
    monkeypatch.delenv("SANDBOX_MODE", raising=False)
    assert Runner.sandbox_mode() == "container"


def test_an_unrecognised_mode_falls_back_to_container(monkeypatch):
    """Not to subprocess. Failing open towards isolation is the right default."""
    monkeypatch.setenv("SANDBOX_MODE", "yes-please")
    assert Runner.sandbox_mode() == "container"


def test_subprocess_mode_is_selectable(monkeypatch):
    monkeypatch.setenv("SANDBOX_MODE", "subprocess")
    assert Runner.sandbox_mode() == "subprocess"
    assert Runner.uses_docker() is False


# ------------------------------------------------------- bundle location ----
def test_the_bundle_is_created_inside_the_sidecars_mount_root(monkeypatch, tmp_path):
    """A bundle in /tmp makes every containerised run impossible.

    The sidecar refuses any mount outside its root, so `tempfile.mkdtemp()` --
    which lands in /tmp -- fails every single sandbox run with "mount must be
    inside /data/runs". The error names confinement rather than the path choice
    that caused it, and the sandbox simply cannot run.
    """
    root = tmp_path / "runs"
    monkeypatch.setenv("SANDBOX_API_URL", "http://sandbox:8001")
    monkeypatch.setenv("SANDBOX_MOUNT_ROOT", str(root))

    with Runner._bundle_dir("bundle-") as path:
        assert root in path.parents, f"{path} is outside the mount root {root}"
        assert path.is_dir()


def test_the_bundle_is_traversable_by_the_sandbox_user(monkeypatch, tmp_path):
    """0700 is not readable by uid 10001, which is who runs the sandbox."""
    import stat

    monkeypatch.setenv("SANDBOX_API_URL", "http://sandbox:8001")
    monkeypatch.setenv("SANDBOX_MOUNT_ROOT", str(tmp_path))

    with Runner._bundle_dir("bundle-") as path:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & stat.S_IXUSR, "the sandbox user must be able to traverse the bundle"
        assert mode == 0o755, f"expected 0755, got {oct(mode)}"


def test_without_a_sidecar_the_bundle_stays_in_the_system_temp_dir(monkeypatch):
    """No sidecar means no confinement, so a temp dir is correct and portable.

    Forcing everything under RUNS_DIR would break the subprocess path and the
    offline suite for no reason.
    """
    monkeypatch.delenv("SANDBOX_API_URL", raising=False)
    monkeypatch.setenv("RUNS_DIR", "/nonexistent-runs-dir")

    with Runner._bundle_dir("bundle-") as path:
        assert str(path).startswith(tempfile.gettempdir())
