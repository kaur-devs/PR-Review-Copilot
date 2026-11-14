"""One HTTP client for an OpenAI-compatible chat API.

Everything else in this package is a piece this needs: `settings` for where
to send, `budget` for whether to send yet, `errors` for what came back
instead, `messages` for reading it.

The names below are re-exported because the rest of the project imports
them from here, and because one import line per caller is worth more than
a tidy module boundary nobody crosses.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

import httpx

from src.llm.budget import (
    CHARACTERS_PER_TOKEN,
    COMPLETION_ALLOWANCE,
    HEADER_LIMIT_TOKENS,
    HEADER_REMAINING_TOKENS,
    HEADER_RESET_TOKENS,
    QuotaCounter,
    RateLimitState,
    _estimate_tokens,
    _pause,
    forget_rate_limits,
    limiter_for,
    parse_duration,
    quota,
    rate_limit,
)
from src.llm.errors import (
    DailyQuotaExhausted,
    InvalidResponse,
    LLMError,
    LLMNotConfigured,
    RateLimited,
    BadToolArguments,
    UnknownTool,
    _bad_tool_arguments,
    _unknown_tool,
)
from src.llm.messages import (
    FINISH_STOP,
    FINISH_TOOL_CALLS,
    Completion,
    ToolCall,
    _read_tool_calls,
    tool_result_message,
)
from src.llm.settings import (
    BACKOFF_SECONDS,
    DAILY_LIMIT_MARKER,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    MAX_ATTEMPTS,
    MAX_WAIT_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    RETRYABLE_STATUS,
)

logger = logging.getLogger(__name__)


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
        return await self._send(
            payload,
            _estimate_tokens(
                [{"content": system}, {"content": user}], None
            ),
        )

    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> Completion:
        """One turn of a tool-using conversation.

        The whole history goes up every time, so the caller owns the message
        list and appends to it between turns.

        `json_mode` stays off while tools are offered: providers disagree
        about whether the two can be combined, and a refused request
        mid-loop costs the whole run. It is worth asking for on a turn that
        offers no tools, because a reply that must be a JSON object cannot
        be a tool call — which is the one thing gpt-oss does when told it
        has no lookups left.
        """
        payload = self._payload(
            messages, json_mode=json_mode, temperature=temperature,
            max_tokens=max_tokens, tools=tools,
        )
        return await self._send(payload, _estimate_tokens(messages, tools))

    async def _send(self, payload: dict, estimated: int) -> Completion:
        if self.client is None:
            raise LLMError("use the provider inside 'async with'")

        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/chat/completions"

        budget = limiter_for(self.model)
        await budget.admit(estimated)

        last_error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self.client.post(url, headers=headers, json=payload)
            except httpx.RequestError as error:
                last_error = error
                logger.warning("attempt %s could not reach the model: %s", attempt, error)
                wait = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
            else:
                budget.update(response.headers)

                if response.status_code == 200:
                    return self._read(response, attempt)

                if response.status_code == 400:
                    # Both are rejected before our toolbox runs, so neither
                    # reaches the ToolFailure path the loop usually uses.
                    rejected = _unknown_tool(response.text) or _bad_tool_arguments(
                        response.text
                    )
                    if rejected is not None:
                        raise rejected

                if (
                    response.status_code == 429
                    and DAILY_LIMIT_MARKER in response.text
                ):
                    raise DailyQuotaExhausted(
                        f"the day's tokens for {self.model} are spent; "
                        "reviews will fail until the budget resets"
                    )

                last_error = LLMError(
                    f"the model returned {response.status_code}: {response.text[:200]}"
                )

                if response.status_code not in RETRYABLE_STATUS:
                    raise last_error

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
            raise InvalidResponse(f"unexpected response shape: {error}") from error

        # content is null whenever a tool was asked for, which is not an error.
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
