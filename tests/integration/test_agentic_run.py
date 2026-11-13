"""The agentic entry point, driven end to end against fakes.

Written after review 15 failed in live running with
`NameError: name 'skipped' is not defined` at the moment the graph was
invoked. 625 tests passed at the time, because every other test drove the
graph's nodes directly and nothing ever called the function that assembles
the state. The assertion that matters here is simply that this returns.
"""

import pytest

from src.github import auth
from src.pipeline.agentic import review_pull_request_with_agents
from src.llm.provider import quota
from src.webhooks.events import PullRequestEvent
from tests.fakes import api_file, build_tarball, fake_github, fake_model, make_patch

FILE_LINES = ["# padding"] * 16 + [
    "def average(values):",
    "    total = sum(values)",
    "    return total / len(values)",
    "",
]

PATCH = make_patch(
    ["def average(values):", "    return sum(values) / len(values)", ""],
    FILE_LINES[16:],
    start=17,
)

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
def clean_slate():
    auth.clear_token_cache()
    quota.reset()
    yield
    auth.clear_token_cache()
    quota.reset()


def a_finding(**overrides):
    entry = {
        "file": "app/stats.py",
        "line": 19,
        "severity": "high",
        "category": "correctness",
        "message": "divides by the length of a list that can be empty",
        "rationale": "values arrives from the caller unchecked",
        "confidence": 0.9,
        "suggestion": None,
    }
    entry.update(overrides)
    return entry


async def run(*, findings):
    archive = build_tarball({"app/stats.py": "\n".join(FILE_LINES).encode()})
    client, _ = fake_github(
        [[api_file("app/stats.py", patch=PATCH, additions=2)]],
        contents={"app/stats.py": "\n".join(FILE_LINES)},
        archive=archive,
    )
    model = fake_model(
        classifications={"app/stats.py": "logic"}, findings=findings
    )
    return await review_pull_request_with_agents(
        EVENT, github_client=client, llm_client=model
    )


async def test_the_agentic_entry_point_assembles_its_state_and_returns():
    """The regression test for review 15: the graph is invoked with a state
    built from the outcome, and `skipped_files` is the name on both sides."""
    outcome = await run(findings=[a_finding()])

    assert outcome.skipped is None
    assert outcome.diffs, "the diff should have survived collection"
    assert outcome.seconds >= 0


async def test_a_pull_request_with_nothing_reviewable_never_reaches_the_graph():
    """collect_diffs returning False must stop before the state is built,
    which is the path that hid the missing name for as long as it did."""
    client, _ = fake_github([[api_file("package-lock.json", patch=PATCH)]])
    model = fake_model(classifications={}, findings=[])

    outcome = await review_pull_request_with_agents(
        EVENT, github_client=client, llm_client=model
    )

    assert outcome.skipped is not None
    assert not outcome.diffs


async def test_a_refused_suggestion_says_why():
    """Review 20 offered four suggestions and refused four, and the trace
    said only the counts. The single-pass pipeline had printed the reasons
    all along; this one lost them when the two were split, so there was no
    way to tell a malformed suggestion from a cautious gate."""
    import src.common.trace as trace

    lines: list[str] = []
    original = trace.item
    trace.item = lambda text: lines.append(text)
    try:
        await run(findings=[a_finding(suggestion="not even python (")])
    finally:
        trace.item = original

    refusals = [line for line in lines if line.startswith("refused ")]
    assert refusals, "a refused suggestion should say which line and why"
    assert "app/stats.py" in refusals[0]
