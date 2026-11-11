"""The loop that lets a model look things up before it answers.

Send the conversation; the model replies with either tool calls or an
answer; run the tools, append what they said, send again. That is the
whole idea. Everything else in this file is about making sure it stops.

An unbounded loop is the real risk in an agentic design, not the cost of
any single turn. Published agent harnesses cap at fifty steps or more
because the failure mode is a long tail: a model that keeps re-reading
the same file, politely, until the budget is gone. Four separate limits
guard against that, and the loop records what it did so the turn count
can be measured rather than assumed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.agents.tools import Toolbox
from src.review.provider import (
    Completion,
    LLMError,
    LLMProvider,
    ToolCall,
    tool_result_message,
)

logger = logging.getLogger(__name__)

MAX_TURNS = 6
MAX_TOOL_CALLS_PER_TURN = 5
MAX_TOKENS = 80_000

# How many times to ask for the conclusion before giving up. More than
# one because a model at the cap often reaches for a last lookup first.
LAST_CALL_ATTEMPTS = 2

STOPPED_ANSWERED = "answered"
STOPPED_TURN_CAP = "turn cap"
STOPPED_TOKEN_CAP = "token cap"
STOPPED_REPEATING = "no new information"
STOPPED_FAILED = "model error"

# Sent when the turns run out. Without it a run that explored for six
# turns ends with nothing to show, having paid for all of them.
LAST_CALL = (
    "You have no more lookups left. Answer now, using only what you have "
    "already seen. If you found nothing, say so."
)

ALREADY_RAN = (
    "You already ran this exact call earlier in this conversation and the "
    "result has not changed. Ask something different, or answer."
)


@dataclass
class Turn:
    """One exchange, kept so the trajectory can be read back.

    The turn count is the single assumption the whole cost model rests
    on, so the loop measures it rather than trusting an estimate.
    """

    number: int
    tools_called: tuple[str, ...]
    prompt_tokens: int
    completion_tokens: int

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class AgentRun:
    answer: str = ""
    turns: list[Turn] = field(default_factory=list)
    stopped_because: str = STOPPED_ANSWERED
    error: str | None = None

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def tool_calls(self) -> int:
        return sum(len(turn.tools_called) for turn in self.turns)

    @property
    def tokens(self) -> int:
        return sum(turn.tokens for turn in self.turns)

    @property
    def answered(self) -> bool:
        return bool(self.answer)

    def __str__(self) -> str:
        return (
            f"{self.turn_count} turns · {self.tool_calls} tool calls · "
            f"{self.tokens} tokens · {self.stopped_because}"
        )


def _fingerprint(call: ToolCall) -> str:
    """What makes two calls the same request.

    The id differs every time and the model is not asking a new question
    by renaming it, so only the name and arguments count.
    """
    return f"{call.name}({call.arguments})"


async def run_agent(
    model: LLMProvider,
    toolbox: Toolbox,
    *,
    system: str,
    user: str,
    tools: list[dict],
    max_turns: int = MAX_TURNS,
    max_tokens: int = MAX_TOKENS,
    temperature: float = 0.0,
) -> AgentRun:
    """Let the model look things up until it answers or runs out of room."""
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    run = AgentRun()
    seen: dict[str, str] = {}

    for number in range(1, max_turns + 1):
        try:
            completion = await model.complete_with_tools(
                messages, tools, temperature=temperature
            )
        except LLMError as error:
            logger.warning("agent turn %s failed: %s", number, error)
            run.stopped_because = STOPPED_FAILED
            run.error = str(error)
            return run

        run.turns.append(
            Turn(
                number=number,
                tools_called=tuple(call.name for call in completion.tool_calls),
                prompt_tokens=completion.prompt_tokens,
                completion_tokens=completion.completion_tokens,
            )
        )

        if not completion.wants_tools:
            run.answer = completion.text
            run.stopped_because = STOPPED_ANSWERED
            return run

        messages.append(completion.as_message())
        fresh = _serve(completion, toolbox, messages, seen)

        if not fresh:
            # Every call this turn repeated an earlier one. The model is
            # circling, not closing in. Ask for the answer it already has.
            run.stopped_because = STOPPED_REPEATING
            break

        if run.tokens >= max_tokens:
            run.stopped_because = STOPPED_TOKEN_CAP
            break
    else:
        run.stopped_because = STOPPED_TURN_CAP

    await _ask_for_the_answer(
        model, messages, tools, run, toolbox, seen, temperature
    )
    return run


def _serve(
    completion: Completion,
    toolbox: Toolbox,
    messages: list[dict],
    seen: dict[str, str],
) -> bool:
    """Run this turn's tools. True if any of them told us something new.

    Several calls in one turn is the point rather than an edge case: three
    lookups answered together cost one prompt instead of three. Anything
    past the per-turn cap is refused with an explanation, so the model
    learns to ask for less rather than silently losing a question.
    """
    anything_new = False

    for position, call in enumerate(completion.tool_calls):
        if position >= MAX_TOOL_CALLS_PER_TURN:
            messages.append(
                tool_result_message(
                    call,
                    f"Not run: at most {MAX_TOOL_CALLS_PER_TURN} lookups per "
                    f"turn. Ask for this one next turn if you still need it.",
                )
            )
            continue

        key = _fingerprint(call)
        if key in seen:
            messages.append(tool_result_message(call, ALREADY_RAN))
            continue

        result = toolbox.run(call)
        seen[key] = result
        anything_new = True
        messages.append(tool_result_message(call, result))

    return anything_new


async def _ask_for_the_answer(
    model: LLMProvider,
    messages: list[dict],
    tools: list[dict],
    run: AgentRun,
    toolbox: Toolbox,
    seen: dict[str, str],
    temperature: float,
) -> None:
    """One last turn, asking for the conclusion rather than more lookups.

    A run that stops at a cap has paid for everything it looked at, so
    not asking for the conclusion wastes the whole run.

    The tools stay in the request even though no more are wanted. Sending
    an empty list makes the provider infer `tool_choice: none`, and a
    model that then emits a tool call anyway — which gpt-oss does — is
    refused outright:

        400 Tool choice is none, but model called a tool

    That turned the safety net into the thing that lost the run. Instead
    the tools stay offered and the instruction asks for an answer; if the
    model reaches for one more lookup it is served, once, and asked
    again.
    """
    messages.append({"role": "user", "content": LAST_CALL})

    for attempt in range(1, LAST_CALL_ATTEMPTS + 1):
        try:
            completion = await model.complete_with_tools(
                messages, tools, temperature=temperature
            )
        except LLMError as error:
            logger.warning("could not get a final answer: %s", error)
            run.error = str(error)
            return

        run.turns.append(
            Turn(
                number=len(run.turns) + 1,
                tools_called=tuple(call.name for call in completion.tool_calls),
                prompt_tokens=completion.prompt_tokens,
                completion_tokens=completion.completion_tokens,
            )
        )

        if completion.text:
            run.answer = completion.text
            return

        if not completion.wants_tools or attempt == LAST_CALL_ATTEMPTS:
            logger.warning("the agent would not answer after %s attempt(s)", attempt)
            return

        messages.append(completion.as_message())
        _serve(completion, toolbox, messages, seen)
        messages.append({"role": "user", "content": LAST_CALL})
