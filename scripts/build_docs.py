"""Build docs/Complete_documentation.md from the numbered documents.

    .venv/bin/python scripts/build_docs.py

The bundle is one file for reading end to end or handing in. It is generated,
never edited: an edited copy drifts from the documents it was made of, which
is what happened to the last one.
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"
OUTPUT = DOCS / "Complete_documentation.md"
NUMBERED = re.compile(r"^\d\d_")


def title_of(path: Path) -> str:
    for line in path.read_text().splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def anchor(title: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    return re.sub(r"[\s_]+", "-", slug).strip("-")


def main() -> int:
    sources = sorted(p for p in DOCS.glob("*.md") if NUMBERED.match(p.name))
    if not sources:
        print("no numbered documents found", file=sys.stderr)
        return 1

    titles = [title_of(p) for p in sources]

    parts = [
        "# PR Review Copilot — Complete Documentation",
        "",
        f"Generated from the {len(sources)} documents in `docs/` on "
        f"{date.today().isoformat()}.",
        "Edit those, then run `.venv/bin/python scripts/build_docs.py`.",
        "",
        "## Contents",
        "",
    ]
    for source, title in zip(sources, titles):
        parts.append(f"{int(source.name[:2])}. [{title}](#{anchor(title)}) — `{source.name}`")

    for source, title in zip(sources, titles):
        parts += ["", "---", "", source.read_text().rstrip()]

    OUTPUT.write_text("\n".join(parts) + "\n")
    print(f"{OUTPUT.relative_to(DOCS.parent)}: {len(sources)} documents, "
          f"{len(OUTPUT.read_text().splitlines())} lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
