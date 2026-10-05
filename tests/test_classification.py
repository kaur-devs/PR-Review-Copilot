import json

import httpx
import pytest

from src.classification.classify import classify
from src.classification.kinds import (
    API,
    CONFIG,
    DEPENDENCY,
    DOCS,
    FALLBACK,
    LOGIC,
    MODEL_BASED,
    RULE_BASED,
    SCHEMA,
    TEST,
    classify_by_rules,
)
from src.context.symbols import Symbol
from src.diff.parse import parse_patch
from src.review.provider import LLMProvider

PATCH = "@@ -1,2 +1,3 @@\n import os\n+import sys\n def run():"


def diff_for(path):
    return parse_patch(PATCH, path)


def fake_model(answer=None, *, status=200):
    def handle(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"error": "no"})
        return httpx.Response(200, json={
            "model": "test",
            "choices": [{"message": {"content": json.dumps(answer or {"files": []})}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def test_tests_are_recognised_without_a_model():
    for path in ["tests/test_user.py", "test_user.py", "app/user_test.py", "tests/fixtures.py"]:
        assert classify_by_rules(path).kind == TEST


def test_migrations_are_schema():
    assert classify_by_rules("backend/alembic/versions/001_init.py").kind == SCHEMA
    assert classify_by_rules("app/migrations/0003_add_email.py").kind == SCHEMA


def test_dependency_files_are_recognised():
    for path in ["requirements.txt", "pyproject.toml", "frontend/package.json", "go.mod"]:
        assert classify_by_rules(path).kind == DEPENDENCY


def test_documentation_is_recognised():
    assert classify_by_rules("README.md").kind == DOCS
    assert classify_by_rules("docs/09_LLD.md").kind == DOCS


def test_configuration_is_recognised():
    for path in ["app/config.py", "alembic.ini", "docker-compose.yml", "Dockerfile"]:
        assert classify_by_rules(path).kind == CONFIG


def test_ordinary_source_needs_the_model():
    assert classify_by_rules("app/services/embedder.py") is None
    assert classify_by_rules("app/routers/repos.py") is None


async def test_rules_settle_what_they_can_without_calling_the_model():
    """Every file settled by a rule is a model request not spent."""
    diffs = [diff_for("tests/test_user.py"), diff_for("requirements.txt")]

    result = await classify(None, diffs, [])

    assert result["tests/test_user.py"].source == RULE_BASED
    assert result["requirements.txt"].source == RULE_BASED


async def test_the_model_is_asked_only_about_what_is_left():
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={
            "model": "t",
            "choices": [{"message": {"content": json.dumps({"files": [
                {"file": "app/routers/repos.py", "kind": "api", "reason": "a route"},
            ]})}}],
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    diffs = [diff_for("README.md"), diff_for("app/routers/repos.py")]

    async with LLMProvider(api_key="k", client=client) as model:
        result = await classify(model, diffs, [])

    prompt = sent[0]["messages"][1]["content"]
    assert "app/routers/repos.py" in prompt
    assert "README.md" not in prompt
    assert result["app/routers/repos.py"].kind == API
    assert result["app/routers/repos.py"].source == MODEL_BASED


async def test_everything_undecided_goes_in_one_request():
    """ADR-006: batch the calls. One request for ten files, not ten."""
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={
            "model": "t",
            "choices": [{"message": {"content": json.dumps({"files": []})}}],
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    diffs = [diff_for(f"app/module_{n}.py") for n in range(10)]

    async with LLMProvider(api_key="k", client=client) as model:
        await classify(model, diffs, [])

    assert len(calls) == 1


async def test_the_changed_symbols_are_included_in_the_prompt():
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={
            "model": "t", "choices": [{"message": {"content": '{"files": []}'}}],
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    symbol = Symbol("embed_texts", "embed_texts", "function", "app/embedder.py", 10, 20)

    async with LLMProvider(api_key="k", client=client) as model:
        await classify(model, [diff_for("app/embedder.py")], [symbol])

    assert "embed_texts" in sent[0]["messages"][1]["content"]


async def test_a_model_failure_falls_back_rather_than_stopping():
    client = fake_model(status=500)
    diffs = [diff_for("app/service.py")]

    async with LLMProvider(api_key="k", client=client) as model:
        result = await classify(model, diffs, [])

    assert result["app/service.py"].kind == LOGIC
    assert result["app/service.py"].source == FALLBACK


async def test_a_file_the_model_forgets_still_gets_a_label():
    client = fake_model({"files": []})
    diffs = [diff_for("app/service.py")]

    async with LLMProvider(api_key="k", client=client) as model:
        result = await classify(model, diffs, [])

    assert result["app/service.py"].source == FALLBACK
    assert "left it out" in result["app/service.py"].reason


async def test_an_invented_kind_is_rejected():
    client = fake_model({"files": [
        {"file": "app/service.py", "kind": "wizardry", "reason": "magic"},
    ]})

    async with LLMProvider(api_key="k", client=client) as model:
        result = await classify(model, [diff_for("app/service.py")], [])

    assert result["app/service.py"].kind == LOGIC
    assert result["app/service.py"].source == FALLBACK


async def test_a_file_we_never_asked_about_is_ignored():
    client = fake_model({"files": [
        {"file": "app/service.py", "kind": "logic", "reason": "ok"},
        {"file": "/etc/passwd", "kind": "config", "reason": "injected"},
    ]})

    async with LLMProvider(api_key="k", client=client) as model:
        result = await classify(model, [diff_for("app/service.py")], [])

    assert "/etc/passwd" not in result


async def test_no_model_at_all_still_returns_a_label_for_everything():
    diffs = [diff_for("app/service.py"), diff_for("README.md")]

    result = await classify(None, diffs, [])

    assert set(result) == {"app/service.py", "README.md"}
    assert result["app/service.py"].source == FALLBACK


def test_only_some_kinds_need_surrounding_code():
    from src.classification.kinds import Classification

    assert Classification("a.py", API, RULE_BASED).needs_context
    assert Classification("a.py", SCHEMA, RULE_BASED).needs_context
    assert not Classification("a.py", DOCS, RULE_BASED).needs_context
    assert not Classification("a.py", DEPENDENCY, RULE_BASED).needs_context
