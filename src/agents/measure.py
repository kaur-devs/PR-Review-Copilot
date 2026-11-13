"""Measure how many turns a specialist actually takes.

Design note: docs/30_Design_Notes.md#src-agents-measure
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.agents.prompts import SPECIALISTS
from src.agents.specialists import review
from src.agents.tools import Toolbox
from src.context.gather import gather_context
from src.context.snapshot import open_snapshot
from src.diff.files import ChangedFileSet, fetch_changed_files
from src.diff.parse import parse_changed_files
from src.diff.skip import worth_reviewing
from src.evaluation.cases import load_cases
from src.github.client import GitHubClient
from src.llm.provider import LLMNotConfigured, LLMProvider, quota

logger = logging.getLogger(__name__)

REPORTS = Path("eval/reports")

# What docs/29 assumed, printed beside the measurement so the gap shows.
ASSUMED_TURNS_PER_AGENT = 3
ASSUMED_TOKENS_PER_REVIEW = 101_500


@dataclass
class AgentMeasurement:
    specialist: str
    turns: int
    tool_calls: int
    tools_used: list[str]
    prompt_tokens: int
    completion_tokens: int
    stopped_because: str
    findings: int

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class PRMeasurement:
    label: str
    reviewable_files: int = 0
    changed_lines: int = 0
    context_items: int = 0
    agents: list[AgentMeasurement] = field(default_factory=list)
    seconds: float = 0.0
    error: str | None = None

    @property
    def turns(self) -> int:
        return sum(agent.turns for agent in self.agents)

    @property
    def tokens(self) -> int:
        return sum(agent.tokens for agent in self.agents)

    @property
    def findings(self) -> int:
        return sum(agent.findings for agent in self.agents)


async def measure_pull_request(
    label: str, repo: str, pr: int, installation_id: int
) -> PRMeasurement:
    started = asyncio.get_running_loop().time()
    result = PRMeasurement(label=label)

    try:
        async with GitHubClient(installation_id) as github:
            pull_request = await github.get(f"/repos/{repo}/pulls/{pr}")
            head_sha = pull_request["head"]["sha"]
            file_set = await fetch_changed_files(github, repo, pr)

        keep, _ = worth_reviewing(file_set.files)
        diffs = [
            diff
            for diff in parse_changed_files(ChangedFileSet(files=keep))
            if diff.parsed
        ]
        result.reviewable_files = len(diffs)
        result.changed_lines = sum(len(diff.added_line_numbers) for diff in diffs)

        if not diffs:
            result.error = "nothing reviewable"
            return result

        context = await gather_context(repo, head_sha, installation_id, diffs)
        result.context_items = len(context.items)

        # Held open for as long as the agents are looking.
        async with open_snapshot(repo, head_sha, installation_id) as snapshot:
            toolbox = Toolbox(snapshot)
            async with LLMProvider.from_env() as model:
                outcome = await review(model, toolbox, diffs, context)

        for specialist in SPECIALISTS:
            run = outcome.runs.get(specialist)
            if run is None:
                continue
            used = [name for turn in run.turns for name in turn.tools_called]
            result.agents.append(
                AgentMeasurement(
                    specialist=specialist,
                    turns=run.turn_count,
                    tool_calls=run.tool_calls,
                    tools_used=sorted(set(used)),
                    prompt_tokens=sum(turn.prompt_tokens for turn in run.turns),
                    completion_tokens=sum(turn.completion_tokens for turn in run.turns),
                    stopped_because=run.stopped_because,
                    findings=len(outcome.found_by.get(specialist, [])),
                )
            )

    except LLMNotConfigured as error:
        result.error = str(error)
    except Exception as error:  # noqa: BLE001 - one bad PR must not end the run
        logger.exception("%s failed", label)
        result.error = f"{type(error).__name__}: {error}"[:200]

    result.seconds = asyncio.get_running_loop().time() - started
    return result


def _report(results: list[PRMeasurement]) -> dict:
    worked = [r for r in results if not r.error and r.agents]
    per_agent = [agent for r in worked for agent in r.agents]

    def summarise(numbers: list[float]) -> dict:
        if not numbers:
            return {}
        ordered = sorted(numbers)
        return {
            "min": ordered[0],
            "median": statistics.median(ordered),
            "mean": round(statistics.fmean(ordered), 2),
            "max": ordered[-1],
        }

    turns = [agent.turns for agent in per_agent]
    tools_used: dict[str, int] = {}
    stops: dict[str, int] = {}
    for agent in per_agent:
        for name in agent.tools_used:
            tools_used[name] = tools_used.get(name, 0) + 1
        stops[agent.stopped_because] = stops.get(agent.stopped_because, 0) + 1

    return {
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pull_requests": len(results),
        "usable": len(worked),
        "assumed_turns_per_agent": ASSUMED_TURNS_PER_AGENT,
        "turns_per_agent": summarise(turns),
        "tool_calls_per_agent": summarise([a.tool_calls for a in per_agent]),
        "tokens_per_agent": summarise([a.tokens for a in per_agent]),
        "tokens_per_review": summarise([r.tokens for r in worked]),
        "seconds_per_review": summarise([round(r.seconds, 1) for r in worked]),
        "stopped_because": stops,
        "tools_used": dict(sorted(tools_used.items(), key=lambda kv: -kv[1])),
        "runs": [asdict(r) for r in results],
    }


def _print(report: dict) -> None:
    turns = report["turns_per_agent"]
    tokens = report["tokens_per_review"]

    print()
    print("=" * 62)
    print(f"  {report['usable']} of {report['pull_requests']} pull requests measured")
    print("=" * 62)

    if not turns:
        print("  nothing usable. See the errors above.")
        return

    assumed = report["assumed_turns_per_agent"]
    print(f"\n  TURNS PER AGENT      assumed {assumed}")
    print(f"    min {turns['min']}   median {turns['median']}   "
          f"mean {turns['mean']}   max {turns['max']}")

    verdict = (
        "cheaper and faster than planned" if turns["mean"] < assumed - 0.5
        else "as planned" if turns["mean"] <= assumed + 0.5
        else "MORE than planned - the design needs cutting back"
    )
    print(f"    -> {verdict}")

    if tokens:
        print(f"\n  TOKENS PER REVIEW    assumed ~{ASSUMED_TOKENS_PER_REVIEW:,} "
              f"(specialists are about half of that)")
        print(f"    min {tokens['min']:,}   median {tokens['median']:,.0f}   "
              f"max {tokens['max']:,}")
        print(f"    free tier at 200,000/day -> "
              f"{200_000 / max(tokens['mean'], 1):.1f} reviews per day")

    print(f"\n  WHY THEY STOPPED")
    for reason, count in report["stopped_because"].items():
        print(f"    {reason:<22}{count}")

    print(f"\n  TOOLS REACHED FOR")
    for name, count in report["tools_used"].items():
        print(f"    {name:<22}{count}")

    print(f"\n  PER PULL REQUEST")
    for run in report["runs"]:
        if run["error"]:
            print(f"    {run['label']:<28}FAILED  {run['error']}")
            continue
        per = "  ".join(
            f"{a['specialist']}:{a['turns']}t/{a['tool_calls']}c" for a in run["agents"]
        )
        print(f"    {run['label']:<28}{per:<28}{run['seconds']:>6.1f}s")
    print()


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, help="eval/cases.json")
    parser.add_argument("--repo", help="owner/name, for a single pull request")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--installation", type=int)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--out", type=Path, default=REPORTS / "turns.json")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    targets: list[tuple[str, str, int, int]] = []
    if args.cases:
        for case in load_cases(args.cases)[: args.limit]:
            targets.append((case.id, case.repo, case.pr, case.installation_id))
    elif args.repo and args.pr and args.installation:
        targets.append((f"{args.repo}#{args.pr}", args.repo, args.pr, args.installation))
    else:
        parser.error("give either --cases or all of --repo --pr --installation")

    print(f"measuring {len(targets)} pull request(s), one at a time\n")

    results = []
    for label, repo, pr, installation in targets:
        print(f"  {label} …", flush=True)
        results.append(await measure_pull_request(label, repo, pr, installation))

    report = _report(results)
    report["quota"] = quota.as_json()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))

    _print(report)
    print(f"  written to {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
