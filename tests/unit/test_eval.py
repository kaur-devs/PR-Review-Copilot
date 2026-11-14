import json
from pathlib import Path

from src.context.bundle import ContextBundle, ContextItem
from src.evaluation.cases import Case, ExpectedFinding, load_cases
from src.evaluation.score import (
    LINE_WINDOW,
    RunScore,
    _same_place,
    score_case,
)
from src.review.findings import Finding


def expected(file="app/models.py", line=100, note="a bug"):
    return ExpectedFinding(file=file, line=line, note=note)


def finding(file="app/models.py", line=100, message="found it"):
    return Finding(
        file=file, line=line, severity="high", category="correctness",
        message=message, rationale="r", confidence=0.9,
    )


def case(expected_findings=(), expected_context=()):
    return Case(
        id="c1", repo="o/r", pr=1, installation_id=42,
        expected=tuple(expected_findings), expected_context=tuple(expected_context),
    )


def bundle(files=()):
    return ContextBundle(items=[
        ContextItem(
            file=name, start_line=1, end_line=5, snippet="x",
            relation="inbound_call", discovery="import_edge_confirmed",
            confidence=0.9, changed_symbol="f", reference_lines=(1,),
        )
        for name in files
    ])


# --- Matching a finding to a known bug ---

def test_a_finding_on_the_exact_line_matches():
    score = score_case(case([expected(line=100)]), [finding(line=100)], ContextBundle())

    assert len(score.matched) == 1
    assert score.missed == []


def test_a_finding_a_line_or_two_off_still_matches():
    """A bug on line 113 reported at 112 is the same bug."""
    score = score_case(case([expected(line=100)]), [finding(line=102)], ContextBundle())

    assert len(score.matched) == 1


def test_a_finding_far_from_the_bug_does_not_match():
    score = score_case(
        case([expected(line=100)]), [finding(line=100 + LINE_WINDOW + 1)], ContextBundle()
    )

    assert score.matched == []
    assert len(score.missed) == 1
    assert len(score.extra) == 1


def test_a_finding_in_a_different_file_does_not_match():
    score = score_case(
        case([expected(file="a.py")]), [finding(file="b.py")], ContextBundle()
    )

    assert score.matched == []


def test_a_bug_nobody_reported_is_counted_as_missed():
    score = score_case(case([expected()]), [], ContextBundle())

    assert len(score.missed) == 1
    assert score.recall == 0.0


def test_a_finding_about_nothing_known_is_counted_as_extra():
    score = score_case(case([]), [finding()], ContextBundle())

    assert len(score.extra) == 1
    assert score.precision == 0.0


def test_one_finding_cannot_satisfy_two_bugs():
    score = score_case(
        case([expected(line=100), expected(line=101)]), [finding(line=100)], ContextBundle()
    )

    assert len(score.matched) == 1
    assert len(score.missed) == 1


# --- The measurement that costs nothing ---

def test_context_recall_is_scored_without_any_model_call():
    score = score_case(
        case(expected_context=["app/caller.py"]), [], bundle(["app/caller.py"])
    )

    assert score.context_found == ["app/caller.py"]
    assert score.context_recall == 1.0


def test_a_missing_context_file_is_reported():
    score = score_case(
        case(expected_context=["app/caller.py"]), [], bundle(["app/other.py"])
    )

    assert score.context_missed == ["app/caller.py"]
    assert score.context_recall == 0.0


def test_context_recall_is_none_when_nothing_was_expected():
    assert score_case(case(), [], ContextBundle()).context_recall is None


# --- Totals across cases ---

def test_a_run_adds_up_across_cases():
    run = RunScore(label="H0")
    run.cases.append(score_case(case([expected(line=1)]), [finding(line=1)], ContextBundle()))
    run.cases.append(score_case(case([expected(line=2)]), [], ContextBundle()))

    assert run.recall == 0.5
    assert run.precision == 1.0


def test_f1_combines_both():
    run = RunScore(label="H0")
    run.cases.append(
        score_case(case([expected(line=1), expected(line=50)]),
                   [finding(line=1), finding(line=200)], ContextBundle())
    )

    assert run.precision == 0.5
    assert run.recall == 0.5
    assert run.f1 == 0.5


def test_a_run_that_found_nothing_has_no_f1():
    run = RunScore(label="H0")
    run.cases.append(score_case(case([expected()]), [], ContextBundle()))

    assert run.f1 is None


def test_failed_cases_are_counted():
    from src.evaluation.score import CaseScore

    run = RunScore(label="H0")
    run.cases.append(CaseScore(case_id="a", error="boom"))
    run.cases.append(score_case(case(), [], ContextBundle()))

    assert run.errors == 1


# --- The case file ---

def test_cases_load_from_disk(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": [{
        "id": "x", "repo": "o/r", "pr": 3, "installation_id": 42,
        "expected": [{"file": "a.py", "line": 10, "note": "a bug"}],
        "expected_context": ["b.py"],
    }]}))

    cases = load_cases(path)

    assert cases[0].id == "x"
    assert cases[0].expected[0].line == 10
    assert cases[0].expected_context == ("b.py",)


def test_the_real_case_file_is_valid():
    cases = load_cases(Path("eval/cases.json"))

    assert cases
    for item in cases:
        assert item.repo and item.pr and item.installation_id
        for bug in item.expected:
            assert bug.file and bug.line


def test_every_ablation_changes_something():
    from src.evaluation.run import ABLATIONS

    assert ABLATIONS["H0"].settings == {}
    for name in ("H1", "H2", "H3"):
        assert ABLATIONS[name].settings


def a_finding(file: str, line: int) -> Finding:
    return Finding(
        file=file, line=line, severity="high", category="correctness",
        message="m", rationale="r", confidence=0.9,
    )


# ------------------------------------------------- both ends of one defect

def test_a_finding_on_either_end_of_a_cross_file_defect_counts():
    """The first real run scored 35% recall and 67% precision. Three of
    the supposed false positives were correct findings anchored at the
    other end of a defect the ground truth pinned to one side: the
    unguarded parse reported on the function rather than its caller, the
    undefined dereference on the component rather than the hook.

    Scoring one anchor only counts a correct finding as a miss and a
    false positive at once. Re-scored with both ends, the same run was
    56% recall and 100% precision.
    """
    expected = ExpectedFinding(
        file="backend/app/routers/flags.py",
        line=107,
        also=(("backend/app/services/schedule.py", 48),),
        note="unguarded parse",
    )

    at_the_caller = a_finding(file="backend/app/routers/flags.py", line=107)
    at_the_function = a_finding(file="backend/app/services/schedule.py", line=48)
    somewhere_else = a_finding(file="backend/app/services/segments.py", line=14)

    assert _same_place(expected, at_the_caller)
    assert _same_place(expected, at_the_function)
    assert not _same_place(expected, somewhere_else)


def test_one_defect_is_claimed_once_however_many_anchors_it_has():
    """Two comments on the two ends of one defect must not score as two
    defects found."""
    case = Case(
        id="c", repo="o/r", pr=1, installation_id=1,
        expected=(
            ExpectedFinding(
                file="a.py", line=10, also=(("b.py", 20),), note="one defect"
            ),
        ),
    )
    both_ends = [a_finding(file="a.py", line=10), a_finding(file="b.py", line=20)]

    score = score_case(case, both_ends, ContextBundle())

    assert len(score.matched) == 1
    assert len(score.extra) == 1, "the second anchor is a duplicate, not a new defect"
