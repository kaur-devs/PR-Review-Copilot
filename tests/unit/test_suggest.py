from src.filter.suggest import (
    MINIMUM_CONFIDENCE,
    apply_to_source,
    is_valid_source,
    keeps_its_place,
    realign,
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
    assert survives_parsing(SOURCE, 3, "    return total / max(len(values), 1)", "a.py").allow


def test_a_replacement_the_model_forgot_to_indent_is_shifted_into_place():
    # The common model mistake: the right fix at column zero.
    decision = survives_parsing(SOURCE, 3, "return total / max(len(values), 1)", "a.py")

    assert decision.allow
    assert decision.suggestion == "    return total / max(len(values), 1)"


def test_a_replacement_indented_with_the_wrong_character_is_refused():
    tabbed = "def average(values):\n\treturn sum(values) / len(values)\n"

    decision = survives_parsing(tabbed, 2, "    return 0.0", "a.py")

    assert not decision.allow
    assert "indented differently" in decision.reason


def test_a_replacement_that_is_not_python_is_caught():
    decision = survives_parsing(SOURCE, 3, "    return total / len(values", "a.py")

    assert not decision.allow


def test_a_file_that_already_does_not_parse_refuses_rather_than_guesses():
    decision = survives_parsing("def broken(:\n    pass\n", 2, "    pass", "a.py")

    assert not decision.allow
    assert "does not parse as it is" in decision.reason


def test_a_high_confidence_finding_with_a_suggestion_passes_the_cheap_checks():
    assert passes_cheap_checks(make()).allow


def test_a_finding_with_no_suggestion_is_not_a_candidate():
    assert not passes_cheap_checks(make(suggestion=None)).allow


def test_medium_severity_carries_a_suggestion():
    # Severity is the model's opinion about impact. On the first live run it
    # called a crash "medium" and three correct suggestions were thrown away.
    assert passes_cheap_checks(make(severity="medium")).allow


def test_low_severity_does_not_carry_a_suggestion():
    decision = passes_cheap_checks(make(severity="low"))

    assert not decision.allow
    assert "low severity" in decision.reason


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
    assert files_to_read([make(), make(confidence=0.1)]) == {"app/stats.py"}
    assert files_to_read([make(suggestion=None)]) == set()


def test_a_sound_suggestion_is_kept():
    outcome = settle([make()], sources={"app/stats.py": SOURCE})

    assert outcome.offered == 1
    assert outcome.refused == []
    assert outcome.findings[0].suggestion


def test_a_refused_suggestion_still_leaves_the_comment():
    outcome = settle([make(confidence=0.4)], sources={"app/stats.py": SOURCE})

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
    assert is_valid_source("app/stats.py", patched)

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
    assert is_valid_source("app/stats.py", "x = 1\n")
    assert not is_valid_source("app/stats.py", "def f(:\n    pass\n")
    assert not is_valid_source("app/stats.py", "def f():\n    pass\nreturn 1\n")
    assert not is_valid_source("app/stats.py", "def f():\n    x = 1\n      y = 2\n")
    assert not is_valid_source("app/stats.py", "x = 'unclosed\n")


# ---------------------------------------------------------------------------
# Shifting a suggestion into place
# ---------------------------------------------------------------------------

# The real shape from the first live run: a dict literal inside a function,
# where the model returned the replacement at column zero.
RETURNS_A_DICT = '''def retrieve_context(db, repo_id, question):
    similar_chunks = search(db, repo_id, question)
    sources = []

    return {
        "primary_chunks": similar_chunks,
        "linked_chunks": [],
    }
'''


def test_the_case_from_the_first_live_run():
    # Line 6 is '        "primary_chunks": similar_chunks,' - eight spaces.
    decision = survives_parsing(
        RETURNS_A_DICT, 6, '"chunks": similar_chunks,', "a.py"
    )

    assert decision.allow
    assert decision.suggestion == '        "chunks": similar_chunks,'


def test_an_already_correct_suggestion_is_left_exactly_as_it_is():
    text = "    return total / len(values) if values else 0.0"

    assert realign(SOURCE, 3, text) == text


def test_a_multi_line_suggestion_keeps_its_own_shape():
    shifted = realign(SOURCE, 3, "if not values:\n    return 0.0\nreturn total / len(values)")

    assert shifted.splitlines() == [
        "    if not values:",
        "        return 0.0",
        "    return total / len(values)",
    ]


def test_an_over_indented_suggestion_is_shifted_back():
    assert realign(SOURCE, 3, "            return 0.0") == "    return 0.0"


def test_blank_lines_are_not_given_trailing_whitespace():
    shifted = realign(SOURCE, 3, "x = 1\n\ny = 2")

    assert shifted.splitlines() == ["    x = 1", "", "    y = 2"]


def test_a_block_with_nothing_to_give_up_is_not_shifted():
    # Shifting left by four would have to eat whitespace the second line does
    # not have, so the shift is refused rather than guessed at.
    assert realign(SOURCE, 3, "        if values:\n  pass") is None


def test_shifting_preserves_relative_shape_and_leaves_the_rest_to_compile():
    # Both lines move by the same amount, which keeps their difference. That
    # is correct even when the difference was nonsense to begin with, and the
    # compile check is what rejects it.
    shifted = realign(SOURCE, 3, "        if values:\n     pass")

    assert shifted == "    if values:\n pass"
    assert not is_valid_source("app/stats.py", apply_to_source(SOURCE, 3, shifted))


def test_tabs_and_spaces_cannot_be_shifted_into_each_other():
    tabbed = "def f():\n\treturn 1\n"

    assert realign(tabbed, 2, "    return 2") is None
    assert realign(SOURCE, 3, "\treturn 2") is None


def test_a_blank_suggestion_cannot_be_shifted():
    assert realign(SOURCE, 3, "   \n  ") is None


def test_a_shifted_suggestion_is_what_reaches_the_finding():
    outcome = settle(
        [make(suggestion="return total / max(len(values), 1)")],
        sources={"app/stats.py": SOURCE},
    )

    assert outcome.offered == 1
    assert outcome.findings[0].suggestion == "    return total / max(len(values), 1)"


def test_shifting_does_not_rescue_a_suggestion_that_will_not_compile():
    outcome = settle(
        [make(suggestion="return total / len(values")],
        sources={"app/stats.py": SOURCE},
    )

    assert outcome.offered == 0
    assert "stop the file parsing" in outcome.refused[0][1]
