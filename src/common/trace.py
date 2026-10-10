from __future__ import annotations

import os
import sys

WIDTH = 66
MAX_DIFF_LINES_PER_FILE = 40
MAX_SNIPPET_LINES = 14


def enabled() -> bool:
    return os.getenv("PIPELINE_TRACE", "1").lower() not in ("0", "false", "no", "off")


def _write(text: str = "") -> None:
    if enabled():
        print(text, file=sys.stderr, flush=True)


def banner(text: str) -> None:
    _write()
    _write("=" * WIDTH)
    _write(f"  {text}")
    _write("=" * WIDTH)


def footer(text: str) -> None:
    _write("-" * WIDTH)
    _write(f"  {text}")
    _write("=" * WIDTH)
    _write()


def section(label: str, summary: str = "") -> None:
    _write()
    _write(f"> {label:<22} {summary}")


def item(text: str, level: int = 1) -> None:
    _write("  " * level + text)


def block(lines: list[str], level: int = 2, limit: int | None = None) -> None:
    prefix = "  " * level + "| "
    shown = lines if limit is None else lines[:limit]
    for line in shown:
        _write(prefix + line)
    if limit is not None and len(lines) > limit:
        _write(prefix + f"... {len(lines) - limit} more lines")


def diff(file_diff) -> None:
    lines: list[str] = []
    for hunk in file_diff.hunks:
        lines.append(f"@@ lines {hunk.new_start}-{hunk.new_start + hunk.new_length - 1} @@")
        for line in hunk.lines:
            marker = "+" if line.is_added else "-" if line.is_removed else " "
            number = line.new_line or line.old_line or 0
            lines.append(f"{number:>5} {marker}{line.content}")
    block(lines, level=2, limit=MAX_DIFF_LINES_PER_FILE)


def snippet(text: str) -> None:
    block(text.splitlines(), level=2, limit=MAX_SNIPPET_LINES)


def finding(f) -> None:
    item(f"[{f.severity:<6} {f.category:<16} {f.confidence}]  {f.file}:{f.line}")
    item(f"{f.message}", level=2)
    if f.rationale and f.rationale != f.message:
        item(f"-> {f.rationale}", level=2)
    _write()


def rejected(entry: dict, reason: str) -> None:
    where = f"{entry.get('file', '?')}:{entry.get('line', '?')}"
    item(f"[rejected] {where}  {reason}")
    message = str(entry.get("message", ""))[:90]
    if message:
        item(f"would have said: {message}", level=2)
    _write()
