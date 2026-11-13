"""The tool-calling loop.

Every test scripts the model's replies, so none of this touches a
network. What is being tested is not whether a model is clever but
whether the loop always stops, always appends the conversation in the
order the API demands, and always reports honestly what it did.
"""

import json
import pathlib

import httpx
import pytest

from src.agents.loop import (
    lookup_notes,
    LAST_CALL_ATTEMPTS,
    MAX_TOOL_CALLS_PER_TURN,
    STOPPED_ANSWERED,
    STOPPED_FAILED,
    STOPPED_REPEATING,
    STOPPED_TURN_CAP,
    run_agent,
)
from src.agents.tools import SCHEMAS, Toolbox
from src.context.snapshot import Snapshot
from src.llm.provider import LLMProvider, quota


@pytest.fixture(autouse=True)
def reset_quota():
    quota.reset()
    yield
    quota.reset()


def says(text: str) -> dict:
    return {
        "model": "m",
        "choices": [{"finish_reason": "stop", "message": {"content": text}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def calls(*wanted) -> dict:
    """A reply asking for tools. Each item is (name, arguments-dict)."""
    return {
        "model": "m",
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{n}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                    for n, (name, args) in enumerate(wanted)
                ],
            },
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def scripted(*replies):
    """A model that returns these replies in order, then repeats the last."""
    sent: list[dict] = []
    remaining = list(replies)

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        reply = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {"message": "no"}})
        return httpx.Response(200, json=reply)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), sent


@pytest.fixture
def box(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "models.py").write_text("def get_user(i):\n    return i\n")
    (tmp_path / "app" / "service.py").write_text(
        "from app.models import get_user\n\ndef e(i):\n    return get_user(i)\n"
    )
    return Toolbox(Snapshot(tmp_path, "o/r", "abc"))


async def run(client, box, **kwargs):
    async with LLMProvider(api_key="k", client=client) as model:
        return await run_agent(
            model, box, system="review this", user="a diff",
            tools=SCHEMAS, **kwargs
        )


# ------------------------------------------------------------ the happy path

async def test_an_answer_on_the_first_turn_ends_it(box):
    client, sent = scripted(says('{"findings": []}'))

    result = await run(client, box)

    assert result.answer == '{"findings": []}'
    assert result.stopped_because == STOPPED_ANSWERED
    assert result.turn_count == 1
    assert len(sent) == 1


async def test_a_tool_is_run_and_the_model_answers_next_turn(box):
    client, sent = scripted(
        calls(("read_file", {"path": "app/models.py"})),
        says("found it"),
    )

    result = await run(client, box)

    assert result.answer == "found it"
    assert result.turn_count == 2
    assert result.tool_calls == 1
    assert box.calls == ["read_file"]


async def test_the_tool_result_reaches_the_model(box):
    client, sent = scripted(
        calls(("read_file", {"path": "app/models.py"})),
        says("done"),
    )

    await run(client, box)

    second = sent[1]["messages"]
    tool_turn = [m for m in second if m["role"] == "tool"][0]
    assert "def get_user" in tool_turn["content"]


async def test_the_assistant_turn_is_appended_before_the_tool_result(box):
    """The API refuses a tool result whose assistant message is missing."""
    client, sent = scripted(
        calls(("read_file", {"path": "app/models.py"})),
        says("done"),
    )

    await run(client, box)

    roles = [m["role"] for m in sent[1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert sent[1]["messages"][2]["tool_calls"][0]["id"] == "call_0"
    assert sent[1]["messages"][3]["tool_call_id"] == "call_0"


# -------------------------------------------------------------- parallelism

async def test_several_tools_in_one_turn_all_run(box):
    """Three lookups answered together cost one prompt, not three."""
    client, sent = scripted(
        calls(
            ("read_file", {"path": "app/models.py"}),
            ("grep", {"pattern": "get_user"}),
            ("find_references", {"symbol": "get_user"}),
        ),
        says("done"),
    )

    result = await run(client, box)

    assert result.turn_count == 2
    assert result.tool_calls == 3
    assert box.calls == ["read_file", "grep", "find_references"]
    assert len([m for m in sent[1]["messages"] if m["role"] == "tool"]) == 3


async def test_too_many_tools_in_one_turn_are_refused_with_a_reason(box):
    client, sent = scripted(
        calls(*[("grep", {"pattern": f"p{n}"}) for n in range(MAX_TOOL_CALLS_PER_TURN + 3)]),
        says("done"),
    )

    await run(client, box)

    results = [m["content"] for m in sent[1]["messages"] if m["role"] == "tool"]
    refused = [r for r in results if r.startswith("Not run:")]
    assert len(refused) == 3
    assert "next turn" in refused[0]


# -------------------------------------------------------------- the stoppers

async def test_the_turn_cap_still_produces_an_answer(box):
    """Six turns of looking, then nothing, would waste the whole run."""
    client, sent = scripted(
        calls(("grep", {"pattern": "unique-0"})),
        calls(("grep", {"pattern": "unique-1"})),
        calls(("grep", {"pattern": "unique-2"})),
        says("here is what I found"),
    )

    result = await run(client, box, max_turns=3)

    assert result.stopped_because == STOPPED_TURN_CAP
    assert result.answer == "here is what I found"
    assert "You have no more lookups" in sent[-1]["messages"][-1]["content"]
    # Shown lookups it may not use, gpt-oss reaches for one and never answers.
    assert "tools" not in sent[-1]


async def test_repeating_the_same_call_stops_the_loop(box):
    """A model re-reading the same file is circling, not closing in."""
    client, _ = scripted(
        calls(("grep", {"pattern": "same"})),
        calls(("grep", {"pattern": "same"})),
        says("nothing more to add"),
    )

    result = await run(client, box, max_turns=6)

    assert result.stopped_because == STOPPED_REPEATING
    assert result.answer == "nothing more to add"
    assert box.calls == ["grep"]


async def test_a_repeat_alongside_something_new_keeps_going(box):
    """Only a turn that is entirely repeats means the model is stuck."""
    client, _ = scripted(
        calls(("grep", {"pattern": "same"})),
        calls(("grep", {"pattern": "same"}), ("grep", {"pattern": "different"})),
        says("done"),
    )

    result = await run(client, box, max_turns=6)

    assert result.stopped_because == STOPPED_ANSWERED
    assert box.calls == ["grep", "grep"]


async def test_the_token_cap_stops_it(box):
    """One lookup is enough to blow a 50-token budget, so the second
    request the model sees is the demand for an answer, not another turn."""
    client, sent = scripted(
        calls(("grep", {"pattern": "a"})),
        says("stopping"),
    )

    result = await run(client, box, max_tokens=50)

    assert result.stopped_because == "token cap"
    assert result.answer == "stopping"
    assert len(sent) == 2
    assert "You have no more lookups" in sent[-1]["messages"][-1]["content"]


async def test_a_model_failure_is_reported_not_raised(box):
    client, _ = scripted(400)

    result = await run(client, box)

    assert result.stopped_because == STOPPED_FAILED
    assert not result.answered
    assert result.error


# ------------------------------------------------------------- the trajectory

async def test_the_run_reports_what_it_actually_did(box):
    """Turn count is the assumption the whole cost model rests on, so it
    is measured rather than estimated."""
    client, _ = scripted(
        calls(("grep", {"pattern": "a"}), ("grep", {"pattern": "b"})),
        calls(("read_file", {"path": "app/models.py"})),
        says("done"),
    )

    result = await run(client, box)

    assert result.turn_count == 3
    assert result.tool_calls == 3
    assert result.tokens == 360
    assert [turn.tools_called for turn in result.turns] == [
        ("grep", "grep"), ("read_file",), (),
    ]
    assert "3 turns · 3 tool calls · 360 tokens · answered" == str(result)


async def test_a_failing_tool_does_not_stop_the_run(box):
    client, sent = scripted(
        calls(("read_file", {"path": "nope.py"})),
        says("recovered"),
    )

    result = await run(client, box)

    assert result.answer == "recovered"
    tool_turn = [m for m in sent[1]["messages"] if m["role"] == "tool"][0]
    assert "not in this repository" in tool_turn["content"]


async def test_a_last_lookup_on_the_final_turn_is_served_not_refused(box):
    """Seen on the first real run: at the cap, gpt-oss reaches for one
    more tool instead of answering.

    Refusing it loses the answer, so the call is run and the question is
    put again. Withholding the tools makes this rare rather than
    impossible — the model can still emit a call nothing offered.
    """
    client, sent = scripted(
        calls(("grep", {"pattern": "a"})),
        calls(("grep", {"pattern": "b"})),
        calls(("read_file", {"path": "app/models.py"})),
        says("now I can answer"),
    )

    result = await run(client, box, max_turns=1)

    assert result.stopped_because == STOPPED_TURN_CAP
    assert result.answer == "now I can answer"
    # Turn one's lookup, and the one it reached for at the cap.
    served = [m for m in sent[-1]["messages"] if m["role"] == "tool"]
    assert len(served) == 2


async def test_an_agent_that_will_not_answer_is_given_up_on(box):
    """Two attempts, then stop. A model looping on tool calls at the cap
    must not loop forever."""
    client, sent = scripted(calls(("grep", {"pattern": "same"})))

    result = await run(client, box, max_turns=1)

    assert not result.answered
    assert len(sent) <= 2 + LAST_CALL_ATTEMPTS


# --------------------------------------------- a tool name that does not exist

TOOL_USE_FAILED = {
    "error": {
        "message": (
            "Tool call validation failed: attempted to call tool 'search' "
            "which was not in request.tools"
        ),
        "type": "invalid_request_error",
        "code": "tool_use_failed",
    }
}


def rejects_the_tool_name() -> httpx.Response:
    return httpx.Response(400, json=TOOL_USE_FAILED)


def scripted_with(*replies):
    """Like scripted, but an httpx.Response in the list is returned as-is."""
    sent: list[dict] = []
    remaining = list(replies)

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        reply = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), sent


async def test_an_invented_tool_name_is_corrected_not_fatal(box):
    """Seen on the first real run against a pull request: the model called
    a tool named 'search', which does not exist. Groq checks names before
    running anything, so the whole request is refused and the call never
    reaches the toolbox — the path that handles a bad argument cannot help.
    Abandoning the run there threw away a turn that had already read code.
    """
    client, sent = scripted_with(
        rejects_the_tool_name(),
        says('{"findings": [{"file": "a.py"}]}'),
    )

    result = await run(client, box)

    assert result.answer == '{"findings": [{"file": "a.py"}]}'
    assert result.stopped_because == STOPPED_ANSWERED

    correction = sent[-1]["messages"][-1]
    assert correction["role"] == "user"
    assert "does not exist" in correction["content"]
    # Naming what does exist is what makes the next turn different.
    for name in ("read_file", "grep", "find_references"):
        assert name in correction["content"]


async def test_a_model_that_keeps_inventing_tool_names_is_given_up_on(box):
    client, _ = scripted_with(rejects_the_tool_name())

    result = await run(client, box)

    assert result.stopped_because == STOPPED_FAILED
    assert "search" in result.error


async def test_a_failed_turn_keeps_what_earlier_turns_found(box):
    """A transient error on turn three must not discard turns one and two.
    The run has already been paid for, and the last call can still read a
    conclusion out of what is sitting in the conversation.
    """
    client, sent = scripted_with(
        calls(("read_file", {"path": "app/models.py"})),
        httpx.Response(400, json={"error": {"message": "something else"}}),
        says("a conclusion from what I read"),
    )

    result = await run(client, box)

    assert result.answer == "a conclusion from what I read"
    assert result.stopped_because == STOPPED_FAILED
    # What it read on turn one survived the failure, either as a tool
    # message or pasted into a fresh prompt as a note.
    last = sent[-1]["messages"]
    assert any(m["role"] == "tool" for m in last) or "WHAT YOU LOOKED UP" in last[-1]["content"]


async def test_the_last_call_offers_no_tools(box):
    """Told it has no lookups left but shown five of them, gpt-oss reaches
    for one anyway and the run ends with nothing. Withholding them leaves
    answering as the only move."""
    client, sent = scripted(
        calls(("grep", {"pattern": "x"})),
        says("done"),
    )

    await run(client, box, max_turns=1)

    assert "tools" in sent[0]
    assert "tools" not in sent[-1]


async def test_the_last_call_falls_back_to_offering_tools(box):
    """The model can emit a tool call even when none was offered, and the
    provider then refuses the request. That must not be the end of it."""
    client, sent = scripted_with(
        calls(("grep", {"pattern": "x"})),
        # The fresh context is tried first and is refused the same way.
        rejects_the_tool_name(),
        rejects_the_tool_name(),
        says("answered on the retry"),
    )

    result = await run(client, box, max_turns=1)

    assert result.answer == "answered on the retry"
    offered_again = [body for body in sent[1:] if body.get("tools")]
    assert offered_again, "after a refusal the tools go back on the table"



# ------------------------------------------------- the last call for an answer

async def test_the_final_turn_demands_a_json_object(box):
    """A reply that must be a JSON object cannot be a tool call.

    Withholding the tools is not enough on its own: gpt-oss emits a call
    anyway and the provider refuses the request with "Tool choice is none,
    but model called a tool". In one 36-agent review that happened on every
    final turn, and two agents returned nothing after more than twenty
    thousand tokens each.
    """
    client, sent = scripted(
        calls(("read_file", {"path": "app/models.py"})),
        says(json.dumps({"findings": []})),
    )

    await run(client, box, max_turns=1)

    final = sent[-1]
    assert final["response_format"] == {"type": "json_object"}
    assert not final.get("tools"), "the final turn offers no tools"


async def test_json_mode_is_never_asked_for_while_tools_are_offered(box):
    """Providers disagree about the combination, and a refusal mid-loop
    throws away every turn already paid for."""
    client, sent = scripted(
        calls(("read_file", {"path": "app/models.py"})),
        says(json.dumps({"findings": []})),
    )

    await run(client, box, max_turns=2)

    offered = [body for body in sent if body.get("tools")]
    assert offered, "the loop should have offered tools at least once"
    assert all("response_format" not in body for body in offered)


async def test_the_answer_is_asked_for_in_a_conversation_with_no_tool_calls():
    """gpt-oss continues a tool-calling conversation by calling a tool,
    whatever it is told and whatever is offered. Review 18 lost whole
    agents to it after they had spent twenty thousand tokens reading.

    So the conclusion is asked for in a fresh conversation: same system
    prompt, same task, and the lookups pasted in as notes. There is no
    tool call in it to continue.
    """
    client, sent = scripted(
        calls(("grep", {"pattern": "get_user"})),
        says(json.dumps({"findings": []})),
    )

    async with LLMProvider(api_key="k", client=client) as model:
        box = Toolbox(Snapshot(pathlib.Path("."), "o/r", "abc"))
        result = await run_agent(
            model, box, system="review this", user="a diff",
            tools=SCHEMAS, max_turns=1,
        )

    assert result.answered

    final = sent[-1]
    assert not final.get("tools"), "no tools, so there is nothing to reach for"
    assert final["response_format"] == {"type": "json_object"}

    roles = {message["role"] for message in final["messages"]}
    assert roles == {"system", "user"}, "no assistant tool calls to continue"

    prompt = final["messages"][-1]["content"]
    assert "a diff" in prompt, "the original task is restated"
    assert "WHAT YOU LOOKED UP ALREADY" in prompt
    assert "get_user" in prompt, "the lookup survives as a note"


def test_lookup_notes_pair_each_result_with_what_asked_for_it():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "a", "type": "function",
                 "function": {"name": "grep", "arguments": '{"pattern":"x"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "two matches"},
    ]

    notes = lookup_notes(messages)

    assert notes == ['grep({"pattern":"x"})\ntwo matches']
