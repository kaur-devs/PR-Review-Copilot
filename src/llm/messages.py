"""The shapes a chat completion arrives in, and the ones we send back."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from src.llm.errors import InvalidResponse

logger = logging.getLogger(__name__)

# Why it stopped. "tool_calls" means it wants something run first.
FINISH_STOP = "stop"
FINISH_TOOL_CALLS = "tool_calls"


@dataclass(frozen=True)
class ToolCall:
    """One request from the model to run a tool.

    The arguments arrive as a JSON string rather than an object, and the
    model does sometimes get that string wrong, so parsing is kept separate
    from reading the response. A badly formed call should be answered with
    an error the model can read, not crash the loop.
    """

    id: str
    name: str
    arguments: str

    def parsed(self) -> dict:
        try:
            value = json.loads(self.arguments or "{}")
        except json.JSONDecodeError as error:
            raise InvalidResponse(
                f"{self.name} was called with arguments that are not JSON: {error}"
            ) from error
        if not isinstance(value, dict):
            raise InvalidResponse(f"{self.name} was called with {type(value).__name__}")
        return value

    def as_wire(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    attempts: int = 1
    finish_reason: str = FINISH_STOP
    tool_calls: tuple[ToolCall, ...] = ()

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def as_json(self) -> dict:
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as error:
            raise InvalidResponse(
                f"the model did not return valid JSON: {error}"
            ) from error

    def as_message(self) -> dict:
        """The assistant turn, ready to append to the conversation.

        A tool result is only accepted if the assistant message that asked
        for it is already in the history, so the loop appends this before
        any answer it produces.
        """
        message: dict = {"role": "assistant", "content": self.text or None}
        if self.tool_calls:
            message["tool_calls"] = [call.as_wire() for call in self.tool_calls]
        return message


def tool_result_message(call: ToolCall, content: str) -> dict:
    """What a tool returned, addressed to the call that asked for it."""
    return {"role": "tool", "tool_call_id": call.id, "content": content}


def _read_tool_calls(message: dict) -> tuple[ToolCall, ...]:
    """Every tool the model asked for, not just the first.

    A model may ask for several at once, which is the whole point of
    allowing it: three lookups in one turn cost one prompt instead of
    three. An entry we cannot read is dropped rather than raised on, so
    one malformed call does not throw away the ones beside it.
    """
    raw = message.get("tool_calls") or []
    calls: list[ToolCall] = []

    for entry in raw:
        function = (entry or {}).get("function") or {}
        name = function.get("name")
        if not name:
            logger.warning("ignoring a tool call with no name: %s", entry)
            continue
        calls.append(
            ToolCall(
                id=str(entry.get("id") or ""),
                name=str(name),
                arguments=function.get("arguments") or "{}",
            )
        )

    return tuple(calls)
