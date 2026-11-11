"""The two specialist reviewers.

The model is scripted throughout, so what is tested is the wiring: that
both run, that one failing does not lose the other, that the per-agent
cap holds, and that an agent's answer survives the same validation the
single-prompt generator uses.
"""

import json

import httpx
import pytest

from src.agents.prompts import ROBUST, SPECIALISTS, WRONG, system_prompt
from src.agents.specialists import MAX_CANDIDATES_PER_SPECIALIST, review
from src.agents.tools import Toolbox
from src.context.bundle import ContextBundle
from src.context.snapshot import Snapshot
from src.diff.parse import parse_patch
from src.review.provider import LLMProvider, quota
from tests.fakes import make_patch


@pytest.fixture(autouse=True)
def reset_quota():
    quota.reset()
    yield
    quota.reset()


BEFORE = ["def charge(amount):", "    return amount"]
AFTER = ["def charge(amount, currency):", "    return amount * rate(currency)"]


def a_diff():
    return [parse_patch(make_patch(BEFORE, AFTER), "app/billing.py")]


def finding(**over):
    entry = {
        "file": "app/billing.py",
        "line": 1,
        "severity": "high",
        "category": "correctness",
        "message": "charge gained a required parameter",
        "rationale": "app/orders.py:12 calls charge with one argument",
        "confidence": 0.85,
        "evidence_files": ["app/orders.py"],
        "suggestion": None,
    }
    entry.update(over)
    return entry


def answers(*per_specialist):
    """Script one reply per specialist, keyed by which prompt arrives."""
    sent = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        system = body["messages"][0]["content"]
        which = WRONG if "is this code wrong" in system else ROBUST
        reply = per_specialist[0] if which == WRONG else per_specialist[-1]

        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {"message": "no"}})

        return httpx.Response(200, json={
            "model": "m",
            "choices": [{"finish_reason": "stop", "message": {"content": reply}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 60},
        })

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), sent


@pytest.fixture
def box(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "billing.py").write_text("def charge(a, c):\n    return a\n")
    (tmp_path / "app" / "orders.py").write_text(
        "from app.billing import charge\n\ndef place():\n    return charge(10)\n"
    )
    return Toolbox(Snapshot(tmp_path, "o/r", "abc"))


async def run(client, box, **kwargs):
    async with LLMProvider(api_key="k", client=client) as model:
        return await review(model, box, a_diff(), ContextBundle(), **kwargs)


# --------------------------------------------------------------- both run

async def test_both_specialists_run_and_their_findings_join(box):
    client, sent = answers(
        json.dumps({"findings": [finding()]}),
        json.dumps({"findings": [finding(category="error_handling", severity="medium")]}),
    )

    outcome = await run(client, box)

    assert len(outcome.findings) == 2
    assert set(outcome.runs) == {WRONG, ROBUST}
    assert len(sent) == 2


async def test_they_get_different_prompts(box):
    client, sent = answers(json.dumps({"findings": []}), json.dumps({"findings": []}))

    await run(client, box)

    systems = [body["messages"][0]["content"] for body in sent]
    assert any("is this code wrong" in s for s in systems)
    assert any("hold up when something goes wrong" in s for s in systems)


async def test_turn_zero_already_carries_the_diff(box):
    """The agent does not start cold; the diff is in its first message."""
    client, sent = answers(json.dumps({"findings": []}), json.dumps({"findings": []}))

    await run(client, box)

    first_user = sent[0]["messages"][1]["content"]
    assert "app/billing.py" in first_user
    assert "def charge(amount, currency):" in first_user


async def test_the_tools_are_offered(box):
    client, sent = answers(json.dumps({"findings": []}), json.dumps({"findings": []}))

    await run(client, box)

    offered = {t["function"]["name"] for t in sent[0]["tools"]}
    assert "find_references" in offered
    assert "list_tests" in offered


async def test_triage_can_wake_only_one(box):
    client, sent = answers(json.dumps({"findings": [finding()]}))

    outcome = await run(client, box, only=(WRONG,))

    assert set(outcome.runs) == {WRONG}
    assert len(sent) == 1


async def test_waking_nobody_costs_nothing(box):
    client, sent = answers(json.dumps({"findings": []}))

    outcome = await run(client, box, only=())

    assert outcome.findings == []
    assert sent == []


# ----------------------------------------------------------- independence

async def test_one_specialist_failing_does_not_lose_the_other(box):
    client, _ = answers(json.dumps({"findings": [finding()]}), 400)

    outcome = await run(client, box)

    assert len(outcome.findings) == 1
    assert outcome.runs[ROBUST].stopped_because == "model error"


async def test_an_unusable_answer_is_recorded_not_raised(box):
    client, _ = answers("I think it looks fine, honestly", json.dumps({"findings": []}))

    outcome = await run(client, box)

    assert outcome.findings == []
    assert any("unusable answer" in reason for _, reason in outcome.dropped)


async def test_json_wrapped_in_a_fence_is_still_read(box):
    """Agents do not run in JSON mode, so the answer arrives as prose."""
    fenced = "```json\n" + json.dumps({"findings": [finding()]}) + "\n```"
    client, _ = answers(fenced, json.dumps({"findings": []}))

    outcome = await run(client, box)

    assert len(outcome.findings) == 1


# ------------------------------------------------------------------ caps

async def test_a_noisy_specialist_is_capped(box):
    many = {"findings": [finding() for _ in range(MAX_CANDIDATES_PER_SPECIALIST + 5)]}
    client, _ = answers(json.dumps(many), json.dumps({"findings": []}))

    outcome = await run(client, box)

    assert len(outcome.findings) == MAX_CANDIDATES_PER_SPECIALIST
    assert any("over the per-specialist cap" in r for _, r in outcome.dropped)


async def test_a_finding_on_a_line_outside_the_diff_is_dropped(box):
    client, _ = answers(
        json.dumps({"findings": [finding(line=999)]}), json.dumps({"findings": []})
    )

    outcome = await run(client, box)

    assert outcome.findings == []
    assert any("not in the diff" in reason for _, reason in outcome.dropped)


# -------------------------------------------------------------- evidence

async def test_the_agent_names_what_it_looked_at(box):
    client, _ = answers(
        json.dumps({"findings": [finding(evidence_files=["app/orders.py"])]}),
        json.dumps({"findings": []}),
    )

    outcome = await run(client, box)

    assert outcome.findings[0].evidence_files == ("app/orders.py",)


# ---------------------------------------------------------- measurement

async def test_the_outcome_reports_the_trajectory(box):
    """These are the numbers the cost model was estimated from."""
    client, _ = answers(json.dumps({"findings": []}), json.dumps({"findings": []}))

    outcome = await run(client, box)

    assert outcome.turns == 2
    assert outcome.tool_calls == 0
    assert outcome.tokens == 1120


# --------------------------------------------------------------- prompts

def test_each_specialist_is_told_to_ignore_the_other_one_s_job():
    assert "Ignore error handling, concurrency" in system_prompt(WRONG)
    assert "Ignore correctness bugs, security" in system_prompt(ROBUST)


def test_both_prompts_demand_evidence():
    for which in SPECIALISTS:
        prompt = system_prompt(which)
        assert "rationale must name what you looked at" in prompt
        assert "suspicion you have not checked" in prompt


def test_an_unknown_specialist_is_refused():
    with pytest.raises(ValueError, match="no such specialist"):
        system_prompt("security")


async def test_findings_are_attributed_to_the_agent_that_found_them(box):
    """"The security prompt found nothing on any PR" is a finding about
    the prompt, and it disappears once the lists are joined."""
    client, _ = answers(
        json.dumps({"findings": [finding()]}),
        json.dumps({"findings": []}),
    )

    outcome = await run(client, box)

    assert len(outcome.found_by[WRONG]) == 1
    assert outcome.found_by[ROBUST] == []
