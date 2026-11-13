"""What can go wrong when talking to a model.

Each one is separate because the caller does something different about it:
a rate limit is waited out, a spent daily budget is not, and a hallucinated
tool name is told back to the model.
"""

from __future__ import annotations

import re

class LLMError(Exception):
    pass


class LLMNotConfigured(LLMError):
    pass


class RateLimited(LLMError):
    pass


class DailyQuotaExhausted(LLMError):
    """The day's tokens are gone for this model.

    Separate from RateLimited because waiting does not help: the window is
    hours wide, every retry is refused, and the clamp on how long we are
    willing to wait turns that into four attempts that each look like a
    transient hiccup. Raised at once so the caller can say so plainly.
    """


class InvalidResponse(LLMError):
    pass


class UnknownTool(LLMError):
    """The model called a tool that was not offered to it.

    Groq validates tool names before running anything, so a hallucinated
    name comes back as a 400 and the call never reaches our toolbox. That
    makes it invisible to the ToolFailure path, which exists for exactly
    this kind of mistake in an argument. Raised separately so the loop can
    tell the model what it did wrong instead of abandoning the run.
    """

    def __init__(self, message: str, *, name: str = "") -> None:
        super().__init__(message)
        self.name = name


class BadToolArguments(LLMError):
    """The model called a real tool with arguments the schema refuses.

    Rejected by the provider before our toolbox sees it, exactly like an
    invented name, so the ToolFailure path never runs and the turn is lost.
    Raised separately so the loop can name the tool and what it omitted,
    which is the only thing that makes the next turn different.
    """

    def __init__(self, message: str, *, name: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.name = name
        self.detail = detail


# What the provider calls a rejected tool name, and how it is wrapped.
TOOL_USE_FAILED = "tool_use_failed"
UNKNOWN_TOOL_PATTERN = re.compile(r"attempted to call tool '([^']+)'")
BAD_ARGUMENTS_PATTERN = re.compile(
    r"parameters for tool '?([\w.-]+)'? did not match schema:?\s*([^\"\n]*)"
)


def _unknown_tool(body: str) -> UnknownTool | None:
    """An UnknownTool if this 400 names a tool that does not exist.

    The name has to be there. "tool_use_failed" also covers the model
    emitting a call when none was offered — "Tool choice is none, but model
    called a tool" — which names nothing and is not something the model can
    be told to stop doing. Treating that as an unknown tool produced a
    correction about a tool called '', which helped nobody.
    """
    if TOOL_USE_FAILED not in body:
        return None

    match = UNKNOWN_TOOL_PATTERN.search(body)
    if match is None:
        return None

    name = match.group(1)
    return UnknownTool(
        f"the model called a tool that does not exist: {name!r}", name=name
    )


def _bad_tool_arguments(body: str) -> BadToolArguments | None:
    """A BadToolArguments if this 400 rejects a real tool's arguments.

    Seen live as `parameters for tool read_file did not match schema:
    errors: [missing properties: 'path']`, twice in one review, each time
    costing a whole turn.
    """
    if TOOL_USE_FAILED not in body:
        return None

    match = BAD_ARGUMENTS_PATTERN.search(body)
    if match is None:
        return None

    name, detail = match.group(1), match.group(2).strip()
    return BadToolArguments(
        f"the model called {name!r} with arguments the schema refuses: {detail}",
        name=name,
        detail=detail,
    )
