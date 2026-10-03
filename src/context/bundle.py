from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.context.references import Reference
from src.context.snapshot import Snapshot
from src.context.symbols import parse_source, symbol_at_line

logger = logging.getLogger(__name__)

CHARACTER_BUDGET = 12000
MAX_SNIPPET_LINES = 60
FALLBACK_PADDING = 3
MINIMUM_CONFIDENCE = 0.5

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_UNAVAILABLE = "unavailable"

DISCOVERY_IMPORT_CONFIRMED = "import_edge_confirmed"
DISCOVERY_CALL_ONLY = "call_site_only"


@dataclass(frozen=True)
class ContextItem:
    file: str
    start_line: int
    end_line: int
    snippet: str
    relation: str
    discovery: str
    confidence: float
    changed_symbol: str
    reference_lines: tuple[int, ...]

    @property
    def cost(self) -> int:
        return len(self.snippet)

    def __str__(self) -> str:
        return (
            f"{self.file}:{self.start_line}-{self.end_line} "
            f"uses {self.changed_symbol} ({self.relation}, {self.confidence})"
        )


@dataclass
class ContextBundle:
    items: list[ContextItem] = field(default_factory=list)
    dropped: list[ContextItem] = field(default_factory=list)
    problems: dict[str, str] = field(default_factory=dict)
    status: str = STATUS_OK

    def __len__(self) -> int:
        return len(self.items)

    @property
    def characters(self) -> int:
        return sum(item.cost for item in self.items)

    @property
    def files(self) -> list[str]:
        return sorted({item.file for item in self.items})


def _snippet_around(snapshot: Snapshot, path: str, line: int) -> tuple[int, int, str]:
    source = snapshot.read(path)
    lines = source.decode("utf-8", "replace").splitlines()

    tree = parse_source(source)
    symbol = symbol_at_line(tree, lines, path, line)

    if symbol is not None and not symbol.is_module_level:
        start, end = symbol.start_line, symbol.end_line
    else:
        start = max(1, line - FALLBACK_PADDING)
        end = min(len(lines), line + FALLBACK_PADDING)

    if end - start + 1 > MAX_SNIPPET_LINES:
        end = start + MAX_SNIPPET_LINES - 1
        body = lines[start - 1 : end] + ["    # ... truncated"]
    else:
        body = lines[start - 1 : end]

    return start, end, "\n".join(body)


def _merge(references: list[Reference]) -> dict[tuple[str, str], list[Reference]]:
    grouped: dict[tuple[str, str], list[Reference]] = {}
    for reference in references:
        key = (reference.file, reference.symbol.qualified_name)
        grouped.setdefault(key, []).append(reference)
    return grouped


def assemble(
    snapshot: Snapshot,
    references: list[Reference],
    problems: dict[str, str] | None = None,
    *,
    budget: int = CHARACTER_BUDGET,
) -> ContextBundle:
    bundle = ContextBundle(problems=dict(problems or {}))

    strong = [r for r in references if r.confidence >= MINIMUM_CONFIDENCE]
    candidates: list[ContextItem] = []

    for (path, symbol_name), group in _merge(strong).items():
        best = max(group, key=lambda r: r.confidence)
        lines = sorted({r.line for r in group if r.line})
        anchor = lines[0] if lines else 1

        start, end, snippet = _snippet_around(snapshot, path, anchor)

        candidates.append(
            ContextItem(
                file=path,
                start_line=start,
                end_line=end,
                snippet=snippet,
                relation=best.relation,
                discovery=(
                    DISCOVERY_IMPORT_CONFIRMED
                    if best.import_confirmed
                    else DISCOVERY_CALL_ONLY
                ),
                confidence=best.confidence,
                changed_symbol=symbol_name,
                reference_lines=tuple(lines),
            )
        )

    candidates.sort(key=lambda item: (-item.confidence, item.file))

    spent = 0
    for item in candidates:
        if spent + item.cost > budget:
            bundle.dropped.append(item)
            continue
        bundle.items.append(item)
        spent += item.cost

    if bundle.problems or bundle.dropped:
        bundle.status = STATUS_DEGRADED

    logger.info(
        "context: %s files, %s characters, %s dropped, status %s",
        len(bundle.files),
        bundle.characters,
        len(bundle.dropped),
        bundle.status,
    )
    return bundle
