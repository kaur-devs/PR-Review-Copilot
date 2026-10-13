from __future__ import annotations

from src.context.bundle import ContextBundle
from src.diff.parse import FileDiff
from src.review.findings import Finding

SYSTEM = """You check whether review findings are supported by the code.

Another reviewer produced these findings. Some are real. Some are plausible
guesses or restatements of what the code does. Your job is to tell them apart.

You are shown the changed code, and sometimes other code from the repository.
You are NOT shown the whole repository. That distinction matters.

Reply with JSON only:

{"verdicts": [{"index": <number>, "verdict": "<supported|contradicted|unverifiable>", "reason": "<one sentence>"}]}

Use "supported" when you can point at specific code that makes the finding
true.

Use "contradicted" when the code you have been shown proves the finding
wrong: the code does not do what it claims, or the problem is already
handled in code you can see.

Use "unverifiable" when you simply cannot tell from what you were given. In
particular, use it when a finding says a change will affect code elsewhere
and that code was not shown to you. Not seeing the callers is not evidence
that there are none.

Choosing "contradicted" deletes the finding, so a developer never sees it.
Only choose it when you have positive evidence, never because something is
absent from what you were shown.

Return a verdict for every index you are given. Do not invent indexes."""


def _diff_block(diffs: list[FileDiff]) -> str:
    blocks = []
    for diff in diffs:
        lines = [f"FILE: {diff.filename}"]
        for hunk in diff.hunks:
            for line in hunk.lines:
                marker = "+" if line.is_added else "-" if line.is_removed else " "
                number = line.new_line or line.old_line or 0
                lines.append(f"{number:>5} {marker}{line.content}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _context_block(context: ContextBundle) -> str:
    if not context.items:
        return ""

    blocks = ["ELSEWHERE IN THE REPOSITORY:"]
    for item in context.items:
        blocks.append(
            f"\n{item.file}, lines {item.start_line}-{item.end_line} "
            f"(uses {item.changed_symbol})\n{item.snippet}"
        )
    return "\n".join(blocks)


def build_user_prompt(
    findings: list[Finding], diffs: list[FileDiff], context: ContextBundle
) -> str:
    claims = []
    for index, finding in enumerate(findings):
        claims.append(
            f"[{index}] {finding.file}:{finding.line} "
            f"({finding.severity}/{finding.category})\n"
            f"    claim: {finding.message}\n"
            f"    reasoning: {finding.rationale}"
        )

    parts = ["CHANGED CODE:", _diff_block(diffs)]

    elsewhere = _context_block(context)
    if elsewhere:
        parts.extend(["", elsewhere])

    parts.extend(["", "FINDINGS TO CHECK:", "\n\n".join(claims)])
    return "\n".join(parts)
