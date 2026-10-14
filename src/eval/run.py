from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from src.eval.cases import Case, load_cases
from src.eval.score import CaseScore, RunScore, score_case
from src.github.client import GitHubClient
from src.review_run import review_pull_request
from src.webhooks.events import PullRequestEvent

logger = logging.getLogger(__name__)

DEFAULT_CASES = Path("eval/cases.json")
REPORTS = Path("eval/reports")


@dataclass(frozen=True)
class Ablation:
    """One configuration to measure. H0 is everything switched on."""

    label: str
    settings: dict[str, str]


ABLATIONS = {
    "H0": Ablation("H0 full pipeline", {}),
    "H1": Ablation("H1 without connected-file context", {"CONTEXT_ENABLED": "0"}),
    "H2": Ablation("H2 without the judge", {"JUDGE_ENABLED": "0"}),
    "H3": Ablation("H3 without per-kind templates", {"TEMPLATES_ENABLED": "0"}),
}


async def _event_for(case: Case) -> PullRequestEvent:
    async with GitHubClient(case.installation_id) as github:
        pull_request = await github.get(f"/repos/{case.repo}/pulls/{case.pr}")
        repository = await github.get(f"/repos/{case.repo}")

    return PullRequestEvent(
        delivery_id=f"eval-{case.id}",
        action="opened",
        installation_id=case.installation_id,
        repo_id=repository["id"],
        repo_full_name=case.repo,
        pr_number=case.pr,
        head_sha=pull_request["head"]["sha"],
        base_sha=pull_request["base"]["sha"],
    )


async def run_case(case: Case) -> CaseScore:
    try:
        event = await _event_for(case)
        outcome = await review_pull_request(event)
    except Exception as error:
        logger.exception("case %s failed", case.id)
        return CaseScore(case_id=case.id, error=str(error)[:200])

    score = score_case(case, outcome.findings, outcome.context)
    score.model_calls = outcome.model_calls
    score.seconds = outcome.seconds
    return score


async def run_ablation(cases: list[Case], ablation: Ablation) -> RunScore:
    previous = {name: os.environ.get(name) for name in ablation.settings}
    os.environ.update(ablation.settings)

    try:
        run = RunScore(label=ablation.label)
        for case in cases:
            print(f"  {case.id} ...", end="", flush=True)
            score = await run_case(case)
            run.cases.append(score)
            if score.error:
                print(f" error: {score.error[:60]}")
            else:
                print(
                    f" {len(score.matched)}/{len(score.matched) + len(score.missed)} found"
                    f", {len(score.extra)} extra"
                )
        return run
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _percent(value: float | None) -> str:
    return f"{value:.0%}" if value is not None else "n/a"


def print_report(runs: list[RunScore]) -> None:
    print()
    print("=" * 78)
    print(f"  {'configuration':<34} {'recall':>8} {'precision':>10} "
          f"{'F1':>6} {'context':>8} {'calls':>6}")
    print("=" * 78)
    for run in runs:
        print(
            f"  {run.label:<34} {_percent(run.recall):>8} "
            f"{_percent(run.precision):>10} {_percent(run.f1):>6} "
            f"{_percent(run.context_recall):>8} {run.model_calls:>6}"
        )
    print("=" * 78)

    for run in runs:
        missed = [m for case in run.cases for m in case.missed]
        if missed:
            print(f"\n  {run.label} missed:")
            for item in missed:
                print(f"     {item}")
    print()


def as_json(runs: list[RunScore]) -> dict:
    return {
        "runs": [
            {
                "label": run.label,
                "recall": run.recall,
                "precision": run.precision,
                "f1": run.f1,
                "context_recall": run.context_recall,
                "model_calls": run.model_calls,
                "errors": run.errors,
                "cases": [
                    {
                        "id": case.case_id,
                        "matched": len(case.matched),
                        "missed": [str(m) for m in case.missed],
                        "extra": [f"{f.file}:{f.line} {f.message}" for f in case.extra],
                        "context_found": case.context_found,
                        "context_missed": case.context_missed,
                        "model_calls": case.model_calls,
                        "seconds": round(case.seconds, 1),
                        "error": case.error,
                    }
                    for case in run.cases
                ],
            }
            for run in runs
        ]
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument(
        "--only", nargs="*", choices=sorted(ABLATIONS), default=["H0"],
        help="which configurations to run (default: H0)",
    )
    parser.add_argument("--case", default=None, help="run a single case by id")
    parser.add_argument(
        "--out", type=Path, default=None,
        help="write JSON here (default: a timestamped file in eval/reports)",
    )
    parser.add_argument(
        "--no-save", action="store_true", help="do not write a report file"
    )
    args = parser.parse_args()

    if not args.cases.exists():
        print(f"No cases file at {args.cases}")
        return 1

    cases = load_cases(args.cases)
    if args.case:
        cases = [c for c in cases if c.id == args.case]
        if not cases:
            print(f"No case called {args.case}")
            return 1

    runs = []
    for name in args.only:
        ablation = ABLATIONS[name]
        print(f"\n{ablation.label}  ({len(cases)} cases)")
        runs.append(await run_ablation(cases, ablation))

    print_report(runs)

    if not args.no_save:
        from datetime import datetime, timezone

        out = args.out
        if out is None:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
            REPORTS.mkdir(parents=True, exist_ok=True)
            out = REPORTS / f"{stamp}-{'-'.join(args.only).lower()}.json"

        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(as_json(runs), indent=2) + "\n")
        print(f"  report written to {out}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
