"""What an agent is allowed to look at.

These are the same functions `src/context/` already uses to build context
up front; the difference is who decides when to call them. The context
builder guesses what will matter before anyone knows what the bug is. An
agent asks because of what it already suspects.

Two rules shape everything here.

Every tool returns a **string**, because that is what goes back into the
conversation. Shape matters as much as content: line numbers are included
so the model can cite them, and paths are repo-relative so it can pass
them straight back to another tool.

Every tool is **bounded**. A grep over a large repository could return
thousands of lines, and on an 8,000 tokens-per-minute budget one careless
answer costs the whole review. Where output is cut, the tool says so,
rather than letting the model believe it saw everything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.context.languages import REVIEWABLE_SUFFIXES, support_for
from src.context.references import verify_references
from src.context.snapshot import Snapshot
from src.context.symbols import Symbol, definitions_named
from src.review.provider import InvalidResponse, ToolCall

logger = logging.getLogger(__name__)

# One read should be a window, not a file. Anything larger is the model
# asking the wrong question, and answering it generously is how a loop
# spends its whole budget on turn two.
MAX_READ_LINES = 200
MAX_GREP_MATCHES = 40
MAX_DEFINITIONS = 10
MAX_REFERENCES = 25
MAX_LINE_CHARACTERS = 300

TEST_MARKERS = ("test_", "_test", ".test.", ".spec.", "tests/", "__tests__/")


class ToolFailure(Exception):
    """Something the model should be told about, not something that stops it.

    A missing file or an unknown symbol is ordinary during a search. The
    loop turns this into a tool result the model can read and recover
    from, which is why it carries a plain sentence and no traceback.
    """


def _clip(line: str) -> str:
    if len(line) <= MAX_LINE_CHARACTERS:
        return line
    return line[:MAX_LINE_CHARACTERS] + "  … (line truncated)"


def _numbered(lines: list[str], first_line: int) -> str:
    width = len(str(first_line + len(lines) - 1))
    return "\n".join(
        f"{first_line + offset:>{width}}  {_clip(line)}"
        for offset, line in enumerate(lines)
    )


def _looks_like_a_test(path: str) -> bool:
    return any(marker in path for marker in TEST_MARKERS)


@dataclass
class Toolbox:
    """The tools, bound to one repository snapshot.

    Calls are counted so a loop can report how much looking an agent did,
    which is the measurement the whole cost model rests on.
    """

    snapshot: Snapshot
    calls: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- tools

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        """A window of a file, with line numbers."""
        if not self.snapshot.exists(path):
            raise ToolFailure(
                f"{path} is not in this repository at this commit. "
                f"Use grep to find the right path."
            )

        lines = self.snapshot.text(path).splitlines()
        start = max(1, start)
        if start > len(lines):
            raise ToolFailure(f"{path} has only {len(lines)} lines")

        last = len(lines) if end is None else min(end, len(lines))
        last = min(last, start + MAX_READ_LINES - 1)
        window = lines[start - 1:last]

        header = f"{path} lines {start}-{last} of {len(lines)}"
        if last < len(lines):
            header += f"  (read {last + 1} onwards for the rest)"

        return f"{header}\n{_numbered(window, start)}"

    def grep(self, pattern: str, glob: str | None = None) -> str:
        """Every line containing this text. A plain substring, not a regex."""
        if not pattern:
            raise ToolFailure("grep needs something to search for")

        needle = pattern.encode()
        matches: list[str] = []
        truncated = False

        for path in self.snapshot.files():
            if glob and not _matches_glob(path, glob):
                continue
            if needle not in self.snapshot.read(path):
                continue

            for number, line in enumerate(self.snapshot.text(path).splitlines(), 1):
                if pattern not in line:
                    continue
                if len(matches) >= MAX_GREP_MATCHES:
                    truncated = True
                    break
                matches.append(f"{path}:{number}: {_clip(line.strip())}")
            if truncated:
                break

        if not matches:
            where = f" in files matching {glob}" if glob else ""
            return f"No line contains {pattern!r}{where}."

        answer = "\n".join(matches)
        if truncated:
            answer += (
                f"\n… stopped at {MAX_GREP_MATCHES} matches. "
                f"Narrow the search with a glob or a longer pattern."
            )
        return answer

    def get_definition(self, symbol: str) -> str:
        """Where a name is defined, and the code that defines it."""
        found = self._definitions_of(symbol)
        if not found:
            raise ToolFailure(
                f"No definition of {symbol!r} was found. It may be imported "
                f"from a dependency, or spelled differently."
            )

        parts = []
        for definition in found[:MAX_DEFINITIONS]:
            body = self.read_file(
                definition.file, definition.start_line, definition.end_line
            )
            parts.append(f"{definition.kind} {definition.qualified_name}\n{body}")

        answer = "\n\n".join(parts)
        if len(found) > MAX_DEFINITIONS:
            answer += f"\n\n… and {len(found) - MAX_DEFINITIONS} more definitions."
        return answer

    def find_references(self, symbol: str) -> str:
        """Who uses this symbol, with how sure we are about each.

        The confidence is not the model's opinion. It comes from the same
        two-pass check the context builder uses: a byte scan to find
        candidates, then an AST check for whether the name is really
        imported and really called. 0.9 means an import edge was verified
        to exist; 0.1 means the name only appears in a comment.
        """
        definitions = self._definitions_of(symbol)
        if not definitions:
            raise ToolFailure(
                f"No definition of {symbol!r} was found, so its uses "
                f"cannot be checked. Try grep instead."
            )

        target = definitions[0]
        references = []
        for path in self.snapshot.files():
            if path == target.file:
                continue
            if symbol.encode() not in self.snapshot.read(path):
                continue
            references.extend(verify_references(self.snapshot, path, target))

        if not references:
            return f"Nothing outside {target.file} uses {symbol}."

        references.sort(key=lambda reference: -reference.confidence)
        shown = references[:MAX_REFERENCES]

        lines = [
            f"{symbol} is defined at {target.file}:{target.start_line}. Uses:"
        ]
        for reference in shown:
            place = f"{reference.file}:{reference.line}" if reference.line else reference.file
            lines.append(
                f"  {place}  {reference.relation}  as {reference.local_name}  "
                f"confidence {reference.confidence}"
                + ("  (import confirmed)" if reference.import_confirmed else "")
            )
        if len(references) > MAX_REFERENCES:
            lines.append(f"  … and {len(references) - MAX_REFERENCES} weaker matches.")
        return "\n".join(lines)

    def list_tests(self, path: str) -> str:
        """Which test files touch this file, by name or by mention."""
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if not stem:
            raise ToolFailure(f"{path!r} is not a file path")

        by_name, by_mention = [], []
        needle = stem.encode()

        for candidate in self.snapshot.files():
            if candidate == path or not _looks_like_a_test(candidate):
                continue
            if stem in candidate:
                by_name.append(candidate)
            elif needle in self.snapshot.read(candidate):
                by_mention.append(candidate)

        if not by_name and not by_mention:
            return (
                f"No test file names or mentions {stem}. "
                f"Changes here are not covered by a test."
            )

        lines = []
        for candidate in by_name[:MAX_GREP_MATCHES]:
            lines.append(f"  {candidate}  (named after it)")
        for candidate in by_mention[:MAX_GREP_MATCHES]:
            lines.append(f"  {candidate}  (mentions it)")
        return f"Tests touching {path}:\n" + "\n".join(lines)

    # ------------------------------------------------------------ plumbing

    def _definitions_of(self, symbol: str) -> list[Symbol]:
        needle = symbol.encode()
        found: list[Symbol] = []
        for path in self.snapshot.files():
            if needle not in self.snapshot.read(path):
                continue
            found.extend(definitions_named(self.snapshot, path, symbol))
        return found

    def run(self, call: ToolCall) -> str:
        """Execute one call and give back what the model should read.

        A failure is returned as text rather than raised, because the model
        can recover from "that file does not exist" and cannot recover from
        the loop stopping. Only the tool name itself not existing is worth
        telling it loudly about.
        """
        self.calls.append(call.name)

        handler = {
            "read_file": self.read_file,
            "grep": self.grep,
            "get_definition": self.get_definition,
            "find_references": self.find_references,
            "list_tests": self.list_tests,
        }.get(call.name)

        if handler is None:
            return (
                f"There is no tool called {call.name!r}. "
                f"Available: read_file, grep, get_definition, "
                f"find_references, list_tests."
            )

        try:
            return handler(**call.parsed())
        except (ToolFailure, InvalidResponse) as error:
            return f"{call.name} failed: {error}"
        except TypeError as error:
            return f"{call.name} was called with the wrong arguments: {error}"
        except Exception:
            logger.exception("tool %s raised", call.name)
            return f"{call.name} could not be run."


def _matches_glob(path: str, glob: str) -> bool:
    """Suffix and directory filtering, deliberately simpler than fnmatch.

    The model reliably produces "*.js" and "frontend/", and unreliably
    produces anything more elaborate, so only those two shapes are honoured.
    """
    glob = glob.strip()
    if glob.startswith("*."):
        return path.endswith(glob[1:])
    if glob.endswith("/"):
        return path.startswith(glob)
    return glob in path


SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read numbered lines from a file in the repository at the "
                "commit under review. Prefer a narrow window over a whole file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repository-relative path, e.g. src/api/users.py",
                    },
                    "start": {"type": "integer", "description": "First line, 1-based"},
                    "end": {"type": "integer", "description": "Last line, inclusive"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": (
                "Find every line containing a plain substring. Not a regular "
                "expression. Use this to locate a file or an unfamiliar name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Literal text to find"},
                    "glob": {
                        "type": "string",
                        "description": "Optional filter: '*.js' or 'frontend/'",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_definition",
            "description": (
                "Show where a function or class is defined, with its source. "
                "Use this before judging whether a call site is correct."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A function or class name"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_references",
            "description": (
                "List everywhere a symbol is used, each with a confidence "
                "score derived from whether the import and the call were "
                "verified in the syntax tree. Use this to decide whether a "
                "change to a signature breaks a caller."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A function or class name"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tests",
            "description": (
                "List test files that name or mention a given source file. "
                "Use this to judge whether a change is covered by a test."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repository-relative path"},
                },
                "required": ["path"],
            },
        },
    },
]

__all__ = ["SCHEMAS", "ToolFailure", "Toolbox", "REVIEWABLE_SUFFIXES", "support_for"]
