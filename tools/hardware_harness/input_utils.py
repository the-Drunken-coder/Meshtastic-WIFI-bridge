from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

SAFE_JSON_TYPES = (dict, list, int, float, str, bool, type(None))


def coerce_value(raw: str, type_hint: str | None) -> Any:
    if type_hint == "int":
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError("Enter an integer") from exc
    if type_hint == "float":
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError("Enter a number") from exc
    if type_hint == "bool":
        truthy = {"1", "true", "yes", "y", "on", "t"}
        falsy = {"0", "false", "no", "n", "off", "f"}
        lowered = raw.lower()
        if lowered in truthy:
            return True
        if lowered in falsy:
            return False
        raise ValueError("Enter true/false/yes/no/on/off/1/0")
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, SAFE_JSON_TYPES):
            return parsed
        raise ValueError("Provide standard JSON types only")
    except json.JSONDecodeError:
        return raw


def prompt_for_payload(
    fields: Iterable[Dict[str, Any]],
    validator: callable | None = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for field in fields:
        name = field["name"]
        prompt = field.get("prompt", name)
        default = field.get("default")
        type_hint = field.get("type")
        if default is not None:
            hint = f" [default: {default}, Enter=default, 'skip'=omit]"
        else:
            hint = " [blank to skip]"
        while True:
            raw = input(f"{prompt}{hint}: ").strip()
            if not raw:
                if default is not None:
                    payload[name] = default
                break
            if default is not None and raw.lower() == "skip":
                break
            try:
                value = coerce_value(raw, type_hint)
                if validator:
                    validator(name, value)
                payload[name] = value
                break
            except ValueError as exc:  # e.g., invalid int/float
                print(f"Invalid value for {name}: {exc}")
    return payload


def prompt_custom_payload() -> Dict[str, Any]:
    while True:
        raw = input("Enter JSON payload (blank for {}): ").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, SAFE_JSON_TYPES):
                return parsed if isinstance(parsed, dict) else {"value": parsed}
            print("Unsupported JSON type; please enter an object/array/primitive.")
        except json.JSONDecodeError as exc:
            print(f"Invalid JSON: {exc}")


def render_menu(actions: List[str], descriptions: Dict[str, str] | None = None) -> None:
    print("\n=== Meshtastic Bridge Harness ===")
    for idx, action in enumerate(actions, start=1):
        suffix = f" - {descriptions[action]}" if descriptions and action in descriptions else ""
        print(f"[{idx}] {action}{suffix}")
    print("[c] Custom command + JSON payload")
    print("[q] Quit")
