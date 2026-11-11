"""Verification that can go and look.

The point of this stage is that it should be able to kill a plausible
finding by reading a file the generator never saw. These tests check the
routing, the batching, and that it fails open the way the cheap judge
does.
"""

import json

import httpx
import pytest

from src.agents.tools import Toolbox
from src.context.snapshot import Snapshot
from src.diff.parse import parse_patch
from src.judge.investigate import (
    INVESTIGATE_BELOW_CONFIDENCE,
    investigate,
    merge,
    needs_investigation,
    split,
)
from src.judge.verify import JudgeResult
from src.review.findings import Finding
from src.review.provider import LLMProvider, quota
from tests.fakes import make_patch


@pytest.fixture(autouse=True)
def reset_quota():
    quota.reset()
    yield
    quota.reset()


def make(severity="high", confidence=0.8, file="app/billing.py", line=1, **over):
    return Finding(
        file=file, line=line, severity=severity, category="correctness",
        message="charge gained a required parameter",
        rationale="app/orders.py:4 calls charge with one argument",
        confidence=confidence, **over,
    )


def a_diff(path="app/billing.py"):
    return parse_patch(
        make_patch(["def charge(a):", "    return a"],
                   ["def charge(a, c):", "    return a * r(c)"]),
        path,
    )


def verdicts(*pairs, prose=""):
    body = json.dumps({
        "verdicts": [
            {"index": n, "verdict": v, "reason": f"checked, {v}"}
            for n, v in enumerate(pairs)
        ]
    })
    return prose + body


def scripted(*replies):
    sent = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        reply = replies[min(len(sent) - 1, len(replies) - 1)]
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {"message": "no"}})
        return httpx.Response(200, json={
            "model": "m",
            "choices": [{"finish_reason": "stop", "message": {"content": reply}}],
            "usage": {"prompt_tokens": 400, "completion_tokens": 50},
        })

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), sent


@pytest.fixture
def box(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "billing.py").write_text("def charge(a, c):\n    return a\n")
    (tmp_path / "app" / "orders.py").write_text(
        "from app.billing import charge\n\ndef place():\n    return charge(10, 'gbp')\n"
    )
    return Toolbox(Snapshot(tmp_path, "o/r", "abc"))


async def run(client, box, findings, diffs=None):
    async with LLMProvider(api_key="k", client=client) as model:
        return await investigate(model, box, findings, diffs or [a_diff()])


# --------------------------------------------------------------- routing

def test_high_severity_always_earns_a_proper_check():
    assert needs_investigation(make("high", 0.99))


def test_an_unsure_finding_earns_a_proper_check():
    assert needs_investigation(make("medium", INVESTIGATE_BELOW_CONFIDENCE - 0.01))


def test_a_confident_medium_finding_takes_the_cheap_path():
    assert not needs_investigation(make("medium", 0.9))


def test_the_split_sends_each_finding_one_way_only():
    findings = [make("high", 0.9), make("medium", 0.95), make("medium", 0.5)]

    risky, rest = split(findings)

    assert len(risky) == 2 and len(rest) == 1
    assert len(risky) + len(rest) == len(findings)


# -------------------------------------------------------------- batching

async def test_findings_in_one_file_share_a_single_loop(box):
    """Their evidence overlaps almost entirely, so opening the file once
    is the whole saving."""
    client, sent = scripted(verdicts("supported", "supported"))

    result, runs = await run(
        client, box, [make(line=1), make(line=2)]
    )

    assert len(sent) == 1
    assert list(runs) == ["app/billing.py"]
    assert len(result.kept) == 2


async def test_two_files_get_two_loops(box):
    client, sent = scripted(verdicts("supported"))

    _, runs = await run(
        client, box,
        [make(file="app/billing.py"), make(file="app/orders.py")],
        diffs=[a_diff("app/billing.py"), a_diff("app/orders.py")],
    )

    assert len(sent) == 2
    assert set(runs) == {"app/billing.py", "app/orders.py"}


# -------------------------------------------------------------- verdicts

async def test_a_contradicted_finding_is_dropped(box):
    """The point of the design: it read the caller and the claim failed."""
    client, _ = scripted(verdicts("contradicted"))

    result, _ = await run(client, box, [make()])

    assert result.kept == []
    assert len(result.rejected) == 1
    assert "contradicted" in result.rejected[0][1]


async def test_an_unverifiable_finding_is_kept_but_weakened(box):
    client, _ = scripted(verdicts("unverifiable"))

    result, _ = await run(client, box, [make(confidence=0.8)])

    assert result.kept[0].confidence == 0.56
    assert len(result.unverified) == 1


async def test_a_supported_finding_keeps_its_confidence(box):
    client, _ = scripted(verdicts("supported"))

    result, _ = await run(client, box, [make(confidence=0.85)])

    assert result.kept[0].confidence == 0.85
    assert result.unverified == []


async def test_a_finding_the_verifier_ignored_is_not_silently_trusted(box):
    """Saying nothing is not the same as saying yes."""
    client, _ = scripted(json.dumps({"verdicts": []}))

    result, _ = await run(client, box, [make(confidence=0.8)])

    assert result.kept[0].confidence == 0.56
    assert "did not say" in result.unverified[0][1]


# ------------------------------------------------------------ fails open

async def test_a_model_failure_keeps_the_findings(box):
    """Silently discarding real findings is worse than keeping a weak one."""
    client, _ = scripted(400)

    result, _ = await run(client, box, [make()])

    assert len(result.kept) == 1


async def test_an_unusable_answer_keeps_the_findings(box):
    client, _ = scripted("I had a look and it seems fine to me")

    result, _ = await run(client, box, [make(confidence=0.8)])

    assert len(result.kept) == 1


async def test_nothing_to_check_makes_no_call(box):
    client, sent = scripted(verdicts("supported"))

    result, runs = await run(client, box, [])

    assert sent == []
    assert runs == {}
    assert not result.ran


async def test_the_judge_can_be_switched_off(box, monkeypatch):
    monkeypatch.setenv("JUDGE_ENABLED", "0")
    client, sent = scripted(verdicts("contradicted"))

    result, _ = await run(client, box, [make()])

    assert sent == []
    assert len(result.kept) == 1
    assert not result.ran


# ---------------------------------------------------------------- prompt

async def test_the_claims_and_the_diff_both_reach_the_verifier(box):
    client, sent = scripted(verdicts("supported"))

    await run(client, box, [make()])

    user = sent[0]["messages"][1]["content"]
    assert "CLAIMS TO CHECK" in user
    assert "[0] app/billing.py:1" in user
    assert "the reviewer was 0.8 sure" in user
    assert "def charge(a, c):" in user


async def test_the_verifier_is_given_the_tools(box):
    client, sent = scripted(verdicts("supported"))

    await run(client, box, [make()])

    offered = {t["function"]["name"] for t in sent[0]["tools"]}
    assert "read_file" in offered
    assert "find_references" in offered


# ----------------------------------------------------------------- merge

def test_the_two_paths_join_back_together():
    kept = make()
    cheap = JudgeResult(kept=[kept], ran=True)
    agentic = JudgeResult(rejected=[(make(line=9), "no")], ran=True)

    joined = merge(agentic, cheap)

    assert joined.kept == [kept]
    assert len(joined.rejected) == 1
    assert joined.ran
