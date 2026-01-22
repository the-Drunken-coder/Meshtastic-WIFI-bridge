"""Terminal UI entrypoint for Meshtastic bridge (WIP)."""

from __future__ import annotations

import shutil
import sys
import time


ESC = "\x1b"


def render_wip() -> None:
    cols, rows = shutil.get_terminal_size(fallback=(100, 30))
    cols = max(cols, 40)
    rows = max(rows, 12)

    title = "MESHTASTIC BRIDGE"
    subtitle = "WIP"
    hint = "Press Ctrl+C to exit"

    box_width = min(72, cols - 8)
    box_width = max(box_width, 32)
    box_height = 5

    def center(text: str, width: int) -> str:
        pad_total = max(width - len(text), 0)
        left = pad_total // 2
        right = pad_total - left
        return (" " * left) + text + (" " * right)

    top = (rows // 2) - 6
    box_top = (rows // 2) - (box_height // 2)
    box_left = (cols - box_width) // 2

    sys.stdout.write(ESC + "[?1049h")  # Alternate screen buffer
    sys.stdout.write(ESC + "[2J")
    sys.stdout.write(ESC + "[?25l")  # Hide cursor

    # Title block
    sys.stdout.write(f"{ESC}[{max(1, top)};1H")
    sys.stdout.write(center(title, cols))
    sys.stdout.write(f"{ESC}[{max(1, top + 2)};1H")
    sys.stdout.write(center(subtitle, cols))

    # Prompt-like box
    sys.stdout.write(f"{ESC}[{box_top};{box_left}H")
    sys.stdout.write("+" + ("-" * (box_width - 2)) + "+")
    sys.stdout.write(f"{ESC}[{box_top + 1};{box_left}H")
    sys.stdout.write("|" + (" " * (box_width - 2)) + "|")
    sys.stdout.write(f"{ESC}[{box_top + 2};{box_left}H")
    sys.stdout.write("|" + center("WIP UI", box_width - 2) + "|")
    sys.stdout.write(f"{ESC}[{box_top + 3};{box_left}H")
    sys.stdout.write("|" + (" " * (box_width - 2)) + "|")
    sys.stdout.write(f"{ESC}[{box_top + 4};{box_left}H")
    sys.stdout.write("+" + ("-" * (box_width - 2)) + "+")

    # Hint
    sys.stdout.write(f"{ESC}[{rows - 2};1H")
    sys.stdout.write(center(hint, cols))
    sys.stdout.flush()


def main() -> None:
    try:
        render_wip()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(ESC + "[?25h")  # Show cursor
        sys.stdout.write(ESC + "[?1049l")  # Leave alternate screen
        sys.stdout.flush()


if __name__ == "__main__":
    main()
