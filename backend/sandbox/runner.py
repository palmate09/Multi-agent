"""Sandbox runner: Docker (no net, limits) with local-subprocess fallback."""
from __future__ import annotations
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from schemas.messages import CodeBundle, TestSuite, TestReport, TestFailure

PY = sys.executable or "python"


def write_bundle(code: CodeBundle, tests: TestSuite, dest: Path) -> None:
    for rel, content in {**code.files, **{f"tests/{k}" if not k.startswith('test') else k: v for k, v in tests.files.items()}}.items():
        # tests go to dest root for pytest discovery; code files to dest root
        name = rel.split("/")[-1]
        target = dest / name if name.startswith("test_") else dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


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


def boot_check(code: CodeBundle, timeout: int = 30) -> tuple[bool, str]:
    tmp = Path(tempfile.mkdtemp(prefix="agent-boot-"))
    try:
        write_bundle(code, TestSuite(files={}), tmp)
        env_py = "import main; print('boot-ok')"
        p = subprocess.run([PY, "-c", env_py], cwd=tmp, capture_output=True, text=True, timeout=timeout)
        ok = "boot-ok" in p.stdout
        return ok, (p.stdout + p.stderr)[-1000:]
    except Exception as e:
        return False, str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
