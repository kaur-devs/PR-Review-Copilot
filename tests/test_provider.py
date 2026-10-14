import json

import httpx
import pytest

from src.review.provider import (
    InvalidResponse,
    LLMError,
    LLMNotConfigured,
    LLMProvider,
    RateLimited,
    quota,
)


@pytest.fixture(autouse=True)
def reset_quota():
    quota.reset()
    yield
    quota.reset()


def fake_model(
    *, content="hello", status=200, statuses=None, body=None, usage=True
):
    calls: list[httpx.Request] = []
    remaining = list(statuses or [])

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        code = remaining.pop(0) if remaining else status

        if code != 200:
            return httpx.Response(code, json={"error": {"message": "no"}})

        payload = body or {
            "model": "openai/gpt-oss-120b",
            "choices": [{"message": {"content": content}}],
        }
        if usage and "usage" not in payload:
            payload["usage"] = {"prompt_tokens": 120, "completion_tokens": 40}

        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), calls


def make(client) -> LLMProvider:
    return LLMProvider(api_key="test-key", client=client)


async def test_a_completion_comes_back():
    client, _ = fake_model(content="a finding")

    async with make(client) as model:
        result = await model.complete("you review code", "look at this")

    assert result.text == "a finding"
    assert result.attempts == 1


async def test_the_request_is_shaped_the_way_providers_expect():
    client, calls = fake_model()

    async with make(client) as model:
        await model.complete("system words", "user words")

    body = json.loads(calls[0].content)
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["messages"][0] == {"role": "system", "content": "system words"}
    assert body["messages"][1] == {"role": "user", "content": "user words"}
    assert calls[0].headers["Authorization"] == "Bearer test-key"
    assert str(calls[0].url).endswith("/chat/completions")


async def test_temperature_is_zero_by_default():
    """Determinism matters: the evaluation harness replays the same inputs."""
    client, calls = fake_model()

    async with make(client) as model:
        await model.complete("s", "u")

    assert json.loads(calls[0].content)["temperature"] == 0.0


async def test_json_mode_is_requested_when_asked():
    client, calls = fake_model(content='{"findings": []}')

    async with make(client) as model:
        await model.complete_json("s", "u")

    assert json.loads(calls[0].content)["response_format"] == {"type": "json_object"}


async def test_json_comes_back_parsed():
    client, _ = fake_model(content='{"findings": [{"line": 7}]}')

    async with make(client) as model:
        result = await model.complete_json("s", "u")

    assert result == {"findings": [{"line": 7}]}


async def test_output_that_is_not_json_is_reported_clearly():
    client, _ = fake_model(content="Sure! Here are the findings:")

    async with make(client) as model:
        with pytest.raises(InvalidResponse):
            await model.complete_json("s", "u")


async def test_rate_limiting_is_retried():
    client, calls = fake_model(statuses=[429, 429, 200])

    async with make(client) as model:
        result = await model.complete("s", "u")

    assert len(calls) == 3
    assert result.attempts == 3


async def test_giving_up_on_rate_limiting_says_so():
    from src.review.provider import MAX_ATTEMPTS

    client, calls = fake_model(statuses=[429] * MAX_ATTEMPTS)

    async with make(client) as model:
        with pytest.raises(RateLimited):
            await model.complete("s", "u")

    assert len(calls) == MAX_ATTEMPTS


async def test_a_server_error_is_retried():
    client, calls = fake_model(statuses=[503, 200])

    async with make(client) as model:
        await model.complete("s", "u")

    assert len(calls) == 2


async def test_a_bad_request_is_not_retried():
    """Our fault, not theirs. Trying again would fail identically."""
    client, calls = fake_model(statuses=[400])

    async with make(client) as model:
        with pytest.raises(LLMError):
            await model.complete("s", "u")

    assert len(calls) == 1


async def test_an_unexpected_response_shape_is_reported():
    client, _ = fake_model(body={"unexpected": True})

    async with make(client) as model:
        with pytest.raises(InvalidResponse):
            await model.complete("s", "u")


async def test_usage_is_counted_towards_the_quota():
    client, _ = fake_model()

    async with make(client) as model:
        await model.complete("s", "u")
        await model.complete("s", "u")

    assert quota.requests == 2
    assert quota.prompt_tokens == 240
    assert quota.completion_tokens == 80


async def test_retries_are_counted_separately():
    client, _ = fake_model(statuses=[429, 200])

    async with make(client) as model:
        await model.complete("s", "u")

    assert quota.requests == 1
    assert quota.retries == 1


def test_a_missing_key_is_reported_with_what_to_do(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    with pytest.raises(LLMNotConfigured, match="console.groq.com"):
        LLMProvider.from_env()


def test_the_provider_is_three_environment_variables(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("LLM_MODEL", "something/else:free")

    model = LLMProvider.from_env()

    assert model.base_url == "https://openrouter.ai/api/v1"
    assert model.model == "something/else:free"


# ---------------------------------------------------------------------------
# Living inside a tokens-per-minute budget
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def forget_rate_limit_state():
    from src.review.provider import rate_limit

    rate_limit.remaining_tokens = None
    rate_limit.reset_in = 0.0
    rate_limit.seen_at = 0.0
    yield
    rate_limit.remaining_tokens = None


def test_groq_duration_format_is_understood():
    from src.review.provider import parse_duration

    assert parse_duration("7.66s") == 7.66
    assert parse_duration("2m59.56s") == 179.56
    assert parse_duration("1h3m") == 3780.0
    assert parse_duration(None) == 0.0
    assert parse_duration("nonsense") == 0.0


def model_with_headers(remaining, reset, *, status=200, retry_after=None, record=None):
    def handle(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        headers = {
            "x-ratelimit-remaining-tokens": str(remaining),
            "x-ratelimit-reset-tokens": reset,
        }
        if retry_after:
            headers["retry-after"] = retry_after
        if status != 200:
            return httpx.Response(status, json={"error": "no"}, headers=headers)
        return httpx.Response(200, headers=headers, json={
            "model": "m",
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def test_the_remaining_token_budget_is_remembered():
    from src.review.provider import rate_limit

    async with make(model_with_headers(5500, "41.2s")) as model:
        await model.complete("s", "u")

    assert rate_limit.remaining_tokens == 5500
    assert rate_limit.reset_in == 41.2


async def test_we_wait_when_the_next_request_will_not_fit(monkeypatch):
    """Better to pause than to be refused and retry blindly."""
    from src.review import provider

    waits = []

    async def record_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(provider.asyncio, "sleep", record_sleep)

    async with make(model_with_headers(100, "30s")) as model:
        await model.complete("s", "u")
        await model.complete("x" * 4000, "y" * 4000)

    assert waits
    assert 0 < waits[0] <= 30


async def test_we_do_not_wait_when_there_is_room(monkeypatch):
    from src.review import provider

    waits = []

    async def record_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(provider.asyncio, "sleep", record_sleep)

    async with make(model_with_headers(7900, "50s")) as model:
        await model.complete("s", "u")
        await model.complete("short", "short")

    assert waits == []


async def test_the_provider_is_obeyed_when_it_says_how_long_to_wait(monkeypatch):
    """Guessing is how a retry lands inside the same closed window."""
    from src.review import provider

    waits = []

    async def record_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(provider.asyncio, "sleep", record_sleep)

    client = model_with_headers(0, "25s", status=429, retry_after="23")

    async with make(client) as model:
        with pytest.raises(RateLimited):
            await model.complete("s", "u")

    assert 23.0 in waits


async def test_a_wait_is_never_longer_than_the_cap(monkeypatch):
    from src.review import provider

    waits = []

    async def record_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(provider.asyncio, "sleep", record_sleep)

    client = model_with_headers(0, "10m", status=429, retry_after="600")

    async with make(client) as model:
        with pytest.raises(RateLimited):
            await model.complete("s", "u")

    assert max(waits) <= provider.MAX_WAIT_SECONDS


async def test_time_spent_waiting_is_counted():
    from src.review.provider import quota

    client = model_with_headers(0, "5s", status=429, retry_after="5")

    async with make(client) as model:
        with pytest.raises(RateLimited):
            await model.complete("s", "u")

    assert quota.waited_seconds > 0
