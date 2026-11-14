"""A linter pass before the model, on rules chosen by what they cost.

CodeRabbit runs 50+ analyzers. Switching one on wholesale here was tried
first and was worse than nothing: ruff's default bugbear and bandit sets
produced seven findings on the flagpole pull request and all seven were
wrong in context. `B008` fires on `Depends()` in an argument default,
which is the FastAPI idiom; `S105` fires on `admin_token = "change-me"`,
a placeholder. Posting those would spend the precision that is this
project's best property.

So the selector below is curated, and the test for inclusion is empirical:
a rule earns its place by firing on real defects and not firing on three
working codebases. The set here found nothing on flagpole, PR Review
Copilot or codebase-chat, and still catches a mutable default argument
and a blocking sleep inside an async function — two defect classes the
evaluation cases are built from.

What this buys is not recall for its own sake. It is the cheap, certain
half of the job done for no tokens, so the agents' budget goes on the
judgements only a model can make.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from src.review.findings import Finding

logger = logging.getLogger(__name__)

# Rules that are almost never wrong. Deliberately excluded, with reasons:
#   B008  Depends() in a default is how FastAPI is written
#   S105  flags every placeholder constant as a hardcoded password
#   F401  an unused import is not worth a comment on a pull request
RULES = (
    "F821,F811,F841,F502,F522,F632,"
    "E711,E712,E713,E714,"
    "B006,B012,B017,B023,B904,"
    "ASYNC,S608,S324,S501,PLE"
)

TIMEOUT_SECONDS = 30

# What a rule means for the person reading the comment, not how the
# linter categorises it.
SEVERITY = {
    "F821": ("high", "correctness"),
    "F811": ("medium", "correctness"),
    "F841": ("low", "maintainability"),
    "F632": ("medium", "correctness"),
    "E711": ("medium", "correctness"),
    "E712": ("medium", "correctness"),
    "E713": ("low", "maintainability"),
    "E714": ("low", "maintainability"),
    "B006": ("medium", "correctness"),
    "B012": ("high", "correctness"),
    "B017": ("low", "maintainability"),
    "B023": ("high", "correctness"),
    "B904": ("low", "error_handling"),
    "S608": ("high", "security"),
    "S324": ("medium", "security"),
    "S501": ("high", "security"),
}
DEFAULT_SEVERITY = ("medium", "correctness")

# Anything starting with these is a blocking call inside async code.
ASYNC_PREFIX = "ASYNC"


def _ruff() -> str | None:
    """Where ruff is, as an absolute path.

    Absolute because the check runs with cwd set to the snapshot, and a
    relative path into our own virtualenv does not exist from there.
    """
    local = Path(".venv/bin/ruff").resolve()
    if local.exists():
        return str(local)
    found = shutil.which("ruff")
    return found


def available() -> bool:
    return _ruff() is not None


def _grade(code: str) -> tuple[str, str]:
    if code.startswith(ASYNC_PREFIX):
        return "high", "concurrency"
    if code.startswith("PLE"):
        return "high", "correctness"
    return SEVERITY.get(code, DEFAULT_SEVERITY)


def run(root: Path, paths: list[str]) -> list[dict]:
    """Raw ruff output for these files, or nothing if it cannot run."""
    python = [p for p in paths if p.endswith(".py")]
    ruff = _ruff()
    if not python or ruff is None:
        return []

    try:
        finished = subprocess.run(
            [ruff, "check", "--select", RULES, "--output-format", "json",
             "--no-cache", "--force-exclude", *python],
            cwd=root, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        # A linter that will not run is not a reason to fail a review.
        logger.warning("ruff did not run: %s", error)
        return []

    try:
        return json.loads(finished.stdout or "[]")
    except json.JSONDecodeError:
        logger.warning("ruff returned something that is not JSON")
        return []


def findings_for(root: Path, diffs) -> list[Finding]:
    """Lint the changed files, keeping only what sits on a changed line.

    The same rule the model is held to: a comment can only attach to a
    line the diff contains, so a real problem on an untouched line is
    somebody else's pull request.
    """
    by_file = {diff.filename: diff for diff in diffs}
    results = run(root, list(by_file))

    findings: list[Finding] = []
    for item in results:
        path = str(Path(item.get("filename", "")).resolve())
        name = next(
            (f for f in by_file if path.endswith(f)), None
        )
        if name is None:
            continue

        line = int(item.get("location", {}).get("row", 0))
        if not by_file[name].can_comment_on(line):
            continue

        code = item.get("code") or ""
        severity, category = _grade(code)
        findings.append(
            Finding(
                file=name,
                line=line,
                severity=severity,
                category=category,
                message=item.get("message", "").strip(),
                rationale=f"ruff {code}, on a line this pull request changed",
                confidence=0.95,
                model="ruff",
                found_by="lint",
            )
        )

    if findings:
        logger.info("lint: %s finding(s) on changed lines", len(findings))
    return findings
