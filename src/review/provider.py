from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"

REQUEST_TIMEOUT_SECONDS = 60.0
MAX_ATTEMPTS = 4

# Used only when the provider does not tell us how long to wait.
BACKOFF_SECONDS = (5.0, 20.0, 45.0)
MAX_WAIT_SECONDS = 90.0

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Free tiers cap tokens per minute, not just requests. A review with its
# surrounding code can be most of a minute's budget on its own, so we wait
# for the window to refill rather than being refused.
HEADER_REMAINING_TOKENS = "x-ratelimit-remaining-tokens"
HEADER_RESET_TOKENS = "x-ratelimit-reset-tokens"

# Roughly four characters to a token. Good enough to decide whether to wait.
CHARACTERS_PER_TOKEN = 4

# What the provider says about why it stopped. "tool_calls" means the model
# wants something run before it can answer.
FINISH_STOP = "stop"
FINISH_TOOL_CALLS = "tool_calls"


class LLMError(Exception):
    pass


class LLMNotConfigured(LLMError):
    pass


class RateLimited(LLMError):
    pass


class InvalidResponse(LLMError):
    pass


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
    """What the provider last told us about the token budget."""

    remaining_tokens: int | None = None
    reset_in: float = 0.0
    seen_at: float = 0.0

    def update(self, headers) -> None:
        remaining = headers.get(HEADER_REMAINING_TOKENS)
        if remaining is None:
            return
        try:
            self.remaining_tokens = int(float(remaining))
        except ValueError:
            return
        self.reset_in = parse_duration(headers.get(HEADER_RESET_TOKENS))
        self.seen_at = time.monotonic()

    def wait_needed(self, estimated_tokens: int) -> float:
        """How long to wait before a request of this size is likely to fit."""
        if self.remaining_tokens is None:
            return 0.0
        if self.remaining_tokens >= estimated_tokens:
            return 0.0

        elapsed = time.monotonic() - self.seen_at
        return max(0.0, min(self.reset_in - elapsed, MAX_WAIT_SECONDS))


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
            )
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
            raise InvalidResponse(f"the model did not return valid JSON: {error}")

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


quota = QuotaCounter()
rate_limit = RateLimitState()


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

    return characters // CHARACTERS_PER_TOKEN


async def _pause(seconds: float, why: str) -> None:
    if seconds <= 0:
        return
    logger.info("waiting %.1fs: %s", seconds, why)
    quota.waited_seconds += seconds
    await asyncio.sleep(seconds)


@dataclass
class LLMProvider:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    client: httpx.AsyncClient | None = None
    _owns_client: bool = field(default=False, init=False)

    @classmethod
    def from_env(
        cls, client: httpx.AsyncClient | None = None, model: str | None = None
    ) -> "LLMProvider":
        api_key = os.getenv("LLM_API_KEY")
        if not api_key:
            raise LLMNotConfigured(
                "LLM_API_KEY is not set. Get a free key from console.groq.com "
                "and add it to .env"
            )
        return cls(
            api_key=api_key,
            base_url=os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL),
            model=model or os.getenv("LLM_MODEL", DEFAULT_MODEL),
            client=client,
        )

    async def __aenter__(self) -> "LLMProvider":
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
            self._owns_client = True
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self.client is not None:
            await self.client.aclose()

    def _payload(
        self, messages: list[dict], *, json_mode: bool, temperature: float,
        max_tokens: int | None, tools: list[dict] | None = None,
    ) -> dict:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    async def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> Completion:
        payload = self._payload(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            json_mode=json_mode, temperature=temperature, max_tokens=max_tokens,
        )
        return await self._send(payload, (len(system) + len(user)) // CHARACTERS_PER_TOKEN)

    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> Completion:
        """One turn of a tool-using conversation.

        The whole history goes up every time, so the caller owns the message
        list and appends to it between turns. JSON mode is deliberately not
        offered: providers disagree about whether it can be combined with
        tools, and the loop can read the final answer with Completion.as_json.
        """
        payload = self._payload(
            messages, json_mode=False, temperature=temperature,
            max_tokens=max_tokens, tools=tools,
        )
        return await self._send(payload, _estimate_tokens(messages, tools))

    async def _send(self, payload: dict, estimated: int) -> Completion:
        if self.client is None:
            raise LLMError("use the provider inside 'async with'")

        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/chat/completions"

        await _pause(
            rate_limit.wait_needed(estimated),
            f"only {rate_limit.remaining_tokens} tokens left this minute, "
            f"this request needs about {estimated}",
        )

        last_error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self.client.post(url, headers=headers, json=payload)
            except httpx.RequestError as error:
                last_error = error
                logger.warning("attempt %s could not reach the model: %s", attempt, error)
                wait = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
            else:
                rate_limit.update(response.headers)

                if response.status_code == 200:
                    return self._read(response, attempt)

                last_error = LLMError(
                    f"the model returned {response.status_code}: {response.text[:200]}"
                )

                if response.status_code not in RETRYABLE_STATUS:
                    raise last_error

                # The provider knows exactly how long the window has left.
                # Guessing is how a retry lands inside the same closed window.
                told_to_wait = parse_duration(response.headers.get("retry-after"))
                wait = min(
                    told_to_wait or BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)],
                    MAX_WAIT_SECONDS,
                )

                if response.status_code == 429:
                    logger.warning(
                        "rate limited on attempt %s, waiting %.1fs", attempt, wait
                    )

            if attempt < MAX_ATTEMPTS:
                quota.retries += 1
                await _pause(wait, f"attempt {attempt} failed")

        if isinstance(last_error, LLMError) and "429" in str(last_error):
            raise RateLimited(str(last_error))
        raise LLMError(f"gave up after {MAX_ATTEMPTS} attempts: {last_error}")

    def _read(self, response: httpx.Response, attempt: int) -> Completion:
        body = response.json()

        try:
            choice = body["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError) as error:
            raise InvalidResponse(f"unexpected response shape: {error}")

        # content is null whenever the model asks for a tool, so an empty
        # string here is only an error if nothing was asked for either.
        text = message.get("content") or ""
        tool_calls = _read_tool_calls(message)
        finish_reason = choice.get("finish_reason") or FINISH_STOP

        if not text and not tool_calls:
            raise InvalidResponse(
                f"the model returned neither content nor a tool call "
                f"(finish_reason {finish_reason!r})"
            )

        usage = body.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        quota.record(prompt_tokens, completion_tokens)

        return Completion(
            text=text,
            model=body.get("model", self.model),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            attempts=attempt,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
        )

    async def complete_json(
        self, system: str, user: str, *, temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> dict:
        completion = await self.complete(
            system, user, json_mode=True, temperature=temperature,
            max_tokens=max_tokens,
        )
        return completion.as_json()
