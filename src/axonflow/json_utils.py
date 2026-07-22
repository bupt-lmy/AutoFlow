"""Helpers for extracting structured JSON objects from model responses."""

from __future__ import annotations

import json
from typing import Any


def parse_json_object(content: str) -> dict[str, Any]:
    """Parse a JSON object even when a model surrounds it with prose or a code fence."""
    stripped = content.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        last_fence = stripped.rfind("```")
        if first_newline >= 0 and last_fence > first_newline:
            stripped = stripped[first_newline + 1 : last_fence].strip()

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, character in enumerate(stripped):
            if character != "{":
                continue
            try:
                parsed, _end = decoder.raw_decode(stripped[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        raise ValueError("Model response does not contain a valid JSON object") from None

    if not isinstance(parsed, dict):
        raise ValueError("Model response JSON must be an object")
    return parsed
