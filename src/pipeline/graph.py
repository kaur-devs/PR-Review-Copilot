"""The agentic review, as a checkpointed state graph.

Design note: docs/30_Design_Notes.md#src-graph
"""

from __future__ import annotations

import logging
from typing import Any

from typing_extensions import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from src.agents.specialists import review as run_specialists
from src.agents.summary import summarise
from src.agents.triage import specialists_for
from src.agents.tools import Toolbox
from src.classification.classify import classify
from src.common import trace
from src.context.bundle import ContextBundle
from src.diff.parse import FileDiff
from src.filter.gate import GateResult, confidence_gate, severity_gate
from src.filter.repeat import SaidBefore, drop_repeats
from src.judge.investigate import investigate, merge, split
from src.judge.verify import judge_findings
from src.review.findings import Finding
from src.llm.provider import LLMProvider

logger = logging.getLogger(__name__)


class ReviewState(TypedDict, total=False):
    """What flows between stages, and what a checkpoint holds."""

    # Set before the graph runs.
    diffs: list[FileDiff]
    context: ContextBundle
    already_said: list[SaidBefore]
    skipped_files: dict[str, str]

    # Decided by triage.
    classifications: dict[str, Any]
    wake: list[str]

    # One node writes this; a reducer would double it on a resumed run.
    candidates: list[Finding]

    # Narrowed stage by stage.
    fresh: list[Finding]
    repeats: list[tuple[Finding, SaidBefore]]
    worth_verifying: list[Finding]
    verified: list[Finding]
    to_post: list[Finding]
    held: list[tuple[Finding, str]]
    summary: str

    # Reported back, not used by the graph.
    turns: int
    stopped_early: str


def _resources(config: RunnableConfig) -> tuple[LLMProvider, LLMProvider, Toolbox]:
    """What the nodes need and the checkpoint must never hold."""
    bag = config["configurable"]
    return bag["model"], bag["judge"], bag["toolbox"]


# --------------------------------------------------------------- the nodes


async def triage(state: ReviewState, config: RunnableConfig) -> dict:
    model, _, _ = _resources(config)
    diffs = state["diffs"]

    classifications = await classify(model, diffs, state["context"].symbols)
    wake = specialists_for(classifications)

    trace.section("TRIAGE", ", ".join(wake) or "nothing worth reviewing")
    for diff in diffs:
        label = classifications.get(diff.filename)
        if label:
            trace.item(f"{diff.filename:<44} {label.kind:<12} ({label.source})")

    return {"classifications": classifications, "wake": list(wake)}


async def specialists(state: ReviewState, config: RunnableConfig) -> dict:
    """Both reviewers, concurrently, writing into one candidate list."""
    model, _, toolbox = _resources(config)
    wake = tuple(state.get("wake", ()))

    if not wake:
        return {"candidates": []}

    outcome = await run_specialists(
        model, toolbox, state["diffs"], state["context"], only=wake
    )

    stopped = ", ".join(
        f"{name}:{run.stopped_because}"
        for name, run in outcome.runs.items()
        if run.stopped_because != "answered"
    )

    return {
        "candidates": outcome.findings,
        "turns": outcome.turns,
        "stopped_early": stopped,
    }


def repeats(state: ReviewState) -> dict:
    """Drop what was already said, before anything is spent verifying it.

    This runs before the gates and before the verifier. A second push
    that finds the same problems should cost the specialists only, not a
    full verification pass to discover there is nothing new to say.
    """
    candidates = state.get("candidates", [])
    already = state.get("already_said", [])

    outcome = drop_repeats(candidates, already)

    if not already:
        return {"fresh": outcome.fresh, "repeats": []}

    trace.section(
        "REPEATS", f"{len(outcome.fresh)} new · {len(outcome.repeats)} already said"
    )
    for finding, earlier in outcome.repeats:
        trace.item(
            f"said before  {finding.file}:{finding.line} "
            f"({finding.category} at line {earlier.line} in an earlier review)"
        )

    return {"fresh": outcome.fresh, "repeats": outcome.repeats}


def severity(state: ReviewState) -> dict:
    """Drop what can never be posted, before paying to verify it."""
    candidates = state.get("fresh", state.get("candidates", []))
    result = severity_gate(candidates)

    for finding, reason in result.hold:
        trace.item(f"dropped  {finding.file}:{finding.line}  ({reason})")

    return {"worth_verifying": result.post, "held": result.hold}


async def verify(state: ReviewState, config: RunnableConfig) -> dict:
    """Check the risky findings properly, the rest cheaply."""
    _, judge, toolbox = _resources(config)
    candidates = state.get("worth_verifying", [])

    if not candidates:
        return {"verified": []}

    risky, routine = split(candidates)

    agentic, runs = await investigate(judge, toolbox, risky, state["diffs"])
    cheap = await judge_findings(
        routine, state["diffs"], state["context"], provider=judge
    )
    result = merge(agentic, cheap)

    trace.section(
        "VERIFY",
        f"{len(risky)} investigated in {len(runs)} file loop(s), "
        f"{len(routine)} checked cheaply · {result.summary}",
    )
    for finding, reason in result.rejected:
        trace.item(f"contradicted  {finding.file}:{finding.line}  {reason}")
    for finding, reason in result.unverified:
        trace.item(f"unverifiable  {finding.file}:{finding.line}  {reason}")

    return {
        "verified": result.kept,
        "turns": state.get("turns", 0) + sum(run.turn_count for run in runs.values()),
    }


def confidence(state: ReviewState) -> dict:
    """The last gate. Runs after the judge, which is what moves a score."""
    result = confidence_gate(state.get("verified", []))

    for finding, reason in result.hold:
        trace.item(f"held  {finding.file}:{finding.line}  ({reason})")

    return {
        "to_post": result.post,
        "held": state.get("held", []) + result.hold,
    }


async def summary(state: ReviewState, config: RunnableConfig) -> dict:
    """The opening comment, written only when there is a comment to open.

    Nothing is posted when the gates keep nothing, so a summary written
    then is a model call whose output no one ever sees. On a free tier
    that is a real share of the day spent on silent reviews.
    """
    if not state.get("to_post"):
        return {"summary": ""}

    model, _, _ = _resources(config)

    text = await summarise(
        model,
        state["diffs"],
        state.get("to_post", []),
        state.get("classifications", {}),
        state.get("skipped_files", {}),
    )

    trace.section("SUMMARY", "written" if text else "none")
    if text:
        trace.snippet(text)

    return {"summary": text}


# -------------------------------------------------------------- the edges


def _should_review(state: ReviewState) -> str:
    """A change with nothing worth reviewing skips straight to the end."""
    return "specialists" if state.get("wake") else "done"


def _anything_to_verify(state: ReviewState) -> str:
    return "verify" if state.get("worth_verifying") else "confidence"


def build() -> StateGraph:
    """The graph, before a checkpointer is attached."""
    builder = StateGraph(ReviewState)

    builder.add_node("triage", triage)
    builder.add_node("specialists", specialists)
    builder.add_node("repeats", repeats)
    builder.add_node("severity", severity)
    builder.add_node("verify", verify)
    builder.add_node("confidence", confidence)
    builder.add_node("summary", summary)

    builder.add_edge(START, "triage")
    builder.add_conditional_edges(
        "triage", _should_review, {"specialists": "specialists", "done": END}
    )
    builder.add_edge("specialists", "repeats")
    builder.add_edge("repeats", "severity")
    builder.add_conditional_edges(
        "severity", _anything_to_verify,
        {"verify": "verify", "confidence": "confidence"},
    )
    builder.add_edge("verify", "confidence")
    builder.add_edge("confidence", "summary")
    builder.add_edge("summary", END)

    return builder


def compile_graph(checkpointer=None):
    """Compile once. A checkpointer makes a crashed run resumable."""
    return build().compile(checkpointer=checkpointer)


__all__ = ["ReviewState", "build", "compile_graph"]
