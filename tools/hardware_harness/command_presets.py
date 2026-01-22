from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

# Shared command definitions for the harness interactive menu
COMMAND_PRESETS: Dict[str, Dict[str, Any]] = {
    "echo": {
        "description": "Round-trip a small payload to verify the link",
        "fields": [
            {"name": "message", "prompt": "Echo message", "default": "hello from harness"},
        ],
    },
    "payload_digest": {
        "description": "Send a larger payload and receive size/hash metadata",
        "fields": [
            {"name": "file_path", "prompt": "File path (blank to skip)"},
            {"name": "content", "prompt": "Inline content (blank to skip)"},
            {
                "name": "size_kb",
                "prompt": "Generate size KB if none provided",
                "default": 10,
                "type": "int",
            },
            {
                "name": "content_type",
                "prompt": "Content type (optional)",
                "default": "text/plain",
                "type": None,
            },
        ],
    },
    "health": {
        "description": "Check gateway health",
        "fields": [],
    },
    "http_request": {
        "description": "Fetch a URL via the gateway (HTTP)",
        "fields": [
            {"name": "url", "prompt": "URL", "default": "https://example.com"},
            {"name": "method", "prompt": "HTTP method", "default": "GET"},
            {"name": "headers", "prompt": "Headers JSON (optional)", "default": ""},
            {"name": "body", "prompt": "Body text (optional)", "default": ""},
        ],
    },
}


def generate_realistic_content(size_kb: int, content_type: str | None) -> bytes:
    """Generate sample content that compresses more like real data."""
    size_kb = max(1, size_kb)
    size_bytes = size_kb * 1024
    is_text = bool(content_type and (content_type.startswith("text/") or "json" in content_type))
    if not is_text:
        return os.urandom(size_bytes)

    lines: List[str] = []
    idx = 0
    while sum(len(line) for line in lines) < size_bytes:
        lines.append(
            json.dumps(
                {
                    "ts": int(time.time()) + idx,
                    "lat": 40.0 + 0.001 * (idx % 500),
                    "lon": -75.0 - 0.001 * (idx % 500),
                    "note": f"sample-{idx % 10}",
                }
            )
            + "\n"
        )
        idx += 1
    text = "".join(lines)
    return text[:size_bytes].encode("utf-8")


def apply_field_defaults(
    _command: str, fields: List[Dict[str, Any]], _context: Dict[str, Any]
) -> List[Dict[str, Any]]:
    return fields


def default_context() -> Dict[str, Any]:
    return {}


def update_context_from_payload(_command: str, _payload: Dict[str, Any], _context: Dict[str, Any]) -> None:
    return None


__all__ = [
    "COMMAND_PRESETS",
    "apply_field_defaults",
    "default_context",
    "generate_realistic_content",
    "update_context_from_payload",
]
