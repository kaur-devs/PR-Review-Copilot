from src.filter.gate import MINIMUM_CONFIDENCE, apply_gate
from src.review.findings import Finding


def make(severity="high", confidence=0.9, line=10):
    return Finding(
        file="app.py", line=line, severity=severity, category="correctness",
        message="m", rationale="r", confidence=confidence,
    )


def test_a_confident_high_severity_finding_is_posted():
    result = apply_gate([make("high", 0.9)])

    assert len(result.post) == 1
    assert result.hold == []


def test_a_confident_medium_severity_finding_is_posted():
    assert len(apply_gate([make("medium", 0.8)]).post) == 1


def test_low_severity_is_stored_but_not_posted():
    """The PRD says low severity is kept and not shown unless asked for."""
    result = apply_gate([make("low", 0.99)])

    assert result.post == []
    assert len(result.hold) == 1
    assert "low severity" in result.hold[0][1]


def test_an_unconfident_finding_is_held_back_whatever_its_severity():
    result = apply_gate([make("high", 0.3)])

    assert result.post == []
    assert "below" in result.hold[0][1]


def test_confidence_is_checked_before_severity():
    """An unconfident low-severity finding should say why it really failed."""
    result = apply_gate([make("low", 0.2)])

    assert "confidence" in result.hold[0][1]


def test_a_finding_exactly_on_the_threshold_is_posted():
    assert len(apply_gate([make("high", MINIMUM_CONFIDENCE)]).post) == 1


def test_the_threshold_can_be_moved_for_an_experiment():
    findings = [make("high", 0.5)]

    assert apply_gate(findings).post == []
    assert len(apply_gate(findings, minimum_confidence=0.4).post) == 1


def test_a_mixed_batch_is_split_correctly():
    result = apply_gate([
        make("high", 0.95, line=1),
        make("medium", 0.80, line=2),
        make("low", 0.99, line=3),
        make("high", 0.20, line=4),
    ])

    assert [f.line for f in result.post] == [1, 2]
    assert [f.line for f, _ in result.hold] == [3, 4]


def test_the_reasons_are_counted():
    result = apply_gate([make("low", 0.9), make("low", 0.9), make("high", 0.1)])

    counts = result.held_reasons
    assert counts["low severity is not posted"] == 2


def test_an_empty_list_is_fine():
    result = apply_gate([])

    assert result.post == []
    assert result.hold == []
