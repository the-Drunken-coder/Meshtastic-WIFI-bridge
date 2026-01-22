"""Terminal UI entrypoint for Meshtastic bridge (WIP)."""

from __future__ import annotations

import shutil


def render_wip() -> None:
    width = shutil.get_terminal_size(fallback=(60, 10)).columns
    width = max(20, min(width, 80))
    title = "MESHTASTIC BRIDGE"
    body = "WIP"
    border = "+" + ("-" * (width - 2)) + "+"

    def pad(text: str) -> str:
        return "| " + text.ljust(width - 4) + " |"

    print(border)
    print(pad(title))
    print(pad(""))
    print(pad(body))
    print(pad(""))
    print(border)


def main() -> None:
    render_wip()


if __name__ == "__main__":
    main()
