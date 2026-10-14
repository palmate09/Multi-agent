"""Sandbox runner: execute generated code in a hardened container.

Three entry points, each with two transports:

    run_tests         pytest against the generated suite
    boot_check        import the app and confirm its entry point resolves
    static_analysis   ruff + bandit over the generated source

Transport is chosen by :data:`SANDBOX_MODE`:

``container`` (default)
    A hardened container: ``--read-only``, ``--cap-drop ALL``,
    ``--security-opt no-new-privileges:true``, ``--pids-limit``, non-root uid,
    a tmpfs ``/tmp``, memory and CPU ceilings, and a compose network marked
    ``internal: true`` so there is no route to the internet. The full argv is
    built by :func:`sandbox.flags.run_argv` and asserted verbatim in
    ``backend/tests/test_sandbox_runner.py``.

``subprocess``
    A plain child process on the host, with the platform's filesystem and
    network. It exists so the project can be developed and tested without a
    container engine, and it is off in every deployed profile. It is **not**
    isolation and the log/report text says so.

``local``
    An alias for ``subprocess``, kept because that is the word the CLI and the
    older tests use.

The cost of ``container`` mode is real and stated rather than hidden: the
sandbox has no route to PyPI, so a generated app importing anything outside the
wheelhouse in ``sandbox/requirements.txt`` cannot boot. That is the intended
trade -- a prebuilt image is the only genuinely strong isolation available
without microVMs -- but it has to be legible, so the failure names the missing
package instead of leaving the user to read an ImportError.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

from agents.introspect import find_app_object
from sandbox import docker as Docker
from sandbox.flags import SandboxLimits, run_argv
from schemas.messages import CodeBundle, TestFailure, TestReport, TestSuite

log = logging.getLogger("sandbox.runner")

PY = sys.executable or "python"

#: The exact pytest invocation used for every sandbox run, local or containered.
#: ``-p no:cacheprovider`` is not a speed trick so much as a containment one:
#: pytest otherwise writes a ``.pytest_cache/`` directory into the very
#: directory the generated app owns, and under ``--read-only`` that write is an
#: outright failure. It also costs ~0.1-0.2 s per run.
PYTEST_ARGS = ("-m", "pytest", "-q", "-p", "no:cacheprovider")

#: Never fewer than this many slots, whatever the configuration says. A cap of
#: zero would deadlock every run; a cap of one would serialise the fix loop.
MIN_CONCURRENT = 1

#: Environment every sandbox container gets. The platform's credentials are not
#: merely absent -- the variables are never passed, so a generated app cannot
#: read them out of its own environment either.
SANDBOX_ENV = {
    # "You are being tested, not deployed."
    "SANDBOX": "1",
    "PREVIEW": "1",
    # Bytecode and pytest's cache both write into the working directory, which
    # is mounted read-only. Turning them off is what makes that mount legal.
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    # Data goes to the writable tmpfs, never to /app.
    "TMPDIR": "/tmp",
    "PREVIEW_DIR": "/data",
    "SQLITE_PATH": "/data/preview.db",
    "DATABASE_URL": "sqlite:////data/preview.db",
}


def sandbox_env(extra: dict | None = None) -> dict[str, str]:
    return {**SANDBOX_ENV, **(extra or {})}


def sandbox_mode() -> str:
    """``container``, ``subprocess`` or ``local``, from ``SANDBOX_MODE``."""
    mode = (os.getenv("SANDBOX_MODE", "") or "").strip().lower()
    return mode if mode in {"container", "subprocess", "local"} else "container"


def uses_docker() -> bool:
    """Whether this call should go to a container."""
    if sandbox_mode() != "container":
        return False
    if (os.getenv("SANDBOX_FORCE_LOCAL", "") or "").strip() in {"1", "true", "yes"}:
        return False
    return Docker.available()


#: Bounds how many sandbox containers this process runs at once. A burst of
#: failed fix-loop iterations -- each of which starts a container -- must not be
#: able to fork-bomb the host. A blocking semaphore rather than a rejection: the
#: pipeline is sequential per run, so the only contention is between concurrent
#: runs, and queueing them is correct.
_semaphores: dict[int, threading.BoundedSemaphore] = {}
_semaphores_guard = threading.Lock()


def _semaphore(limit: int) -> threading.BoundedSemaphore:
    with _semaphores_guard:
        sem = _semaphores.get(limit)
        if sem is None:
            sem = threading.BoundedSemaphore(max(MIN_CONCURRENT, limit))
            _semaphores[limit] = sem
        return sem


@contextmanager
def _slot(limits: SandboxLimits):
    sem = _semaphore(limits.max_concurrent)
    sem.acquire()
    try:
        yield
    finally:
        sem.release()


def _runs_volume() -> str | None:
    """The named volume the runs data lives in, when the sidecar is in play.

    The backend and the sidecar see the same volume, but the *daemon* sees
    neither: it resolves a `-v` source on the host, so the bundle path -- which
    exists only inside the backend container's mount -- would be turned into an
    empty directory on the host. The sandbox would then start, run pytest, and
    collect nothing. Naming the volume instead lets the daemon resolve it
    globally, and :func:`sandbox.flags.run_argv` sets the workdir to the bundle
    within it.
    """
    if not (os.getenv("SANDBOX_API_URL", "") or "").strip():
        return None
    return (os.getenv("RUNS_VOLUME", "") or "").strip() or None


def _mount_root() -> Path | None:
    """The directory the sidecar will accept a mount from, if one is in play.

    ``SANDBOX_MOUNT_ROOT`` is the sidecar's own confinement root; ``RUNS_DIR``
    is the same volume mounted at the same path on the backend, so either name
    resolves to it. Returns ``None`` when no sidecar is configured, because the
    confinement simply does not exist then and a temp directory is correct.
    """
    if not (os.getenv("SANDBOX_API_URL", "") or "").strip():
        return None
    for name in ("SANDBOX_MOUNT_ROOT", "RUNS_DIR"):
        raw = (os.getenv(name, "") or "").strip()
        if raw:
            root = Path(raw)
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            return root
    return None


@contextmanager
def _bundle_dir(prefix: str):
    """A scratch directory the container's non-root user can actually read.

    Created **inside the sidecar's mount root** when there is one. This is not a
    tidy-up detail: the sidecar refuses any mount outside its root, so a bundle
    under ``tempfile.gettempdir()`` (``/tmp``) makes every single containerised
    test run fail with ``mount must be inside /data/runs`` -- the sandbox cannot
    run at all, and the error reads like a confinement bug rather than the path
    choice that caused it. Both processes see the same volume at the same path,
    so a directory created here is visible to the sidecar.

    ``tempfile.mkdtemp`` creates 0700 owned by whoever started the run, and the
    sandbox runs as uid 10001 -- which cannot traverse the directory it is
    asked to read. Every pytest invocation then died with ``PermissionError:
    '/app/pytest.toml'`` during rootdir discovery, which reads like a pytest
    problem and is not one. 0755 is correct here: the contents are generated
    source the platform just wrote, and the mount is read-only anyway.
    """
    root = _mount_root()
    if root is None:
        path = Path(tempfile.mkdtemp(prefix=prefix))
    else:
        # mkdtemp under the mount root: same 0700 default, same need to widen it.
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(root)))
    try:
        os.chmod(path, 0o755)
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def write_bundle(code: CodeBundle, tests: TestSuite, dest: Path) -> None:
    """Lay the generated files out so ``pytest`` can import and collect them.

    Package-style modules (``routers/loans.py``) are written as directories and
    given an ``__init__.py``, because a generated plan can ask for several
    modules and relative imports then need a package to resolve against.
    """
    everything: dict[str, str] = {**code.files, **{f"tests/{k}": v for k, v in tests.files.items()}}
    for rel, content in everything.items():
        rel = rel.lstrip("/")
        if not rel or rel.startswith("..") or Path(rel).is_absolute():
            continue
        if rel.startswith("tests/"):
            # Test modules sit at the root so pytest's rootdir-based sys.path
            # insertion lets `import <entrypoint>` resolve.
            rel = rel[len("tests/") :]
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    # Any directory that got modules becomes a package.
    for directory in {p for p in dest.rglob("*") if p.is_dir()}:
        if any(f.suffix == ".py" for f in directory.iterdir() if f.is_file()):
            (directory / "__init__.py").touch()


def _parse_pytest(output: str) -> TestReport:
    passed = failed = 0
    m = re.search(r"(\d+) passed", output)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+) failed", output)
    if m:
        failed = int(m.group(1))
    failures: list[TestFailure] = []
    for m in re.finditer(r"(FAILED|ERROR)\s+(\S+).*?(?:Error|assert)[^\n]*", output):
        failures.append(TestFailure(name=m.group(2)[:120], error=m.group(0)[:300]))
    # Fallback: short summary lines
    if failed and not failures:
        for line in output.splitlines():
            if line.startswith("FAILED"):
                failures.append(TestFailure(name=line.split()[1][:120], error=line[:300]))
    # A collection/import error never prints "N failed", so a suite that could
    # not even be collected would otherwise report 0/0 and look green.
    if failed == 0:
        errors = re.search(r"(\d+) errors?", output)
        if errors:
            failed = int(errors.group(1))
    if failed == 0 and passed == 0 and ("ERROR collecting" in output or "INTERNALERROR" in output):
        failed = 1
    if failed == 0 and passed == 0 and output.strip() and "no tests ran" in output:
        failed = 1
    return TestReport(passed=passed, failed=failed, failures=failures[:10], raw=output[-4000:])


#: `ModuleNotFoundError: No module named 'foo'` and its dotted variants, which
#: is what a missing wheelhouse entry looks like from the inside.
_MISSING_MODULE = re.compile(r"No module named ['\"]([\w.]+)['\"]")


def explain_missing_dependencies(output: str, generated: Iterable[str] = ()) -> str:
    """Name the imports the sandbox wheelhouse could not satisfy.

    The sandbox network is ``internal: true``, so a generated app that imports
    something outside ``sandbox/requirements.txt`` cannot install it and cannot
    boot. That trade is only acceptable if the user is told what happened:
    "ModuleNotFoundError" in a pytest log reads like a bug in the generated
    code, when it is a boundary decision and the fix is to add one line to the
    wheelhouse file.

    ``generated`` is the bundle's own module names. They are excluded because
    the most commonly missing module of all is the generated entrypoint --
    ``No module named 'main'`` means the code is broken or the entry point moved,
    and telling the user to add `main` to the wheelhouse file sends them to edit
    entirely the wrong thing.
    """
    mine = {m.split(".")[0] for m in generated}
    found: list[str] = []
    for name in _MISSING_MODULE.findall(output or ""):
        top = name.split(".")[0]
        if top in found or top in mine or top in sys.stdlib_module_names:
            continue
        found.append(top)
    if not found:
        return ""
    names = ", ".join(f"`{n}`" for n in found[:6])
    many = len(found) > 1
    return (
        f"dependenc{'ies' if many else 'y'} {names} not in the sandbox wheelhouse. "
        f"The sandbox has no network access, so it cannot install "
        f"{'them' if many else 'it'}; add {'them' if many else 'it'} to "
        f"backend/sandbox/requirements.txt and rebuild the sandbox image."
    )


def _annotate(report: TestReport, output: str, generated: Iterable[str] = ()) -> TestReport:
    """Attach the wheelhouse explanation to the report's first failure."""
    note = explain_missing_dependencies(output, generated)
    if not note:
        return report
    report.failures = [
        TestFailure(name="sandbox-wheelhouse", error=note[:400]),
        *report.failures,
    ][:10]
    return report


def _failure(name: str, error: str, raw: str = "") -> TestReport:
    return TestReport(
        passed=0,
        failed=99,
        failures=[TestFailure(name=name, error=error[:400])],
        raw=raw[-2000:] or error[:2000],
    )


def run_tests_local(code: CodeBundle, tests: TestSuite, timeout: int = 60) -> TestReport:
    """Run pytest as a child process of *this* interpreter.

    Not isolation. It exists for development without a container engine, and
    the mode name is reported alongside the result so a report written from a
    subprocess run cannot be mistaken for a sandboxed one.
    """
    with _bundle_dir("agent-team-") as tmp:
        try:
            write_bundle(code, tests, tmp)
            p = subprocess.run([PY, *PYTEST_ARGS], cwd=tmp,
                               capture_output=True, text=True, timeout=timeout)
            output = p.stdout + "\n" + p.stderr
            return _annotate(_parse_pytest(output), output, _importable_modules(code))
        except subprocess.TimeoutExpired:
            return _failure("timeout", f"pytest exceeded {timeout}s in subprocess mode")


def run_tests_container(
    code: CodeBundle,
    tests: TestSuite,
    timeout: int = 60,
    limits: SandboxLimits | None = None,
) -> TestReport:
    """Run the generated suite in a hardened container.

    The image is built at most once per Dockerfile revision
    (:func:`sandbox.docker.ensure_image`) rather than once per test cycle, and
    its tag carries the Dockerfile's hash so concurrent runs cannot race on a
    shared name.
    """
    limits = limits or SandboxLimits.from_env()
    try:
        Docker.ensure_network(limits.network)
        image = Docker.ensure_image(limits.image)
    except Docker.DockerUnavailable as exc:
        return _failure("sandbox-image", f"could not prepare the sandbox: {exc}")

    with _bundle_dir("agent-team-") as tmp:
        try:
            write_bundle(code, tests, tmp)
            # The run directory is the only host path the container can see,
            # mounted :ro at /app. The container user cannot write to it, which
            # is the point: it is the platform's copy of the generated files and
            # the suite that grades them, not scratch space.
            volume = _runs_volume()
            # With a volume the bundle is addressed *inside* the mount, so the
            # workdir is the bundle's name under the mount point -- not the
            # path this process sees. The sidecar turns that back into the
            # confined host-side mount it validates before starting anything.
            inside = None
            if volume:
                rel = tmp.relative_to(_mount_root() or tmp.parent)
                inside = f"{limits.workdir}/{rel.as_posix()}"
            argv = run_argv(
                image,
                list(PYTEST_ARGS),
                limits=limits,
                mount=None if volume else str(tmp),
                mount_volume=volume,
                workdir=inside,
                env=sandbox_env(),
                entrypoint="python",
                with_data=True,
            )
            with _slot(limits):
                result = Docker.run_container(argv, timeout=timeout)
            output = result.output
            return _annotate(_parse_pytest(output), output, _importable_modules(code))
        except Docker.DockerUnavailable as exc:
            return _failure("docker", str(exc))
        except subprocess.TimeoutExpired:
            return _failure("timeout", f"sandbox container exceeded {timeout}s")
        except Exception as exc:  # a runner exception must never crash a run
            log.exception("sandbox container run failed")
            return _failure("sandbox", f"{type(exc).__name__}: {exc}")


def run_tests(
    code: CodeBundle,
    tests: TestSuite,
    timeout: int = 60,
    use_docker: bool | None = None,
) -> TestReport:
    """Run the generated suite in whatever mode is configured.

    ``use_docker=None`` (the default everywhere) means "defer to
    ``SANDBOX_MODE``". Passing ``False`` forces the subprocess path, which is
    what ``cli.py --docker=false`` and the offline tests want; passing ``True``
    requests the container but still degrades to a subprocess when no engine is
    reachable, because a run must not fail for want of one.
    """
    mode = sandbox_mode()
    if use_docker is False:
        mode = "subprocess"
    elif use_docker is True:
        mode = "container"
    if mode == "container" and uses_docker():
        return run_tests_container(code, tests, timeout)
    return run_tests_local(code, tests, timeout)


# Resolve each declared name against the modules actually generated. A contract
# may name "LudoGame" or "LudoGame.roll_dice"; the defining module is inferred
# by trying every generated module in turn.
#
# The body is substituted wholesale rather than formatted, because a format
# placeholder would collide with the probe's own braces.
_PROGRAM_PROBE = """import importlib

WANTED = __WANTED__
MODS = __MODS__


def resolve(dotted, mod_name, mod):
    parts = dotted.split('.')
    stem = mod_name.rsplit('.', 1)[-1]
    if parts and parts[0] == stem:
        parts = parts[1:]
    obj = mod
    for i, attr in enumerate(parts):
        if not hasattr(obj, attr):
            # Instance attributes (self.x) are not visible on the class.
            # Try instantiating the class and checking the instance.
            if isinstance(obj, type):
                try:
                    inst = obj()
                    if hasattr(inst, attr):
                        obj = getattr(inst, attr)
                        continue
                except Exception:
                    pass
            return False
        obj = getattr(obj, attr)
    return True


import_errors = []
found, missing = [], []
for _name in WANTED:
    _ok = False
    for _mod_name in MODS:
        try:
            _mod = importlib.import_module(_mod_name)
        except Exception as _e:
            import_errors.append(f"import {_mod_name}: {type(_e).__name__}: {_e}")
            continue
        try:
            if resolve(_name, _mod_name, _mod):
                _ok = True
                break
        except Exception:
            continue
    (found if _ok else missing).append(_name)

print('boot-resolved', len(found), 'of', len(WANTED))
if missing:
    print('MISSING:' + ','.join(missing))
if import_errors:
    # The real reason nothing resolved: show the first import failure.
    # Without this the developer sees "0 of N" and cannot fix a NameError.
    print('IMPORT-ERROR:' + import_errors[0])
"""




def boot_check(
    code: CodeBundle,
    timeout: int = 60,
    public_api: list[str] | None = None,
) -> tuple[bool, str]:
    """Import the generated code and confirm its entry point is usable.

    The module to import is resolved from the code (``agents.introspect``)
    rather than assumed to be ``main``: a real generation names the entrypoint
    whatever suits it, and hardcoding ``main`` reported "does not boot" for an
    app that imported perfectly well.

    Passing ``public_api`` switches the check from "exposes a FastAPI app" to
    "exposes these declared names", which is what a non-service project needs.
    """
    if public_api:
        modules = _importable_modules(code)
        if not modules:
            return False, "no importable python module in the bundle"
        probe = _PROGRAM_PROBE.replace(
            "__WANTED__", repr(list(public_api))
        ).replace("__MODS__", repr(modules))
    else:
        found = find_app_object(code.files)
        if not found:
            return False, "no module assigns a FastAPI() app to a module-level name"
        module, attr = found
        probe = (
            f"import {module} as _m; "
            f"a = getattr(_m, {attr!r}, None); "
            f"assert a is not None, 'module has no {attr}'; "
            f"print('boot-ok', type(a).__name__)"
        )

    with _bundle_dir("agent-boot-") as tmp:
        try:
            write_bundle(code, TestSuite(files={}), tmp)
            if uses_docker():
                out = _exec_in_container(["-c", probe], tmp, timeout)
            else:
                p = subprocess.run(
                    [PY, "-c", probe], cwd=tmp, capture_output=True, text=True, timeout=timeout
                )
                out = p.stdout + p.stderr
            ok, detail = _read_boot_verdict(out, bool(public_api), _importable_modules(code))
            return ok, detail
        except subprocess.TimeoutExpired:
            return False, f"import timed out after {timeout}s (a module probably blocks at import)"
        except Exception as e:
            return False, str(e)


def _read_boot_verdict(
    output: str, public_api: bool, generated: Iterable[str] = ()
) -> tuple[bool, str]:
    """Did the probe succeed, and what should the user be shown if it did not?"""
    if public_api:
        # "boot-resolved 0 of 3" is not a pass: every declared name must
        # actually resolve or the tests cannot import them.
        ok = "MISSING:" not in output and "boot-resolved" in output
    else:
        ok = "boot-ok" in output
    detail = output[-1000:]
    if not ok:
        note = explain_missing_dependencies(output, generated)
        if note:
            # Same trade as the test loop, reported the same way: a generated
            # app that imports something outside the wheelhouse fails at import
            # and the reason has to name the wheelhouse.
            detail = f"{note}\n\n{detail}"[-1400:]
    return ok, detail


def _exec_in_container(py_args: list[str], workdir: Path, timeout: int) -> str:
    """``python <args>`` in a hardened sandbox container. Returns combined output."""
    limits = SandboxLimits.from_env()
    try:
        Docker.ensure_network(limits.network)
        image = Docker.ensure_image(limits.image)
    except Docker.DockerUnavailable as exc:
        return f"sandbox unavailable: {exc}"
    argv = run_argv(
        image,
        py_args,
        limits=limits,
        mount=str(workdir),
        env=sandbox_env(),
        entrypoint="python",
        with_data=True,
    )
    with _slot(limits):
        return Docker.run_container(argv, timeout=timeout).output



def _importable_modules(code: CodeBundle) -> list[str]:
    """Dotted module names for each generated file, package-style where needed."""
    out: list[str] = []
    for path, body in sorted(code.files.items()):
        if not path.endswith(".py"):
            continue
        try:
            ast.parse(body)
        except SyntaxError:
            continue
        rel = path[:-3].strip("/")
        if rel.endswith(".__init__"):
            rel = rel[: -len(".__init__")]
        out.append(rel.replace("/", "."))
    return out


def static_analysis(code: CodeBundle) -> tuple[str, str]:
    """``(ruff_output, bandit_output)`` over the generated source.

    Both tools run inside the sandbox image, in the same containment as pytest.
    That is not tidiness: linting generated code means parsing it, and ruff and
    bandit both resolve imports and walk the filesystem. Running them as
    children of the platform process would execute the analysis of
    model-generated paths with the platform's privileges.
    """
    with _bundle_dir("agent-lint-") as tmp:
        try:
            write_bundle(code, TestSuite(files={}), tmp)
            if uses_docker():
                ruff_out = _exec_in_container(["-m", "ruff", "check", "."], tmp, 60)
                bandit_out = _exec_in_container(["-m", "bandit", "-q", "-r", "."], tmp, 60)
                return ruff_out[-2000:], bandit_out[-2000:]
            r = subprocess.run([PY, "-m", "ruff", "check", "."], cwd=tmp, capture_output=True, text=True, timeout=30)
            ruff_out = (r.stdout + r.stderr)[-2000:]
            b = subprocess.run([PY, "-m", "bandit", "-q", "-r", "."], cwd=tmp, capture_output=True, text=True, timeout=30)
            bandit_out = (b.stdout + b.stderr)[-2000:]
            return ruff_out, bandit_out
        except Exception as e:
            return str(e), ""

