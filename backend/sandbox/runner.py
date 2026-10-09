"""Sandbox runner: Docker (no net, limits) with local-subprocess fallback."""
from __future__ import annotations
import ast
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from agents.introspect import find_app_object
from schemas.messages import CodeBundle, TestFailure, TestReport, TestSuite

PY = sys.executable or "python"


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


def run_tests_local(code: CodeBundle, tests: TestSuite, timeout: int = 60) -> TestReport:
    tmp = Path(tempfile.mkdtemp(prefix="agent-team-"))
    try:
        write_bundle(code, tests, tmp)
        p = subprocess.run([PY, "-m", "pytest", "-q"], cwd=tmp,
                           capture_output=True, text=True, timeout=timeout)
        return _parse_pytest(p.stdout + "\n" + p.stderr)
    except subprocess.TimeoutExpired:
        return TestReport(passed=0, failed=99, failures=[TestFailure(name="timeout", error="pytest timeout")], raw="timeout")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_tests(code: CodeBundle, tests: TestSuite, timeout: int = 60, use_docker: bool = False) -> TestReport:
    if use_docker and shutil.which("docker"):
        try:
            tmp = Path(tempfile.mkdtemp(prefix="agent-team-"))
            write_bundle(code, tests, tmp)
            subprocess.run(["docker", "build", "-q", "-t", "agent-team-sandbox", str(Path(__file__).parent)],
                           capture_output=True, timeout=120)
            p = subprocess.run(["docker", "run", "--rm", "--network", "none",
                                "--memory", "512m", "--cpus", "1",
                                "-v", f"{tmp}:/app", "agent-team-sandbox"],
                               capture_output=True, text=True, timeout=timeout)
            return _parse_pytest(p.stdout + "\n" + p.stderr)
        except Exception as e:
            return TestReport(passed=0, failed=99, failures=[TestFailure(name="docker", error=str(e)[:300])], raw=str(e))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
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

    tmp = Path(tempfile.mkdtemp(prefix="agent-boot-"))
    try:
        write_bundle(code, TestSuite(files={}), tmp)
        p = subprocess.run(
            [PY, "-c", probe], cwd=tmp, capture_output=True, text=True, timeout=timeout
        )
        if public_api:
            # "boot-resolved 0 of 3" is not a pass: every declared name must
            # actually resolve or the tests cannot import them.
            ok = "MISSING:" not in p.stdout and "boot-resolved" in p.stdout
        else:
            ok = "boot-ok" in p.stdout
        return ok, (p.stdout + p.stderr)[-1000:]
    except subprocess.TimeoutExpired:
        return False, f"import timed out after {timeout}s (a module probably blocks at import)"
    except Exception as e:
        return False, str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



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
    tmp = Path(tempfile.mkdtemp(prefix="agent-lint-"))
    try:
        write_bundle(code, TestSuite(files={}), tmp)
        r = subprocess.run([PY, "-m", "ruff", "check", "."], cwd=tmp, capture_output=True, text=True, timeout=30)
        ruff_out = (r.stdout + r.stderr)[-2000:]
        b = subprocess.run([PY, "-m", "bandit", "-q", "-r", "."], cwd=tmp, capture_output=True, text=True, timeout=30)
        bandit_out = (b.stdout + b.stderr)[-2000:]
        return ruff_out, bandit_out
    except Exception as e:
        return str(e), ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
