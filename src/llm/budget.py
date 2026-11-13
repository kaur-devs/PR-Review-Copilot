"""Token budgets, and what a review has spent.

The provider counts tokens per model id per minute, so this reserves
before sending rather than reporting after the fact. Two requests that
each ask "is there room?" and both get told yes will both be refused.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from src.llm.settings import DEFAULT_MODEL, MAX_WAIT_SECONDS

logger = logging.getLogger(__name__)

# Free tiers cap tokens per minute, so we wait rather than be refused.
HEADER_REMAINING_TOKENS = "x-ratelimit-remaining-tokens"
HEADER_RESET_TOKENS = "x-ratelimit-reset-tokens"
HEADER_LIMIT_TOKENS = "x-ratelimit-limit-tokens"

# Roughly four characters to a token. Good enough to decide whether to wait.
CHARACTERS_PER_TOKEN = 4

# The answer is charged too, so reserve for it or every estimate runs under.
COMPLETION_ALLOWANCE = 1_200

# A daily budget does not refill in ninety seconds, so retrying it is silence.
DAILY_LIMIT_MARKER = "tokens per day"


def parse_duration(value: str | None) -> float:
    """Read Groq's duration format: "7.66s", "2m59.56s", "1h3m".

    Returns 0 when it cannot be read, which means do not wait.
    """
    if not value:
        return 0.0

    total = 0.0
    number = ""
    for character in value.strip():
        if character.isdigit() or character == ".":
            number += character
            continue
        if not number:
            continue
        amount = float(number)
        number = ""
        if character == "h":
            total += amount * 3600
        elif character == "m":
            total += amount * 60
        elif character == "s":
            total += amount

    if number:
        total += float(number)

    return total


@dataclass
class RateLimitState:
    """The token budget for one minute, as far as we can tell.

    This has to *reserve*, not just report. The specialists run
    concurrently against one budget, and a limiter that only reads the
    last response lets both of them look, both see the same headroom, and
    both send — so the pair overshoots and at least one is refused. The
    numbers from the first real runs: two requests of about 4,100 and
    3,700 tokens against a budget of 8,000, deciding independently.

    So admission is serialized. A caller waits for room, subtracts what it
    is about to spend, and only then sends. Headers correct the estimate
    when the answer comes back, but may never raise it above what we have
    already promised away.
    """

    remaining_tokens: int | None = None
    limit_tokens: int | None = None
    reset_in: float = 0.0
    seen_at: float = 0.0
    _gate: asyncio.Lock | None = None

    def _lock(self) -> asyncio.Lock:
        # Built on first use: a Lock made at import time binds the wrong loop.
        if self._gate is None:
            self._gate = asyncio.Lock()
        return self._gate

    def update(self, headers) -> None:
        limit = headers.get(HEADER_LIMIT_TOKENS)
        if limit is not None:
            try:
                self.limit_tokens = int(float(limit))
            except ValueError:
                pass

        remaining = headers.get(HEADER_REMAINING_TOKENS)
        if remaining is None:
            return
        try:
            reported = int(float(remaining))
        except ValueError:
            return

        # Never above what is reserved: a reply in flight describes a stale budget.
        self.remaining_tokens = (
            reported
            if self.remaining_tokens is None
            else min(reported, self.remaining_tokens)
        )
        self.reset_in = parse_duration(headers.get(HEADER_RESET_TOKENS))
        self.seen_at = time.monotonic()

    def wait_needed(self, estimated_tokens: int) -> float:
        """How long before a request of this size is likely to fit."""
        if self.remaining_tokens is None:
            return 0.0
        if self.remaining_tokens >= estimated_tokens:
            return 0.0

        elapsed = time.monotonic() - self.seen_at
        return max(0.0, min(self.reset_in - elapsed, MAX_WAIT_SECONDS))

    async def admit(self, estimated_tokens: int) -> None:
        """Wait for room for this request, then take it off the budget.

        One caller at a time. Holding the lock across the wait is the
        point: the next caller then asks about a budget this one has
        already drawn on, instead of the one they both saw a moment ago.
        """
        async with self._lock():
            wait = self.wait_needed(estimated_tokens)
            if wait > 0:
                await _pause(
                    wait,
                    f"only {self.remaining_tokens} tokens left this minute, "
                    f"this request needs about {estimated_tokens}",
                )
                # The window turned over; a full budget is the best guess.
                self.remaining_tokens = self.limit_tokens

            if self.remaining_tokens is not None:
                self.remaining_tokens -= estimated_tokens


@dataclass
class QuotaCounter:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retries: int = 0
    waited_seconds: float = 0.0

    def record(self, prompt: int, completion: int) -> None:
        self.requests += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion

    def reset(self) -> None:
        self.requests = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.retries = 0
        self.waited_seconds = 0.0

    def as_json(self) -> dict:
        return {
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "retries": self.retries,
            "waited_seconds": round(self.waited_seconds, 1),
        }


quota = QuotaCounter()

# One per model id: sharing made a wait on one hold up the other.
_limiters: dict[str, RateLimitState] = {}


def limiter_for(model: str) -> RateLimitState:
    if model not in _limiters:
        _limiters[model] = RateLimitState()
    return _limiters[model]


def forget_rate_limits() -> None:
    """Start again knowing nothing about any budget.

    For tests: a budget left over from one test makes the next one sleep
    for real, which looks like a hang rather than a leak.
    """
    for state in _limiters.values():
        state.remaining_tokens = None
        state.limit_tokens = None
        state.reset_in = 0.0
        state.seen_at = 0.0


# The default model's limiter, for callers that reach for "the" rate limiter.
rate_limit = limiter_for(DEFAULT_MODEL)


def _estimate_tokens(messages: list[dict], tools: list[dict] | None) -> int:
    """Rough size of a request, for deciding whether to wait for the window.

    Tool schemas are counted too. They are sent on every turn of a loop and
    are easily a thousand tokens, so leaving them out would under-estimate
    every request an agent makes.
    """
    characters = 0

    for message in messages:
        characters += len(message.get("content") or "")
        for call in message.get("tool_calls") or []:
            characters += len(((call or {}).get("function") or {}).get("arguments") or "")

    if tools:
        characters += len(json.dumps(tools))

    return characters // CHARACTERS_PER_TOKEN + COMPLETION_ALLOWANCE


async def _pause(seconds: float, why: str) -> None:
    if seconds <= 0:
        return
    logger.info("waiting %.1fs: %s", seconds, why)
    quota.waited_seconds += seconds
    await asyncio.sleep(seconds)
