from src.filter.suggest import (
    MINIMUM_CONFIDENCE,
    apply_to_source,
    is_valid_python,
    keeps_its_place,
    files_to_read,
    passes_cheap_checks,
    settle,
    survives_parsing,
)
from src.review.findings import Finding

SOURCE = """def average(values):
    total = sum(values)
    return total / len(values)
"""


def make(**overrides):
    fields = {
        "file": "app/stats.py",
        "line": 3,
        "severity": "high",
        "category": "correctness",
        "message": "divides by the length of a list that can be empty",
        "rationale": "values comes from the caller unchecked",
        "confidence": 0.9,
        "suggestion": "    return total / len(values) if values else 0.0",
    }
    fields.update(overrides)
    return Finding(**fields)


def test_one_line_in_can_be_several_lines_out():
    patched = apply_to_source(
        SOURCE, 3, "    if not values:\n        return 0.0\n    return total / len(values)"
    )

    assert patched.splitlines() == [
        "def average(values):",
        "    total = sum(values)",
        "    if not values:",
        "        return 0.0",
        "    return total / len(values)",
    ]


def test_replacing_a_line_that_is_not_there_gives_nothing():
    assert apply_to_source(SOURCE, 99, "x = 1") is None
    assert apply_to_source(SOURCE, 0, "x = 1") is None


def test_a_sound_replacement_survives_parsing():
    assert survives_parsing(SOURCE, 3, "    return total / max(len(values), 1)").allow


def test_a_replacement_indented_wrong_is_caught():
    decision = survives_parsing(SOURCE, 3, "return total / len(values)")

    assert not decision.allow
    assert "stop the file parsing" in decision.reason


def test_a_replacement_that_is_not_python_is_caught():
    decision = survives_parsing(SOURCE, 3, "    return total / len(values")

    assert not decision.allow


def test_a_file_that_already_does_not_parse_refuses_rather_than_guesses():
    decision = survives_parsing("def broken(:\n    pass\n", 2, "    pass")

    assert not decision.allow
    assert "does not parse as it is" in decision.reason


def test_a_high_confidence_finding_with_a_suggestion_passes_the_cheap_checks():
    assert passes_cheap_checks(make()).allow


def test_a_finding_with_no_suggestion_is_not_a_candidate():
    assert not passes_cheap_checks(make(suggestion=None)).allow


def test_medium_severity_does_not_carry_a_suggestion():
    decision = passes_cheap_checks(make(severity="medium"))

    assert not decision.allow
    assert "medium severity" in decision.reason


def test_confidence_below_the_bar_does_not_carry_a_suggestion():
    decision = passes_cheap_checks(make(confidence=MINIMUM_CONFIDENCE - 0.05))

    assert not decision.allow
    assert "below" in decision.reason


def test_a_finding_the_judge_could_not_confirm_does_not_carry_a_suggestion():
    finding = make()

    decision = passes_cheap_checks(finding, unverified={finding})

    assert not decision.allow
    assert "judge" in decision.reason


def test_only_candidate_files_are_worth_reading():
    assert files_to_read([make(), make(severity="low")]) == {"app/stats.py"}
    assert files_to_read([make(suggestion=None)]) == set()


def test_a_sound_suggestion_is_kept():
    outcome = settle([make()], sources={"app/stats.py": SOURCE})

    assert outcome.offered == 1
    assert outcome.refused == []
    assert outcome.findings[0].suggestion


def test_a_refused_suggestion_still_leaves_the_comment():
    outcome = settle([make(severity="medium")], sources={"app/stats.py": SOURCE})

    assert len(outcome.findings) == 1
    assert outcome.findings[0].suggestion is None
    assert outcome.findings[0].message == make().message


def test_a_file_we_could_not_read_refuses_the_suggestion():
    outcome = settle([make()], sources={})

    assert outcome.offered == 0
    assert outcome.refused[0][1] == "the file could not be read, so it was not checked"


def test_a_finding_that_never_had_a_suggestion_is_not_reported_as_refused():
    outcome = settle([make(suggestion=None)], sources={"app/stats.py": SOURCE})

    assert outcome.refused == []
    assert len(outcome.findings) == 1


def test_findings_keep_their_order_and_none_are_lost():
    findings = [make(line=3), make(line=2, severity="low"), make(line=3)]

    outcome = settle(findings, sources={"app/stats.py": SOURCE})

    assert len(outcome.findings) == 3
    assert [f.line for f in outcome.findings] == [3, 2, 3]


NESTED = """def charge(amount, dry_run):
    if not dry_run:
        log(amount)
        send(amount)
"""


def test_a_replacement_that_compiles_but_escapes_its_block_is_caught():
    # Valid Python, and a silent behaviour change: send() now runs even when
    # dry_run is true. compile() alone has no objection to this.
    patched = apply_to_source(NESTED, 4, "    send(amount)")
    assert is_valid_python(patched)

    decision = keeps_its_place(NESTED, 4, "    send(amount)")

    assert not decision.allow
    assert "indented differently" in decision.reason


def test_a_replacement_at_the_same_depth_is_fine():
    assert keeps_its_place(NESTED, 4, "        send(amount, retries=3)").allow


def test_a_multi_line_replacement_is_judged_on_its_first_real_line():
    assert keeps_its_place(
        NESTED, 4, "        if amount > 0:\n            send(amount)"
    ).allow


def test_leading_blank_lines_do_not_confuse_the_check():
    assert keeps_its_place(NESTED, 4, "\n        send(amount)").allow


def test_a_blank_suggestion_is_refused():
    decision = keeps_its_place(NESTED, 4, "   \n  ")

    assert not decision.allow
    assert "blank" in decision.reason


def test_tabs_and_spaces_are_not_interchangeable():
    tabbed = "def f():\n\treturn 1\n"

    assert keeps_its_place(tabbed, 2, "\treturn 2").allow
    assert not keeps_its_place(tabbed, 2, "    return 2").allow


def test_what_cpython_rejects():
    assert is_valid_python("x = 1\n")
    assert not is_valid_python("def f(:\n    pass\n")
    assert not is_valid_python("def f():\n    pass\nreturn 1\n")
    assert not is_valid_python("def f():\n    x = 1\n      y = 2\n")
    assert not is_valid_python("x = 'unclosed\n")
