import json

import httpx
import pytest

from src.context.bundle import ContextBundle, ContextItem
from src.diff.parse import parse_patch
from src.judge.prompts import build_user_prompt
from src.judge.verify import judge_findings
from src.review.findings import Finding
from tests.fakes import make_patch

PATCH = make_patch(
    ["def get_user(user_id):", "    return _USERS[user_id]", ""],
    ["def get_user(user_id):", "    return _USERS.get(user_id)", ""],
    start=17,
)
DIFFS = [parse_patch(PATCH, "app/models.py")]


def make(line=18, message="Callers assume a user is returned"):
    return Finding(
        file="app/models.py", line=line, severity="high", category="correctness",
        message=message, rationale="service.py reads user.email straight after.",
        confidence=0.9,
    )


def fake_judge(verdicts=None, *, raw=None, status=200, record=None):
    def handle(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={"error": "no"})
        body = raw if raw is not None else {"verdicts": verdicts or []}
        return httpx.Response(200, json={
            "model": "qwen/qwen3.8-27b",
            "choices": [{"message": {"content": json.dumps(body)}}],
        })

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def run(findings, client, context=None):
    return await judge_findings(
        findings, DIFFS, context or ContextBundle(), client=client
    )


async def test_a_supported_finding_survives():
    result = await run(
        [make()], fake_judge([{"index": 0, "supported": True, "reason": "real"}])
    )

    assert len(result.kept) == 1
    assert result.rejected == []


async def test_an_unsupported_finding_is_dropped_with_a_reason():
    result = await run(
        [make()],
        fake_judge([{"index": 0, "supported": False, "reason": "the code handles this"}]),
    )

    assert result.kept == []
    assert result.rejected[0][1] == "the code handles this"


async def test_a_mixed_batch_is_split():
    findings = [make(17), make(18), make(19)]
    result = await run(findings, fake_judge([
        {"index": 0, "supported": True, "reason": "real"},
        {"index": 1, "supported": False, "reason": "hypothetical"},
        {"index": 2, "supported": True, "reason": "real"},
    ]))

    assert [f.line for f in result.kept] == [17, 19]
    assert [f.line for f, _ in result.rejected] == [18]


async def test_every_finding_is_checked_in_one_request():
    """ADR-006: judge the whole batch in one call, not one call each."""
    calls = []
    findings = [make(17), make(18), make(19)]

    await run(findings, fake_judge([], record=calls))

    assert len(calls) == 1


async def test_a_judge_failure_keeps_everything():
    """Silently dropping real findings is worse than keeping a weak one."""
    result = await run([make()], fake_judge(status=500))

    assert len(result.kept) == 1
    assert result.ran is False


async def test_an_unusable_answer_keeps_everything():
    result = await run([make()], fake_judge(raw={"nonsense": True}))

    assert len(result.kept) == 1
    assert result.ran is False


async def test_a_finding_the_judge_skipped_is_kept():
    findings = [make(17), make(18)]
    result = await run(findings, fake_judge([
        {"index": 0, "supported": False, "reason": "no"},
    ]))

    assert [f.line for f in result.kept] == [18]


async def test_an_index_we_never_sent_is_ignored():
    result = await run([make()], fake_judge([
        {"index": 0, "supported": True, "reason": "real"},
        {"index": 99, "supported": False, "reason": "invented"},
    ]))

    assert len(result.kept) == 1


async def test_the_judge_is_skipped_when_switched_off(monkeypatch):
    monkeypatch.setenv("JUDGE_ENABLED", "0")
    calls = []

    result = await run([make()], fake_judge([], record=calls))

    assert len(result.kept) == 1
    assert result.ran is False
    assert calls == []


async def test_nothing_to_judge_makes_no_request():
    calls = []

    result = await run([], fake_judge([], record=calls))

    assert result.kept == []
    assert calls == []


async def test_a_different_model_does_the_judging(monkeypatch):
    """A model asked to check its own work mostly agrees with itself."""
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("LLM_JUDGE_MODEL", "qwen/qwen3.8-27b")
    calls = []

    await run([make()], fake_judge([], record=calls))

    assert calls[0]["model"] == "qwen/qwen3.8-27b"


async def test_the_judge_sees_the_code_not_just_the_claim():
    calls = []

    await run([make()], fake_judge([], record=calls))

    prompt = calls[0]["messages"][1]["content"]
    assert "_USERS.get(user_id)" in prompt
    assert "Callers assume a user is returned" in prompt


async def test_the_judge_sees_the_connected_code_too():
    context = ContextBundle(items=[ContextItem(
        file="app/service.py", start_line=4, end_line=6,
        snippet="def user_email(i):\n    return get_user(i).email",
        relation="inbound_call", discovery="import_edge_confirmed",
        confidence=0.9, changed_symbol="get_user", reference_lines=(5,),
    )])
    calls = []

    await run([make()], fake_judge([], record=calls), context=context)

    prompt = calls[0]["messages"][1]["content"]
    assert "app/service.py" in prompt
    assert "get_user(i).email" in prompt


def test_findings_are_numbered_so_verdicts_can_be_matched_back():
    prompt = build_user_prompt([make(17), make(18)], DIFFS, ContextBundle())

    assert "[0]" in prompt
    assert "[1]" in prompt
