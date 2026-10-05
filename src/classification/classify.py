from __future__ import annotations

import logging

from src.classification.kinds import (
    CHANGE_KINDS,
    FALLBACK,
    LOGIC,
    MODEL_BASED,
    Classification,
    classify_by_rules,
)
from src.classification.prompts import SYSTEM, build_user_prompt
from src.context.symbols import Symbol
from src.diff.parse import FileDiff
from src.review.provider import LLMError, LLMProvider

logger = logging.getLogger(__name__)


def _fallback(path: str, reason: str) -> Classification:
    return Classification(path, LOGIC, FALLBACK, reason)


def _read_model_answer(
    answer: dict, expected: set[str]
) -> dict[str, Classification]:
    classified: dict[str, Classification] = {}

    for entry in answer.get("files", []):
        if not isinstance(entry, dict):
            continue

        path = entry.get("file")
        kind = entry.get("kind")

        if path not in expected:
            logger.info("the model returned an unexpected file: %s", path)
            continue

        if kind not in CHANGE_KINDS:
            logger.info("the model returned an unknown kind for %s: %s", path, kind)
            continue

        classified[path] = Classification(
            path, kind, MODEL_BASED, str(entry.get("reason", ""))[:120]
        )

    return classified


async def classify(
    model: LLMProvider | None,
    diffs: list[FileDiff],
    symbols: list[Symbol],
) -> dict[str, Classification]:
    results: dict[str, Classification] = {}
    undecided: list[FileDiff] = []

    for diff in diffs:
        by_rule = classify_by_rules(diff.filename)
        if by_rule is not None:
            results[diff.filename] = by_rule
        else:
            undecided.append(diff)

    logger.info(
        "classification: %s settled by rules, %s need the model",
        len(results),
        len(undecided),
    )

    if not undecided:
        return results

    if model is None:
        for diff in undecided:
            results[diff.filename] = _fallback(diff.filename, "no model available")
        return results

    expected = {diff.filename for diff in undecided}

    try:
        answer = await model.complete_json(
            SYSTEM, build_user_prompt(undecided, symbols)
        )
    except LLMError as error:
        logger.warning("classification fell back to logic: %s", error)
        for path in expected:
            results[path] = _fallback(path, f"the model failed: {error}")
        return results

    from_model = _read_model_answer(answer, expected)
    results.update(from_model)

    for path in expected - from_model.keys():
        results[path] = _fallback(path, "the model left it out")

    return results
