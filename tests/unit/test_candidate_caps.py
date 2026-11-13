"""Merging and capping what the fan-out collects.

Both are list logic, tested directly: driving twenty agents through a
fake provider to observe a slice would test asyncio, not the rule.
"""

from src.agents.specialists import MAX_CANDIDATES_TOTAL, _dedup
from src.review.findings import Finding


def a_finding(file="app/billing.py", line=1, severity="high", **over):
    return Finding(
        file=file,
        line=line,
        severity=severity,
        category=over.pop("category", "correctness"),
        message="charge gained a required parameter",
        rationale="app/orders.py:12 calls charge with one argument",
        confidence=over.pop("confidence", 0.85),
        **over,
    )


def test_the_same_file_line_and_category_is_one_finding():
    kept, duplicates = _dedup([a_finding(), a_finding(), a_finding()])

    assert len(kept) == 1
    assert duplicates == 2


def test_the_most_confident_wording_is_the_one_kept():
    kept, _ = _dedup(
        [a_finding(confidence=0.6), a_finding(confidence=0.95), a_finding(confidence=0.8)]
    )

    assert kept[0].confidence == 0.95


def test_one_line_gets_one_comment_whatever_the_category_says():
    """Review 22 put two comments on ScheduleBadge.tsx:24 for a single
    unguarded dereference, one filed error_handling and one correctness.
    The category is the model's label for a bug, not a second bug."""
    kept, duplicates = _dedup(
        [a_finding(category="concurrency"), a_finding(category="error_handling")]
    )

    assert len(kept) == 1
    assert duplicates == 1


def test_a_finding_in_another_file_is_never_merged_away():
    """Proximity only merges within one file."""
    kept, _ = _dedup([a_finding(), a_finding(file="app/orders.py")])

    assert {finding.file for finding in kept} == {"app/billing.py", "app/orders.py"}


def test_what_survives_the_cap_is_the_most_serious():
    """Verification is the expensive stage, so the fan-out cannot hand it
    an unbounded pile — but it must not be the high-severity ones that go."""
    low = [
        a_finding(line=index * 10, severity="low", confidence=0.4,
                  category="performance")
        for index in range(1, MAX_CANDIDATES_TOTAL + 10)
    ]
    high = a_finding(line=500, severity="high", confidence=0.95)

    kept, _ = _dedup(low + [high])
    capped = kept[:MAX_CANDIDATES_TOTAL]

    assert len(capped) == MAX_CANDIDATES_TOTAL
    assert capped[0] is high
    assert all(item.severity == "low" for item in capped[1:])


# ------------------------------------------------------ how far an agent ranges

from src.agents.loop import MAX_TURNS
from src.agents.specialists import TURNS_FOR_A_SMALL_CHANGE, _turn_budget
from src.diff.parse import parse_patch
from tests.fakes import make_patch


def a_diff_of(lines: int):
    before = ["def f():", "    pass"]
    after = ["def f():"] + [f"    x{n} = {n}" for n in range(lines)]
    return parse_patch(make_patch(before, after), "app/thing.py")


def test_a_small_change_gets_a_shallow_pass():
    """ChatPanel.jsx changed 23 lines, spent eight turns and 23,768
    tokens, and found nothing."""
    assert _turn_budget(a_diff_of(5)) == TURNS_FOR_A_SMALL_CHANGE


def test_a_large_change_keeps_the_full_budget():
    assert _turn_budget(a_diff_of(200)) == MAX_TURNS


def test_a_small_change_is_still_reviewed():
    """main.py changed five lines and the CORS bug was in them. Fewer
    turns, never fewer files."""
    assert _turn_budget(a_diff_of(3)) >= 1


def test_two_findings_a_line_apart_are_one_problem_seen_twice():
    """Review 23 posted three comments for one unguarded fromisoformat,
    two of them on adjacent lines of the same function."""
    kept, duplicates = _dedup([a_finding(line=48), a_finding(line=49)])

    assert len(kept) == 1
    assert duplicates == 1


def test_findings_far_apart_in_one_file_both_survive():
    kept, _ = _dedup([a_finding(line=10), a_finding(line=90)])

    assert len(kept) == 2
