"""Reading JSON back out of an agent's reply.

Agents are not run in JSON mode. Providers disagree about whether
`response_format` can be combined with `tools`, and the combination is
not worth the risk of a refused request mid-loop. The cost is that an
answer can arrive wrapped in prose, a fence, or both.
"""

from __future__ import annotations

import json

from src.llm.provider import InvalidResponse


def read_json_object(answer: str) -> dict:
    """The JSON object in a reply, however it was wrapped."""
    text = (answer or "").strip()

    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise InvalidResponse("no JSON object in the answer")

    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError as error:
        raise InvalidResponse(f"the answer is not valid JSON: {error}") from error

    if not isinstance(value, dict):
        raise InvalidResponse(f"the answer is a {type(value).__name__}, not an object")

    return value
