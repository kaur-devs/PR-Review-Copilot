from __future__ import annotations

from src.classification.kinds import API, CONFIG, DEPENDENCY, LOGIC, SCHEMA, TEST
from src.context.bundle import ContextBundle
from src.diff.parse import FileDiff
from src.review.findings import CATEGORIES, SEVERITIES

BASE_SYSTEM = f"""You review pull requests. You are shown a diff, and sometimes
code elsewhere in the repository that uses what changed.

Report only problems you can point at in the code in front of you. If you are
not sure, say nothing. A reviewer who invents problems gets muted.

Reply with JSON only, in this shape:

{{"findings": [
  {{"file": "<path from the diff>",
    "line": <a line number that appears in the diff>,
    "severity": "<{'|'.join(SEVERITIES)}>",
    "category": "<{'|'.join(CATEGORIES)}>",
    "message": "<one sentence, what is wrong>",
    "rationale": "<one or two sentences, why, pointing at the code>",
    "confidence": <0.0 to 1.0>}}
]}}

Return {{"findings": []}} when the change looks fine.

Rules:
- line must be a line shown in the diff. Anything else cannot be posted.
- Never comment on style, formatting or naming preferences.
- Never restate what the code does.
- One finding per problem. Do not repeat yourself."""

QUESTIONS = {
    API: """This change touches a route, endpoint or public interface. Ask:
- Does the request or response shape change in a way that breaks callers?
- Is a required field added without a default?
- Is input validated before it is used?
- Are errors returned, or do they escape as a 500?""",
    SCHEMA: """This change touches the database. Ask:
- Does a column become non-nullable without a default, on a table with rows?
- Is a column or table removed that code still reads?
- Is an index missing on something that will be queried?
- Can this migration be undone?""",
    DEPENDENCY: """This change touches dependencies. Ask:
- Is a version pin loosened or removed?
- Is a major version crossed?
- Is a package added that duplicates one already present?""",
    CONFIG: """This change touches configuration. Ask:
- Is a secret written in plain text?
- Does a new setting have no default, so existing deployments break?
- Does a changed default alter behaviour silently?""",
    LOGIC: """This change touches logic. Ask:
- Can a value be None, empty or zero where the code assumes otherwise?
- Are errors swallowed?
- Is a loop or lookup doing work that grows faster than it needs to?
- Does the change contradict what the surrounding code expects?""",
    TEST: """This change touches tests. Ask:
- Does a test assert nothing meaningful?
- Was a test weakened or skipped rather than fixed?""",
}


def build_system_prompt(kind: str) -> str:
    questions = QUESTIONS.get(kind)
    if not questions:
        return BASE_SYSTEM
    return f"{BASE_SYSTEM}\n\n{questions}"


def _diff_block(diff: FileDiff) -> str:
    lines = [f"FILE: {diff.filename}"]
    for hunk in diff.hunks:
        lines.append(f"@@ lines {hunk.new_start}-{hunk.new_start + hunk.new_length - 1} @@")
        for line in hunk.lines:
            marker = "+" if line.is_added else "-" if line.is_removed else " "
            number = line.new_line if line.new_line else line.old_line
            lines.append(f"{number:>5} {marker}{line.content}")
    return "\n".join(lines)


def _context_block(context: ContextBundle, files: set[str]) -> str:
    relevant = [item for item in context.items if item.changed_symbol]
    if not relevant:
        return ""

    blocks = ["ELSEWHERE IN THE REPOSITORY, code that uses what changed:"]
    for item in relevant:
        blocks.append(
            f"\n{item.file}, lines {item.start_line}-{item.end_line} "
            f"(uses {item.changed_symbol}, {item.relation}, confidence {item.confidence})\n"
            f"{item.snippet}"
        )
    return "\n".join(blocks)


def build_user_prompt(diffs: list[FileDiff], context: ContextBundle) -> str:
    parts = ["CHANGED CODE:"]
    parts.extend(_diff_block(diff) for diff in diffs)

    elsewhere = _context_block(context, {diff.filename for diff in diffs})
    if elsewhere:
        parts.append("")
        parts.append(elsewhere)

    return "\n\n".join(parts)
