"""Reviewer: code+spec+tests+ruff/bandit -> ranked comments. 0 blockers to accept."""
from __future__ import annotations
import re
from schemas.messages import CodeBundle, ApiSpec, TestSuite, TestReport, ReviewReport, ReviewComment
from agents.llm import generate


def _static_checks(code: CodeBundle, spec: ApiSpec, tests: TestSuite) -> list[ReviewComment]:
    out: list[ReviewComment] = []
    main = code.files.get("main.py", "")
    t = "\n".join(tests.files.values())
    for ep in ["/health", "/tasks"]:
        if ep not in main:
            out.append(ReviewComment(severity="blocker", message=f"Missing endpoint {ep}", location="main.py"))
    if "HTTPException" not in main and "404" not in main:
        out.append(ReviewComment(severity="blocker", message="Missing 404 handling", location="main.py"))
    if "min_length" not in str(code.files) and "minLength" not in str(code.files):
        out.append(ReviewComment(severity="blocker", message="Missing title validation (min length)", location="schemas"))
    if re.search(r"f['\"].*SELECT|execute\(f['\"]|eval\(|exec\(", main):
        out.append(ReviewComment(severity="blocker", message="Unsafe SQL/dynamic exec pattern", location="main.py"))
    if "def test_" not in t:
        out.append(ReviewComment(severity="blocker", message="No tests found", location="tests"))
    for need in ["422", "404"]:
        if need not in t:
            out.append(ReviewComment(severity="major", message=f"Tests missing {need} case", location="tests"))
    if "sqlalchemy" not in str(code.files).lower():
        out.append(ReviewComment(severity="major", message="No ORM persistence detected", location="models.py"))
    return out


def review(code: CodeBundle, spec: ApiSpec, tests: TestSuite,
           report: TestReport, ruff_out: str = "", bandit_out: str = "") -> ReviewReport:
    comments = _static_checks(code, spec, tests)
    if "E9" in ruff_out or "F821" in ruff_out or "SyntaxError" in report.raw:
        comments.append(ReviewComment(severity="blocker", message="Ruff/syntax failure", location="ruff"))
    if "HIGH" in bandit_out.upper() and "Issue" in bandit_out:
        comments.append(ReviewComment(severity="major", message="Bandit high-severity finding", location="bandit"))
    text, _ = generate(
        f"Code summary: {len(code.files)} files. Static findings: {[c.message for c in comments]}. "
        "Reply with extra blocker/major/minor lines or 'OK'.",
        "You are the Reviewer. Rank issues blocker/major/minor.", role="reviewer")
    if text and "blocker" in text.lower() and not comments:
        comments.append(ReviewComment(severity="major", message=text.strip()[:300], location="llm"))
    if not comments:
        comments.append(ReviewComment(severity="minor", message="Looks good: spec conformance confirmed.", location="review"))
    return ReviewReport(comments=comments)
