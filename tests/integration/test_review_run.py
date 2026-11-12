"""The review as a whole, with nothing stored and nothing posted."""

import pytest

from src.github import auth
from src.pipeline.single_pass import review_pull_request
from src.webhooks.events import PullRequestEvent
from tests.fakes import api_file, fake_github, fake_model, make_patch

# Sixteen lines of padding put the function at 17, so the line numbers in the
# diff, the file and the finding all have to agree for the test to pass.
FILE = "\n".join(
    ["# padding"] * 16
    + [
        "def average(values):",
        "    total = sum(values)",
        "    return total / len(values)",
        "",
    ]
)

PATCH = make_patch(
    ["def average(values):", "    return sum(values) / len(values)", ""],
    [
        "def average(values):",
        "    total = sum(values)",
        "    return total / len(values)",
        "",
    ],
    start=17,
)

GOOD_SUGGESTION = "    return total / len(values) if values else 0.0"

EVENT = PullRequestEvent(
    delivery_id="d-1",
    action="opened",
    installation_id=166408152,
    repo_id=987654321,
    repo_full_name="kaur-devs/pr-review-sandbox",
    pr_number=7,
    head_sha="a" * 40,
    base_sha="b" * 40,
)


@pytest.fixture(autouse=True)
def forget_cached_tokens():
    auth.clear_token_cache()
    yield
    auth.clear_token_cache()


def finding(**overrides):
    entry = {
        "file": "app/stats.py",
        "line": 19,
        "severity": "high",
        "category": "correctness",
        "message": "divides by the length of a list that can be empty",
        "rationale": "values arrives from the caller unchecked",
        "confidence": 0.9,
        "suggestion": GOOD_SUGGESTION,
    }
    entry.update(overrides)
    return entry


async def run(*, findings, contents=None):
    pages = [[api_file("app/stats.py", patch=PATCH, additions=2)]]
    client, calls = fake_github(pages, contents=contents)
    model = fake_model(
        classifications={"app/stats.py": "logic"}, findings=findings
    )

    outcome = await review_pull_request(
        EVENT, github_client=client, llm_client=model
    )
    return outcome, calls


async def test_a_sound_suggestion_survives_the_whole_review():
    outcome, _ = await run(
        findings=[finding()], contents={"app/stats.py": FILE}
    )

    assert len(outcome.findings) == 1
    assert outcome.findings[0].suggestion == GOOD_SUGGESTION
    assert outcome.suggestions.offered == 1


async def test_the_file_is_read_at_the_reviewed_commit():
    _, calls = await run(findings=[finding()], contents={"app/stats.py": FILE})

    reads = [c for c in calls if "/contents/" in c.url.path]
    assert len(reads) == 1
    assert reads[0].url.params["ref"] == EVENT.head_sha


async def test_a_suggestion_the_model_forgot_to_indent_is_shifted_into_place():
    outcome, _ = await run(
        findings=[finding(suggestion="return total / len(values) if values else 0.0")],
        contents={"app/stats.py": FILE},
    )

    assert outcome.suggestions.offered == 1
    assert outcome.findings[0].suggestion == (
        "    return total / len(values) if values else 0.0"
    )


async def test_a_suggestion_that_will_not_compile_is_dropped_but_the_finding_stays():
    outcome, _ = await run(
        findings=[finding(suggestion="    return total / len(values")],
        contents={"app/stats.py": FILE},
    )

    assert len(outcome.findings) == 1
    assert outcome.findings[0].suggestion is None
    assert outcome.findings[0].message == finding()["message"]
    assert "stop the file parsing" in outcome.suggestions.refused[0][1]


async def test_a_file_that_cannot_be_read_drops_the_suggestion_only():
    outcome, _ = await run(findings=[finding()], contents={})

    assert len(outcome.findings) == 1
    assert outcome.findings[0].suggestion is None
    assert outcome.suggestions.offered == 0


async def test_a_low_severity_finding_costs_no_network_call():
    _, calls = await run(
        findings=[finding(severity="low")], contents={"app/stats.py": FILE}
    )

    assert [c for c in calls if "/contents/" in c.url.path] == []


async def test_a_medium_severity_finding_now_gets_a_suggestion():
    outcome, _ = await run(
        findings=[finding(severity="medium")], contents={"app/stats.py": FILE}
    )

    assert outcome.suggestions.offered == 1


async def test_no_suggestions_at_all_costs_no_network_call():
    _, calls = await run(
        findings=[finding(suggestion=None)], contents={"app/stats.py": FILE}
    )

    assert [c for c in calls if "/contents/" in c.url.path] == []


async def test_suggestions_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("SUGGESTIONS_ENABLED", "0")

    outcome, calls = await run(
        findings=[finding()], contents={"app/stats.py": FILE}
    )

    assert len(outcome.findings) == 1
    assert outcome.findings[0].suggestion is None
    assert [c for c in calls if "/contents/" in c.url.path] == []


async def test_the_single_pass_pipeline_also_writes_a_summary():
    """The summary is the feature both reference designs call a launch
    requirement and this project did not have. It must not be available
    only behind the agentic flag."""
    outcome, _ = await run(findings=[finding()], contents={"app/stats.py": FILE})

    assert "A change was made." in outcome.summary
    assert "something worth a look" in outcome.summary


async def test_the_summary_counts_what_will_post_not_what_was_found():
    """A note saying "2 comments below" above one comment is worse than
    no note, so the summary is told what survives the gate."""
    outcome, _ = await run(
        findings=[finding(severity="low"), finding(line=18)],
        contents={"app/stats.py": FILE},
    )

    assert "1 comment(s) below" in outcome.summary
