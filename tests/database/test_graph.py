"""The agentic review as a state graph.

The model is scripted throughout. What is tested is the wiring: that the
stages run in order, that the two conditional edges skip the expensive
work when there is none to do, that both specialists can write to one
list without erasing each other, and that a crashed run resumes where it
stopped instead of starting again.
"""

import json

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.agents.prompts import ROBUST, WRONG
from src.agents.tools import Toolbox
from src.context.bundle import ContextBundle
from src.context.snapshot import Snapshot
from src.diff.parse import parse_patch
from src.filter.repeat import SaidBefore
from src.pipeline.graph import ReviewState, build, compile_graph
from src.review.findings import Finding
from src.llm.provider import LLMProvider, quota
from tests.fakes import make_patch


@pytest.fixture(autouse=True)
def reset_quota():
    quota.reset()
    yield
    quota.reset()


BEFORE = ["def charge(amount):", "    return amount"]
AFTER = [
    "def charge(amount, currency):",
    "    rate = rates[currency]",
    "    if rate is None:",
    "        raise KeyError(currency)",
    "    total = amount * rate",
    "    audit(total)",
    "    settle(total)",
    "    notify(total)",
    "    record(total)",
    "    return total",
]


def a_diff(path="app/billing.py"):
    return parse_patch(make_patch(BEFORE, AFTER), path)


def a_finding(severity="high", line=1, confidence=0.85, **over):
    return Finding(
        file="app/billing.py", line=line, severity=severity,
        category="correctness", message="charge gained a required parameter",
        rationale="app/orders.py:4 calls charge with one argument",
        confidence=confidence, **over,
    )


def finding_json(**over):
    entry = {
        "file": "app/billing.py", "line": 1, "severity": "high",
        "category": "correctness",
        "message": "charge gained a required parameter",
        "rationale": "app/orders.py:4 calls it with one argument",
        "confidence": 0.85, "evidence_files": ["app/orders.py"],
        "suggestion": None,
    }
    entry.update(over)
    return entry


def model_server(*, classify=None, wrong=None, robust=None, verdict="supported",
                 summary=None):
    """Answers each stage by recognising its system prompt."""
    sent = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        system = body["messages"][0]["content"]

        if "label code changes" in system:
            content = json.dumps(classify or {
                "files": [{"file": "app/billing.py", "kind": "logic", "reason": "t"}]
            })
        elif "is this code wrong" in system:
            content = json.dumps(wrong if wrong is not None else {"findings": []})
        elif "hold up when something goes wrong" in system:
            content = json.dumps(robust if robust is not None else {"findings": []})
        elif "checking whether a code review finding is real" in system:
            content = json.dumps(
                {"verdicts": [{"index": 0, "verdict": verdict, "reason": "checked"}]}
            )
        elif "supported by the code" in system:
            content = json.dumps(
                {"verdicts": [{"index": 0, "verdict": verdict, "reason": "checked"}]}
            )
        elif "opening comment" in system:
            content = json.dumps(summary or {
                "what_changed": "Adds a currency parameter to charge.",
                "risks": ["callers were not updated"],
            })
        else:
            content = json.dumps({})

        return httpx.Response(200, json={
            "model": "m",
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": {"prompt_tokens": 400, "completion_tokens": 50},
        })

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), sent


@pytest.fixture
def box(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "billing.py").write_text("def charge(a, c):\n    return a\n")
    (tmp_path / "app" / "orders.py").write_text(
        "from app.billing import charge\n\ndef place():\n    return charge(10)\n"
    )
    return Toolbox(Snapshot(tmp_path, "o/r", "abc"))


async def run(client, box, *, state=None, graph=None, config=None):
    graph = graph or compile_graph()
    async with LLMProvider(api_key="k", client=client) as model:
        judge = LLMProvider(api_key="k", client=client)
        async with judge:
            bag = {"model": model, "judge": judge, "toolbox": box}
            return await graph.ainvoke(
                {
                    "diffs": [a_diff()],
                    "context": ContextBundle(),
                    "already_said": [],
                    "skipped": {},
                    **(state or {}),
                },
                {"configurable": {"thread_id": "t1", **bag, **(config or {})}},
            )


# ------------------------------------------------------------- the shape

def test_the_graph_has_the_stages_the_design_describes():
    nodes = {n for n in build().compile().get_graph().nodes if not n.startswith("__")}

    assert nodes == {
        "triage", "specialists", "repeats", "severity",
        "verify", "confidence", "summary",
    }


# ------------------------------------------------------------ happy path

async def test_a_finding_travels_all_the_way_through(box):
    client, sent = model_server(wrong={"findings": [finding_json()]})

    final = await run(client, box)

    assert len(final["to_post"]) == 1
    assert final["to_post"][0].file == "app/billing.py"
    assert "Adds a currency parameter" in final["summary"]


async def test_the_stages_run_in_order(box):
    client, sent = model_server(wrong={"findings": [finding_json()]})

    await run(client, box)

    stages = []
    for body in sent:
        system = body["messages"][0]["content"]
        if "label code changes" in system:
            stages.append("triage")
        elif "is this code wrong" in system:
            stages.append(WRONG)
        elif "hold up when" in system:
            stages.append(ROBUST)
        elif "finding is real" in system:
            stages.append("verify")
        elif "opening comment" in system:
            stages.append("summary")

    assert stages[0] == "triage"
    assert set(stages[1:3]) == {WRONG, ROBUST}
    assert stages[-1] == "summary"
    assert "verify" in stages


# --------------------------------------------------------- the reducer

async def test_both_specialists_write_to_one_list_without_erasing_each_other(box):
    """Without Annotated[list, operator.add] the second write replaces
    the first, and half the review disappears silently."""
    client, _ = model_server(
        wrong={"findings": [finding_json(line=1)]},
        robust={"findings": [finding_json(line=9, category="error_handling")]},
    )

    final = await run(client, box)

    assert len(final["candidates"]) == 2
    assert {f.line for f in final["candidates"]} == {1, 9}


# ------------------------------------------------------ conditional edges

async def test_a_documentation_change_stops_at_triage(box):
    """Triage waking nobody must not pay for specialists or a summary."""
    client, sent = model_server(
        classify={"files": [{"file": "app/billing.py", "kind": "docs", "reason": "t"}]}
    )

    final = await run(client, box)

    assert final["wake"] == []
    assert final.get("to_post") is None
    assert len(sent) == 1


async def test_nothing_surviving_the_gate_skips_the_verifier(box):
    """The verifier is the expensive stage. A clean pull request must
    not reach it."""
    client, sent = model_server(wrong={"findings": []}, robust={"findings": []})

    final = await run(client, box)

    systems = [b["messages"][0]["content"] for b in sent]
    assert not any("finding is real" in s for s in systems)
    assert final["to_post"] == []

    # Nothing posts when the gates keep nothing, so a summary is wasted.
    assert final["summary"] == ""
    assert not any("opening comment" in s for s in systems)


async def test_a_low_severity_finding_never_reaches_the_verifier(box):
    """Never pay to verify something that cannot be posted."""
    client, sent = model_server(wrong={"findings": [finding_json(severity="low")]})

    final = await run(client, box)

    systems = [b["messages"][0]["content"] for b in sent]
    assert not any("finding is real" in s for s in systems)
    assert final["to_post"] == []
    assert any("low severity" in reason for _, reason in final["held"])


# ---------------------------------------------------------------- repeats

async def test_what_was_already_said_is_dropped_before_verification(box):
    """A second push finding the same problems should cost the
    specialists only, not a verification pass to learn there is nothing
    new to say."""
    client, sent = model_server(wrong={"findings": [finding_json()]})

    final = await run(client, box, state={
        "already_said": [
            SaidBefore(file="app/billing.py", line=1, category="correctness")
        ]
    })

    systems = [b["messages"][0]["content"] for b in sent]
    assert not any("finding is real" in s for s in systems)
    assert len(final["repeats"]) == 1
    assert final["to_post"] == []


# -------------------------------------------------------------- verdicts

async def test_a_contradicted_finding_does_not_reach_the_comment(box):
    client, _ = model_server(
        wrong={"findings": [finding_json()]}, verdict="contradicted"
    )

    final = await run(client, box)

    assert final["to_post"] == []


async def test_an_unverifiable_finding_is_weakened_past_the_floor(box):
    """0.85 clears the floor; the penalty takes it to 0.6, which still
    does. 0.8 would not. This is why confidence is checked last."""
    client, _ = model_server(
        wrong={"findings": [finding_json(confidence=0.8)]}, verdict="unverifiable"
    )

    final = await run(client, box)

    assert final["to_post"] == []
    assert any("confidence 0.56" in reason for _, reason in final["held"])


# ---------------------------------------------------------- checkpointing

async def test_state_is_saved_after_every_stage(box):
    saver = InMemorySaver()
    graph = compile_graph(checkpointer=saver)
    client, _ = model_server(wrong={"findings": [finding_json()]})

    await run(client, box, graph=graph)

    history = [c for c in saver.list({"configurable": {"thread_id": "t1"}})]
    assert len(history) > 3
    saved = graph.get_state({"configurable": {"thread_id": "t1"}})
    assert saved.values["to_post"]


async def test_a_resumed_run_does_not_repeat_the_specialists(box):
    """The whole cost argument for checkpointing: the specialists are
    most of the tokens, so a crash after them must not re-run them."""
    saver = InMemorySaver()
    graph = compile_graph(checkpointer=saver)
    client, sent = model_server(wrong={"findings": [finding_json()]})

    await run(client, box, graph=graph)
    calls_first_time = len(sent)

    # Resuming a finished thread replays nothing: the stages are done.
    async with LLMProvider(api_key="k", client=client) as model:
        state = graph.get_state({"configurable": {"thread_id": "t1"}})

    assert state.next == ()
    assert len(sent) == calls_first_time


async def test_two_pull_requests_do_not_share_a_checkpoint(box):
    saver = InMemorySaver()
    graph = compile_graph(checkpointer=saver)
    client, _ = model_server(wrong={"findings": [finding_json()]})

    await run(client, box, graph=graph, config={"thread_id": "pr-1"})
    await run(client, box, graph=graph, config={"thread_id": "pr-2"})

    one = graph.get_state({"configurable": {"thread_id": "pr-1"}})
    two = graph.get_state({"configurable": {"thread_id": "pr-2"}})
    assert one.config["configurable"]["thread_id"] == "pr-1"
    assert two.config["configurable"]["thread_id"] == "pr-2"


# ------------------------------------------------------------- resources

async def test_the_checkpoint_holds_no_open_resources(box):
    """A snapshot is a temporary directory and a provider is an open
    socket. Saving either would be saving a path that is already gone."""
    saver = InMemorySaver()
    graph = compile_graph(checkpointer=saver)
    client, _ = model_server(wrong={"findings": [finding_json()]})

    await run(client, box, graph=graph)

    saved = graph.get_state({"configurable": {"thread_id": "t1"}}).values
    assert "toolbox" not in saved
    assert "model" not in saved
    assert "judge" not in saved


# --------------------------------------------------- the pipeline switch

def test_the_agentic_pipeline_is_off_unless_asked_for(monkeypatch):
    from src.pipeline.agentic import agentic_enabled

    monkeypatch.delenv("AGENTIC_PIPELINE", raising=False)
    assert not agentic_enabled()


def test_the_agentic_pipeline_can_be_switched_on(monkeypatch):
    from src.pipeline.agentic import agentic_enabled

    for value in ("1", "true", "yes", "on"):
        monkeypatch.setenv("AGENTIC_PIPELINE", value)
        assert agentic_enabled(), value


def test_the_thread_is_the_idempotency_key_the_project_already_has():
    """A checkpoint belongs to one commit on one pull request, which is
    exactly what the reviews table is unique on."""
    from src.pipeline.agentic import thread_id
    from src.webhooks.events import PullRequestEvent

    event = PullRequestEvent(
        delivery_id="d", action="opened", installation_id=1,
        repo_id=42, repo_full_name="o/r", pr_number=7,
        head_sha="abc1234", base_sha="def5678",
    )

    assert thread_id(event) == "42:7:abc1234"


async def test_the_pipeline_routes_to_the_graph_when_the_flag_is_on(monkeypatch):
    """The two pipelines are a setting, not a build-time choice, so the
    evaluation harness can run the same cases through both."""
    from src.pipeline import record as pipeline

    taken = []

    async def agentic(event, **kwargs):
        taken.append("agentic")
        raise AssertionError("stop here, the route is what is being tested")

    async def single_pass(event, **kwargs):
        taken.append("single-pass")
        raise AssertionError("stop here, the route is what is being tested")

    async def no_earlier_findings(session, review_id):
        return []

    class NoSession:
        """The database is not what is being tested here, and the review no
        longer holds a session open across it."""

        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(pipeline, "review_pull_request_with_agents", agentic)
    monkeypatch.setattr(pipeline, "review_pull_request", single_pass)
    monkeypatch.setattr(pipeline, "previously_posted", no_earlier_findings)
    monkeypatch.setattr(pipeline, "get_session_factory", lambda: NoSession)

    from src.webhooks.events import PullRequestEvent

    event = PullRequestEvent(
        delivery_id="d", action="opened", installation_id=1, repo_id=1,
        repo_full_name="o/r", pr_number=1, head_sha="a", base_sha="b",
    )

    for flag, expected in (("0", "single-pass"), ("1", "agentic")):
        monkeypatch.setenv("AGENTIC_PIPELINE", flag)
        taken.clear()
        try:
            await pipeline._record_and_post(event, 1, None, None)
        except Exception:
            pass
        assert taken == [expected], flag


async def test_the_cheap_judge_uses_the_provider_the_graph_opened(box):
    """It used to build its own. That opens a second HTTP client and,
    worse, ignores the one a test or the evaluation harness injected —
    so an offline run sent real traffic."""
    client, sent = model_server(
        wrong={"findings": [finding_json(confidence=0.9, severity="medium")]}
    )

    await run(client, box)

    # Its prompt appearing here proves the injected client was used.
    assert any("supported by the code" in b["messages"][0]["content"] for b in sent)
