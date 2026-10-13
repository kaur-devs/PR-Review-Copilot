from __future__ import annotations

from src.context.bundle import ContextBundle
from src.diff.parse import FileDiff
from src.review.findings import Finding

SYSTEM = """You check whether review findings are actually supported by the code.

Another reviewer produced these findings. Some are real. Some are plausible
guesses, restatements of what the code does, or concerns about situations the
code does not actually create. Your job is to tell them apart.

Reply with JSON only:

{"verdicts": [{"index": <number>, "supported": true/false, "reason": "<one sentence>"}]}

Mark a finding supported when you can point at the specific code that makes
it true.

Mark it unsupported when:
- the code does not do what the finding claims
- the concern is hypothetical and nothing in the code shown creates it
- it describes what the code does without identifying a problem
- the problem it describes is already handled elsewhere in the code shown

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
