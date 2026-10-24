from src.filter.repeat import LINE_WINDOW, SaidBefore, drop_repeats, is_the_same
from src.review.findings import Finding


def make(file="app/stats.py", line=113, category="correctness"):
    return Finding(
        file=file, line=line, severity="high", category=category,
        message="divides by a length that can be zero",
        rationale="values arrives unchecked", confidence=0.9,
    )


def said(file="app/stats.py", line=113, category="correctness"):
    return SaidBefore(file=file, line=line, category=category)


def test_the_same_place_and_category_is_the_same_finding():
    assert is_the_same(make(), said())


def test_a_shifted_line_within_the_window_is_still_the_same():
    # A push adding three lines above moves the finding without changing it.
    assert is_the_same(make(line=113 + LINE_WINDOW), said(line=113))
    assert is_the_same(make(line=113 - LINE_WINDOW), said(line=113))


def test_a_line_outside_the_window_is_a_different_finding():
    assert not is_the_same(make(line=113 + LINE_WINDOW + 1), said(line=113))


def test_a_different_category_on_the_same_line_is_not_a_repeat():
    assert not is_the_same(make(category="performance"), said(category="correctness"))


def test_a_different_file_is_not_a_repeat():
    assert not is_the_same(make(file="app/other.py"), said(file="app/stats.py"))


def test_wording_is_not_part_of_the_comparison():
    # The generator is not deterministic, so the same bug comes back worded
    # differently. Matching on wording would dedupe almost nothing.
    reworded = Finding(
        file="app/stats.py", line=113, severity="high", category="correctness",
        message="completely different words for the same problem",
        rationale="and a different rationale too", confidence=0.7,
    )

    assert is_the_same(reworded, said())


def test_with_nothing_said_before_everything_is_fresh():
    findings = [make(), make(line=200)]

    outcome = drop_repeats(findings, [])

    assert outcome.fresh == findings
    assert outcome.repeats == []


def test_a_repeat_is_held_back_and_the_rest_go_through():
    fresh, repeat = make(line=400), make(line=113)

    outcome = drop_repeats([fresh, repeat], [said(line=114)])

    assert outcome.fresh == [fresh]
    assert [f for f, _ in outcome.repeats] == [repeat]


def test_a_repeat_carries_what_it_matched():
    earlier = said(line=114)

    outcome = drop_repeats([make(line=113)], [earlier])

    assert outcome.repeats[0][1] == earlier


def test_everything_can_be_a_repeat():
    outcome = drop_repeats([make(), make(line=200)], [said(), said(line=200)])

    assert outcome.fresh == []
    assert len(outcome.repeats) == 2


def test_order_is_kept():
    findings = [make(line=10), make(line=113), make(line=300)]

    outcome = drop_repeats(findings, [said(line=113)])

    assert [f.line for f in outcome.fresh] == [10, 300]
