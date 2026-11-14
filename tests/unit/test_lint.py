"""The linter pass, and the rules it is deliberately not asked for.

CodeRabbit runs 50+ analyzers. Switching one on wholesale was tried here
first and was worse than nothing: the default bugbear and bandit sets
produced seven findings on the flagpole pull request and every one was
wrong in context. These tests pin the rules in and the rules out.
"""

from pathlib import Path

from src.analysis.lint import RULES, _grade, findings_for, run
from src.diff.parse import parse_patch
from tests.fakes import make_patch

BAD = [
    "def collect(messages, found=[]):",
    "    found.append(messages)",
    "    return found",
]


def a_python_file(tmp_path: Path, lines: list[str], name: str = "app.py") -> Path:
    (tmp_path / name).write_text("\n".join(lines) + "\n")
    return tmp_path


def a_diff_over(lines: list[str], name: str = "app.py"):
    return parse_patch(make_patch(["# was empty"], lines), name)


def test_a_mutable_default_argument_is_caught_without_a_model(tmp_path):
    """One of the defects planted in an earlier round, found for free."""
    root = a_python_file(tmp_path, BAD)

    findings = findings_for(root, [a_diff_over(BAD)])

    assert [f.message for f in findings if "mutable" in f.message.lower()]
    assert findings[0].model == "ruff"
    assert findings[0].found_by == "lint"


def test_a_blocking_sleep_in_async_code_is_caught(tmp_path):
    """The shape of an existing evaluation case, settled mechanically."""
    lines = ["import time", "", "", "async def fetch():", "    time.sleep(1)"]
    root = a_python_file(tmp_path, lines)

    findings = findings_for(root, [a_diff_over(lines)])

    assert any("sleep" in f.message for f in findings)
    assert any(f.category == "concurrency" for f in findings)


def test_the_fastapi_idiom_is_not_reported(tmp_path):
    """B008 fires on Depends() in an argument default, which is how
    FastAPI is written. Six of the seven false positives that killed the
    first attempt at this were B008."""
    lines = [
        "from fastapi import Depends",
        "from app.db import get_db",
        "",
        "",
        "def handler(db=Depends(get_db)):",
        "    return db",
    ]
    root = a_python_file(tmp_path, lines)

    assert findings_for(root, [a_diff_over(lines)]) == []
    assert "B008" not in RULES


def test_a_placeholder_constant_is_not_called_a_hardcoded_password(tmp_path):
    """S105 fires on admin_token = "change-me". It was the seventh."""
    lines = ['admin_token: str = "change-me"']
    root = a_python_file(tmp_path, lines)

    assert findings_for(root, [a_diff_over(lines)]) == []
    assert "S105" not in RULES


def test_a_problem_on_a_line_nobody_touched_is_not_reported(tmp_path):
    """The same rule the model is held to: a comment can only attach to
    a changed line, so a real problem elsewhere is another pull request."""
    lines = ["def untouched(found=[]):", "    return found", "", "x = 1"]
    root = a_python_file(tmp_path, lines)

    # A diff that only covers the last line.
    diff = parse_patch(make_patch(["x = 0"], ["x = 1"], start=4), "app.py")

    assert findings_for(root, [diff]) == []


def test_a_missing_linter_is_not_a_failed_review(tmp_path, monkeypatch):
    monkeypatch.setattr("src.analysis.lint._ruff", lambda: None)

    assert run(tmp_path, ["app.py"]) == []


def test_severity_is_what_it_means_to_a_reader_not_the_rule_family():
    assert _grade("F821") == ("high", "correctness")
    assert _grade("ASYNC251") == ("high", "concurrency")
    assert _grade("S608") == ("high", "security")
    assert _grade("F841") == ("low", "maintainability")
