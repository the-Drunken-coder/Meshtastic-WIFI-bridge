from __future__ import annotations

from typing import Any, Dict, List


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    return f"{size / 1024:.1f} KB"


def render_diagnostics(diags: List[Dict[str, Any]]) -> None:
    if not diags:
        return
    total_time = sum(diag.get("duration_seconds", 0.0) for diag in diags)
    total_request = sum(diag.get("request_bytes", 0) for diag in diags)
    total_response = sum(diag.get("response_bytes", 0) for diag in diags)
    total_timeouts = sum(1 for diag in diags if diag.get("timed_out"))
    print("\n=== Harness Diagnostics ===")
    for diag in diags:
        print(f"Command: {diag.get('command')}")
        print(f"Status: {diag.get('status')}")
        if diag.get("timed_out"):
            print("Timed out: yes")
        print(f"Duration: {diag.get('duration_seconds', 0.0):.2f}s")
        print(f"Request size: {_format_bytes(diag.get('request_bytes', 0))}")
        print(f"Response size: {_format_bytes(diag.get('response_bytes', 0))}")
        print(f"Total payload: {_format_bytes(diag.get('request_bytes', 0) + diag.get('response_bytes', 0))}")
        print(f"Timeout: {diag.get('timeout_seconds', 0.0):.1f}s, Retries: {diag.get('retries', 0)}")
        if diag.get("response_type"):
            print(f"Response type: {diag.get('response_type')}")
        if diag.get("error"):
            print(f"Error: {diag.get('error')}")
        print("---")
    if len(diags) > 1:
        print("Aggregate")
        print(f"Total duration: {total_time:.2f}s")
        print(f"Total request bytes: {_format_bytes(total_request)}")
        print(f"Total response bytes: {_format_bytes(total_response)}")
        print(f"Total payload: {_format_bytes(total_request + total_response)}")
        if total_timeouts:
            print(f"Timeouts: {total_timeouts}")


__all__ = ["render_diagnostics"]
