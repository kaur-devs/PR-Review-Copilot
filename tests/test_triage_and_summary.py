"""Who gets woken, and what the reviewer reads first."""

import json

import httpx
import pytest

from src.agents.prompts import ROBUST, WRONG
from src.agents.summary import build_user_prompt, render, summarise
from src.agents.triage import risk_notes, specialists_for
from src.classification.kinds import Classification, RULE_BASED
from src.diff.parse import parse_patch
from src.github.review import build_review_body
from src.review.findings import Finding
from src.review.provider import LLMProvider, quota
from tests.fakes import make_patch


@pytest.fixture(autouse=True)
def reset_quota():
    quota.reset()
    yield
    quota.reset()


def labelled(**paths):
    return {
        path: Classification(path, kind, RULE_BASED) for path, kind in paths.items()
    }


def a_finding(severity="high", line=1, message="charge breaks its caller"):
    return Finding(
        file="app/billing.py", line=line, severity=severity,
        category="correctness", message=message,
        rationale="app/orders.py:4", confidence=0.85,
    )


def a_diff():
    return parse_patch(
        make_patch(["def charge(a):"], ["def charge(a, c):"]), "app/billing.py"
    )


# --------------------------------------------------------------- triage

def test_a_documentation_change_wakes_nobody():
    """The cheapest review is the one that never reaches a model."""
    assert specialists_for(labelled(**{"README.md": "docs"})) == ()


def test_configuration_only_wakes_the_correctness_reviewer():
    """A secret in plain text is its problem. There is no error handling
    in a settings file to get wrong."""
    assert specialists_for(labelled(**{"app.yml": "config"})) == (WRONG,)


def test_a_test_change_wakes_the_robustness_reviewer():
    assert specialists_for(labelled(**{"test_a.py": "test"})) == (ROBUST,)


def test_a_logic_change_wakes_both():
    assert specialists_for(labelled(**{"a.py": "logic"})) == (WRONG, ROBUST)


def test_one_logic_file_among_documentation_still_earns_a_full_review():
    chosen = specialists_for(labelled(**{"README.md": "docs", "a.py": "logic"}))

    assert chosen == (WRONG, ROBUST)


def test_an_unknown_kind_wakes_everyone():
    """Erring towards reviewing is the safe direction for an unknown."""
    assert specialists_for(labelled(**{"a.weird": "something_new"})) == (WRONG, ROBUST)


def test_triage_costs_no_model_call():
    """It reads labels that src/classification already paid for."""
    before = quota.requests

    specialists_for(labelled(**{"a.py": "logic"}))

    assert quota.requests == before


def test_risk_notes_name_what_the_change_touches():
    notes = risk_notes(labelled(**{"m.py": "schema", "a.py": "logic"}))

    assert "changes the database schema" in notes
    assert "changes behaviour with no test touched" in notes


def test_a_change_with_a_test_is_not_flagged_as_untested():
    notes = risk_notes(labelled(**{"a.py": "logic", "test_a.py": "test"}))

    assert "changes behaviour with no test touched" not in notes


# -------------------------------------------------------------- summary

def test_the_summary_says_what_changed_and_where_to_look():
    body = render(
        "Reorders the callbacks passed to streamChat.",
        ["the hook that calls it was not updated"],
        [a_finding()],
        {},
    )

    assert body.startswith("Reorders the callbacks")
    assert "**Worth a look**" in body
    assert "- the hook that calls it was not updated" in body
    assert "**1 comment(s) below**: 1 high." in body


def test_a_clean_review_says_so_rather_than_going_silent():
    """Silence reads as a broken bot."""
    body = render("Adds a bulk delete helper.", [], [], {})

    assert "**No issues found.**" in body


def test_what_was_not_reviewed_is_admitted():
    body = render("A change.", [], [], {"package-lock.json": "generated"})

    assert "Not reviewed: 1 file(s)" in body
    assert "generated" in body


async def test_one_call_produces_the_summary():
    answer = {
        "what_changed": "Reorders the streamChat callbacks.",
        "risks": ["useChat.jsx passes them positionally"],
    }

    def handle(request):
        return httpx.Response(200, json={
            "model": "m",
            "choices": [{"message": {"content": json.dumps(answer)}}],
            "usage": {"prompt_tokens": 300, "completion_tokens": 60},
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    async with LLMProvider(api_key="k", client=client) as model:
        body = await summarise(model, [a_diff()], [a_finding()], labelled(**{"app/billing.py": "logic"}), {})

    assert "Reorders the streamChat callbacks." in body
    assert "useChat.jsx passes them positionally" in body
    assert quota.requests == 1


async def test_a_failed_summary_does_not_stop_the_review():
    """A review with inline comments and no summary is worth posting."""
    def handle(request):
        return httpx.Response(500, json={"error": {"message": "no"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    async with LLMProvider(api_key="k", client=client) as model:
        body = await summarise(model, [a_diff()], [a_finding()], {}, {})

    assert body == ""


async def test_no_reviewable_files_means_no_call():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: None))
    async with LLMProvider(api_key="k", client=client) as model:
        assert await summarise(model, [], [], {}, {}) == ""
    assert quota.requests == 0


def test_the_prompt_tells_the_model_not_to_repeat_the_comments():
    prompt = build_user_prompt(
        [a_diff()], [a_finding()], labelled(**{"app/billing.py": "logic"}), {}
    )

    assert "do not repeat these" in prompt
    assert "charge breaks its caller" in prompt


# ------------------------------------------------------ the posted body

def test_the_summary_replaces_the_terse_counts():
    body = build_review_body([a_finding()], 0, summary="A clear explanation.")

    assert body == "A clear explanation."


def test_without_a_summary_the_old_body_is_unchanged():
    body = build_review_body([a_finding()], 0)

    assert "Found 1 issue(s): 1 high." in body


def test_held_back_findings_are_still_admitted_under_a_summary():
    body = build_review_body([a_finding()], 3, summary="A clear explanation.")

    assert "A clear explanation." in body
    assert "3 further finding(s)" in body
