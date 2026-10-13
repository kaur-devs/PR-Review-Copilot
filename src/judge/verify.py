from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace

from src.context.bundle import ContextBundle
from src.diff.parse import FileDiff
from src.judge.prompts import SYSTEM, build_user_prompt
from src.review.findings import Finding
from src.review.provider import LLMError, LLMProvider

logger = logging.getLogger(__name__)

DEFAULT_JUDGE_MODEL = "qwen/qwen3.8-27b"

SUPPORTED = "supported"
CONTRADICTED = "contradicted"
UNVERIFIABLE = "unverifiable"
VERDICTS = frozenset({SUPPORTED, CONTRADICTED, UNVERIFIABLE})

# A finding the judge could not check is kept, but trusted less. At the gate's
# floor of 0.6 this means a strong finding still posts and a middling one does
# not, rather than everything surviving or nothing.
UNVERIFIABLE_PENALTY = 0.7


def judge_enabled() -> bool:
    return os.getenv("JUDGE_ENABLED", "1").lower() not in ("0", "false", "no", "off")


def judge_model_name() -> str:
    return os.getenv("LLM_JUDGE_MODEL", DEFAULT_JUDGE_MODEL)


@dataclass
class JudgeResult:
    kept: list[Finding] = field(default_factory=list)
    rejected: list[tuple[Finding, str]] = field(default_factory=list)
    unverified: list[tuple[Finding, str]] = field(default_factory=list)
    ran: bool = True

    @property
    def summary(self) -> str:
        if not self.ran:
            return "not run"
        return (
            f"{len(self.kept) - len(self.unverified)} supported, "
            f"{len(self.unverified)} unverifiable, "
            f"{len(self.rejected)} contradicted"
        )


def _read_verdicts(answer: dict, count: int) -> dict[int, tuple[str, str]]:
    verdicts: dict[int, tuple[str, str]] = {}

    raw = answer.get("verdicts")
    if not isinstance(raw, list):
        logger.warning("the judge did not return a verdicts list")
        return verdicts

    for entry in raw:
        if not isinstance(entry, dict):
            continue

        try:
            index = int(entry.get("index"))
        except (TypeError, ValueError):
            continue

        if not 0 <= index < count:
            logger.info("the judge returned an index we did not ask about: %s", index)
            continue

        verdict = entry.get("verdict")

        # Older wording, and a safety net if the model answers yes or no.
        if verdict not in VERDICTS:
            if "supported" in entry:
                verdict = SUPPORTED if entry["supported"] else CONTRADICTED
            else:
                logger.info("unreadable verdict for index %s: %r", index, verdict)
                verdict = UNVERIFIABLE

        verdicts[index] = (verdict, str(entry.get("reason", ""))[:200])

    return verdicts


async def judge_findings(
    findings: list[Finding],
    diffs: list[FileDiff],
    context: ContextBundle,
    *,
    client=None,
) -> JudgeResult:
    """Ask a second model whether each finding is really supported.

    A different model from the one that generated them, because a model asked
    to check its own work mostly agrees with itself.

    Fails open: if the judge errors or answers unusably, every finding is
    kept. Silently discarding real findings is worse than keeping a weak one.
    """
    if not findings:
        return JudgeResult(ran=False)

    if not judge_enabled():
        logger.info("judge is switched off, keeping all %s findings", len(findings))
        return JudgeResult(kept=list(findings), ran=False)

    try:
        provider = LLMProvider.from_env(client=client, model=judge_model_name())
    except LLMError as error:
        logger.warning("judge unavailable, keeping everything: %s", error)
        return JudgeResult(kept=list(findings), ran=False)

    try:
        async with provider as model:
            answer = await model.complete_json(
                SYSTEM, build_user_prompt(findings, diffs, context)
            )
    except LLMError as error:
        logger.warning("judge failed, keeping everything: %s", error)
        return JudgeResult(kept=list(findings), ran=False)

    verdicts = _read_verdicts(answer, len(findings))

    if not verdicts:
        logger.warning("the judge returned nothing usable, keeping everything")
        return JudgeResult(kept=list(findings), ran=False)

    result = JudgeResult()
    for index, finding in enumerate(findings):
        verdict, reason = verdicts.get(index, (SUPPORTED, "the judge did not say"))

        if verdict == CONTRADICTED:
            result.rejected.append((finding, reason))
            continue

        if verdict == UNVERIFIABLE:
            weakened = replace(
                finding,
                confidence=round(finding.confidence * UNVERIFIABLE_PENALTY, 2),
            )
            result.kept.append(weakened)
            result.unverified.append((weakened, reason))
            continue

        result.kept.append(finding)

    logger.info(
        "judge (%s): %s", judge_model_name(), result.summary
    )
    return result
