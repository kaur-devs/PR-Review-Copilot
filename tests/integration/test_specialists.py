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
from src.agents.specialists import (
    MAX_CANDIDATES_PER_FILE,
    MAX_CANDIDATES_TOTAL,
    review,
)
from src.agents.tools import Toolbox
from src.context.bundle import ContextBundle
from src.context.snapshot import Snapshot
from src.diff.parse import parse_patch
from src.llm.provider import LLMProvider, quota
from tests.fakes import make_patch


@pytest.fixture(autouse=True)
def reset_quota():
    quota.reset()
    yield
    quota.reset()


BEFORE = ["def charge(amount):", "    return amount"]
AFTER = [
    "def charge(amount, currency):",
    "    rate = rates[currency]",
    "    if rate is None:",
    "        raise KeyError(currency)",
    "    total = amount * rate",
    "    audit(total)",
    "    settle(total)",
    "    notify(total)",
    "    record(total)",
    "    return total",
]


BILLING = "app/billing.py"


def a_diff():
    return [parse_patch(make_patch(BEFORE, AFTER), BILLING)]


def on(which, path=BILLING):
    """How a run is keyed now that one agent covers one file."""
    return f"{which}:{path}"


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
        json.dumps({"findings": [finding(line=1)]}),
        json.dumps({"findings": [
            finding(line=9, category="error_handling", severity="medium")
        ]}),
    )

    outcome = await run(client, box)

    assert len(outcome.findings) == 2
    assert set(outcome.runs) == {on(WRONG), on(ROBUST)}
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

    assert set(outcome.runs) == {on(WRONG)}
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
    assert outcome.runs[on(ROBUST)].stopped_because == "model error"


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

async def test_a_noisy_specialist_is_capped_per_file(box):
    """Distinct categories, so the cap is what trims them and not dedup."""
    many = {
        "findings": [
            finding(line=1, category="correctness"),
            finding(line=5, category="error_handling"),
            finding(line=9, category="concurrency", confidence=0.7),
            finding(line=7, category="security", confidence=0.7),
            finding(line=3, category="performance", severity="low"),
        ]
    }
    client, _ = answers(json.dumps(many), json.dumps({"findings": []}))

    outcome = await run(client, box)

    # The cap trims inside the agent, before dedup collapses what is left
    # onto one comment per line, so what it fired on is what to assert.
    assert any("over the per-file cap" in r for _, r in outcome.dropped)
    assert len(outcome.findings) <= MAX_CANDIDATES_PER_FILE


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


def test_both_prompts_say_when_to_stop():
    """Measured on two real pull requests: the correctness agent ran to
    the repeat detector both times while the robustness agent concluded.
    It had no termination rule, only a list of things to look for."""
    for which in SPECIALISTS:
        prompt = system_prompt(which)
        assert "WHEN TO STOP" in prompt
        assert "FINDING NOTHING IS A GOOD ANSWER" in prompt


def test_the_correctness_agent_is_given_an_order_not_a_checklist():
    """A flat list of unrelated things has no end. Ordered questions do:
    each is answered and dropped."""
    prompt = system_prompt(WRONG)

    assert "in order" in prompt
    assert "1. DID A CONTRACT CHANGE?" in prompt
    assert "When all\nfour are done, you are done." in prompt


def test_the_correctness_agent_is_asked_what_is_absent():
    """Review 19 missed an endpoint with no permission dependency while
    two siblings twelve lines away had one. The check existed, buried in
    question 3 behind "only ask this when the diff touches a query, a
    path, a command" — so it was answered "no" and never reached."""
    prompt = system_prompt(WRONG)

    assert "4. WAS SOMETHING LEFT OUT?" in prompt
    assert "against its siblings in the file already in front of you" in prompt
    assert "authorisation checked on one path but not a sibling" not in prompt


def test_the_correctness_agent_is_told_where_to_spend_its_lookups():
    """A flat "two or three lookups is normal" was pulling against the
    instruction to grep the whole repository for a renamed name. The
    budget belongs to question 1; the other two are answered by reading."""
    prompt = system_prompt(WRONG)

    assert "Spend your lookups on question 1" in prompt
    assert "not a lookup, it is stalling" in prompt


def test_both_prompts_say_what_each_severity_means():
    """The first posted review found a ZeroDivisionError, called it low,
    and the gate dropped it — correctly, because low is never posted.
    Nothing had ever told the model what the three words mean."""
    for which in SPECIALISTS:
        prompt = system_prompt(which)
        assert "Only \"high\" and \"medium\" are posted" in prompt
        assert "An unhandled exception on a path" in prompt
        assert '"Could crash" is not low' in prompt


def test_the_correctness_agent_is_told_the_caller_may_be_another_language():
    """find_references follows imports. A dict key read in JSX, or a
    callback handed one more argument than its handler declares, is on the
    far side of a gap imports do not cross."""
    prompt = system_prompt(WRONG)

    assert "not even in the same\n   language" in prompt
    assert "grep for the OLD name across the whole repository" in prompt


def test_both_prompts_demand_evidence():
    for which in SPECIALISTS:
        prompt = system_prompt(which)
        assert "rationale must name what you looked at" in prompt
        assert "problems you can point at in code you have actually seen" in prompt


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


# ------------------------------------------------------------- the fan-out

ORDERS_BEFORE = ["def place():", "    return charge(10)"]
ORDERS_AFTER = ["def place():", "    return charge(10, 'usd')"]


def two_diffs():
    return [
        parse_patch(make_patch(BEFORE, AFTER), BILLING),
        parse_patch(make_patch(ORDERS_BEFORE, ORDERS_AFTER), "app/orders.py"),
    ]


async def run_over(client, box, diffs, **kwargs):
    async with LLMProvider(api_key="k", client=client) as model:
        return await review(model, box, diffs, ContextBundle(), **kwargs)


async def test_every_changed_file_gets_its_own_agent(box):
    """Eighteen files in one conversation produced three tool calls and
    five candidates. One file per agent is the whole point of the change."""
    client, sent = answers(json.dumps({"findings": []}), json.dumps({"findings": []}))

    outcome = await run_over(client, box, two_diffs())

    assert set(outcome.runs) == {
        on(WRONG, BILLING),
        on(WRONG, "app/orders.py"),
        on(ROBUST, BILLING),
        on(ROBUST, "app/orders.py"),
    }
    assert len(sent) == 4


async def test_an_agent_is_shown_only_its_own_file(box):
    """A narrow scope is what makes the model dig rather than skim."""
    client, sent = answers(json.dumps({"findings": []}), json.dumps({"findings": []}))

    await run_over(client, box, two_diffs())

    prompts = [body["messages"][1]["content"] for body in sent]
    assert all(
        (BILLING in prompt) != ("app/orders.py" in prompt) for prompt in prompts
    ), "each agent should see one changed file, not both"


async def test_a_finding_against_another_changed_file_survives(box):
    """The cross-file case the fan-out exists for: an agent scoped to the
    file that renamed something must be able to report the finding against
    the file that still reads the old name. Anchoring to the scope alone
    would reject exactly these."""
    elsewhere = finding(file="app/orders.py", line=2)
    client, _ = answers(
        json.dumps({"findings": [elsewhere]}), json.dumps({"findings": []})
    )

    outcome = await run_over(client, box, two_diffs())

    assert [f.file for f in outcome.findings] == ["app/orders.py"]


async def test_the_same_problem_found_twice_becomes_one_comment(box):
    """Both specialists on one file, or two agents on either end of a
    break, legitimately arrive at the same finding."""
    client, _ = answers(
        json.dumps({"findings": [finding(confidence=0.7)]}),
        json.dumps({"findings": [finding(confidence=0.95)]}),
    )

    outcome = await run_over(client, box, a_diff())

    assert len(outcome.findings) == 1
    assert outcome.findings[0].confidence == 0.95, "the surer wording wins"


def test_the_absence_question_is_told_it_costs_no_lookups():
    """Review 20 found the missing permission dependency and lost all three
    cross-file findings it had made in review 19, on the same diff with the
    same turn and token budget. A fourth question competing for question
    one's lookups is a trade, not an addition — and question 4 needs none,
    because the siblings are in the file already in the prompt."""
    prompt = system_prompt(WRONG)

    assert "Questions 2, 3 and 4 are answered by reading" in prompt
    assert "Question 4 in particular costs nothing" in prompt
    assert "this needs no\n   lookups at all" in prompt
    assert "Spend your lookups on question 1" in prompt


def test_the_agents_are_told_a_file_can_hold_several_problems():
    """Across three runs of one identical diff, every agent reported
    exactly one finding per file — ten file-runs produced one, nine
    produced two (one from each specialist), and none ever produced
    three. MAX_CANDIDATES_PER_FILE has never bound.

    flags.py carries four planted defects and exactly two were reported
    every time. The prompt nudged towards reporting nothing in five
    separate places and never once said that independent problems are
    separate findings, so changing which question is asked only changed
    which single defect came back.
    """
    for which in SPECIALISTS:
        prompt = system_prompt(which)
        assert "That is about inventing, not about counting" in prompt
        assert "are three,\nnot one" in prompt
        assert "FINDING NOTHING IS A GOOD ANSWER" in prompt, (
            "the anti-invention bias stays: it is why precision is perfect"
        )


def test_the_last_call_asks_for_everything_not_the_worst_thing():
    from src.agents.loop import LAST_CALL

    assert "List every problem you found, not only the most serious one" in LAST_CALL
