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


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    attempts: int = 1

    def as_json(self) -> dict:
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as error:
            raise InvalidResponse(f"the model did not return valid JSON: {error}")


quota = QuotaCounter()
rate_limit = RateLimitState()


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
        self, system: str, user: str, *, json_mode: bool, temperature: float,
        max_tokens: int | None,
    ) -> dict:
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens
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
        if self.client is None:
            raise LLMError("use the provider inside 'async with'")

        payload = self._payload(
            system, user, json_mode=json_mode, temperature=temperature,
            max_tokens=max_tokens,
        )
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/chat/completions"

        estimated = (len(system) + len(user)) // CHARACTERS_PER_TOKEN
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
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as error:
            raise InvalidResponse(f"unexpected response shape: {error}")

        usage = body.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        quota.record(prompt_tokens, completion_tokens)

        return Completion(
            text=text or "",
            model=body.get("model", self.model),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            attempts=attempt,
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
