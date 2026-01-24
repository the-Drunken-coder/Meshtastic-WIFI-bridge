"""Terminal UI entrypoint for Meshtastic bridge."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import textwrap
import glob
from pathlib import Path
from dataclasses import dataclass
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich.layout import Layout
from rich.live import Live
from rich.style import Style

from backend_service import BackendService, BackendState


MESHTASTIC_LOGO = """
███╗   ███╗███████╗███████╗██╗  ██╗████████╗ █████╗ ███████╗████████╗██╗ ██████╗
████╗ ████║██╔════╝██╔════╝██║  ██║╚══██╔══╝██╔══██╗██╔════╝╚══██╔══╝██║██╔════╝
██╔████╔██║█████╗  ███████╗███████║   ██║   ███████║███████╗   ██║   ██║██║     
██║╚██╔╝██║██╔══╝  ╚════██║██╔══██║   ██║   ██╔══██║╚════██║   ██║   ██║██║     
██║ ╚═╝ ██║███████╗███████║██║  ██║   ██║   ██║  ██║███████║   ██║   ██║╚██████╗
╚═╝     ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚══════╝   ╚═╝   ╚═╝ ╚═════╝
"""

BRIDGE_SUBTITLE = "WiFi Bridge"
BRIDGE_VERSION = "unknown"


def _load_version() -> str:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    package_path = os.path.join(root, "package.json")
    try:
        with open(package_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        version = data.get("version")
        return str(version) if version else "unknown"
    except Exception:
        return "unknown"


def _load_modes() -> list[str]:
    root = Path(__file__).resolve().parent.parent
    modes_dir = root / "modes"
    names: list[str] = []
    try:
        for path in glob.glob(str(modes_dir / "*.json")):
            name = Path(path).stem
            names.append(name)
    except Exception:
        return ["general"]
    return names or ["general"]


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    """Convert a hex color string like '#rrggbb' or 'rrggbb' to an (r, g, b) tuple."""
    color = color.lstrip("#")
    if len(color) != 6:
        # Fallback to white if the color format is unexpected
        return (255, 255, 255)
    try:
        r = int(color[0:2], 16)
        g = int(color[2:4], 16)
        b = int(color[4:6], 16)
    except ValueError:
        # Fallback to white on parse error
        return (255, 255, 255)
    return (r, g, b)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    """Convert an (r, g, b) tuple to a hex color string '#rrggbb'."""
    r, g, b = rgb
    r = max(0, min(255, r))
    g = max(0, min(255, g))
    b = max(0, min(255, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def _interpolate_rgb(
    start: tuple[int, int, int],
    end: tuple[int, int, int],
    ratio: float,
) -> tuple[int, int, int]:
    """Linearly interpolate between two RGB colors."""
    ratio = max(0.0, min(1.0, ratio))
    sr, sg, sb = start
    er, eg, eb = end
    r = int(sr + (er - sr) * ratio)
    g = int(sg + (eg - sg) * ratio)
    b = int(sb + (eb - sb) * ratio)
    return (r, g, b)


def create_gradient_text(text: str, start_color: str, end_color: str) -> Text:
    """Create text with gradient color effect."""
    result = Text()
    lines = text.split('\n')
    
    # Prepare gradient endpoints
    start_rgb = _hex_to_rgb(start_color)
    end_rgb = _hex_to_rgb(end_color)
    
    # Count non-empty lines to spread the gradient across them
    non_empty_count = sum(1 for line in lines if line.strip())
    if non_empty_count == 0:
        return result
    
    current_index = 0
    for line in lines:
        if line.strip():
            if non_empty_count == 1:
                ratio = 0.0
            else:
                ratio = current_index / (non_empty_count - 1)
            rgb = _interpolate_rgb(start_rgb, end_rgb, ratio)
            color_hex = _rgb_to_hex(rgb)
            result.append(line + "\n", style=Style(color=color_hex, bold=True))
            current_index += 1
        else:
            result.append("\n")
    
    return result


def create_ui_layout() -> Layout:
    """Create the main UI layout."""
    layout = Layout()
    
    layout.split_column(
        Layout(name="header", size=8),
        Layout(name="body"),
        Layout(name="footer", size=4),
    )
    
    return layout


@dataclass
class UIState:
    view: str = "menu"
    menu_index: int = 0
    client_gateway_id: str = ""
    client_url: str = ""
    client_active_field: int = 0
    client_scroll: int = 0
    client_notice: str | None = None
    client_notice_time: float = 0.0
    modes: list[str] = None  # type: ignore[assignment]
    mode_index: int = 0
    palette_open: bool = False
    palette_index: int = 0
    palette_options: list[dict] = None  # type: ignore[assignment]


MENU_OPTIONS = ["Open Client", "Start Gateway"]


class KeyReader:
    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="ui-key-reader",
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=1.0)

    def get_key(self) -> str | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def _run(self) -> None:
        if os.name == "nt":
            self._run_windows()
        else:
            self._run_posix()

    def _run_windows(self) -> None:
        import msvcrt

        while not self._stop_event.is_set():
            if not msvcrt.kbhit():
                time.sleep(0.05)
                continue
            key = _read_key_windows()
            if key:
                self._queue.put(key)

    def _run_posix(self) -> None:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        tty.setraw(fd)
        try:
            while not self._stop_event.is_set():
                readable, _, _ = select.select([sys.stdin], [], [], 0.1)
                if not readable:
                    continue
                key = _read_key_posix()
                if key:
                    self._queue.put(key)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _read_key_windows() -> str | None:
    import msvcrt

    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        ch2 = msvcrt.getwch()
        mapping = {
            "H": "up",
            "P": "down",
            "K": "left",
            "M": "right",
            "I": "pgup",
            "Q": "pgdn",
        }
        return mapping.get(ch2)
    if ch == "\r":
        return "enter"
    if ch == "\x08":
        return "backspace"
    if ch == "\x1b":
        return "esc"
    if ch == "\x10":  # Ctrl+P
        return "ctrl+p"
    if ch == "\t":
        return "tab"
    return ch


def _read_key_posix() -> str | None:
    ch = sys.stdin.read(1)
    if ch == "\x1b":
        nxt = sys.stdin.read(2)
        if nxt in ("[A", "[B", "[C", "[D"):
            mapping = {"[A": "up", "[B": "down", "[C": "right", "[D": "left"}
            return mapping.get(nxt, "esc")
        if nxt == "[5":
            sys.stdin.read(1)
            return "pgup"
        if nxt == "[6":
            sys.stdin.read(1)
            return "pgdn"
        return "esc"
    if ch == "\x10":
        return "ctrl+p"
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x7f":
        return "backspace"
    if ch == "\t":
        return "tab"
    return ch




def render_ui(console: Console, backend_state: BackendState, ui_state: UIState) -> Layout:
    """Render the beautiful UI."""
    layout = create_ui_layout()
    content_width = max(20, console.size.width - 20)
    if ui_state.palette_open:
        ui_state.palette_options = _build_palette_options(ui_state, backend_state)
        ui_state.palette_index = _clamp_scroll(ui_state.palette_index, len(ui_state.palette_options), 1)
    
    # Header with logo
    logo_text = create_gradient_text(MESHTASTIC_LOGO, "#00ffff", "#0033ff")
    layout["header"].update(Align.center(logo_text, vertical="top"))
    
    content_text = _render_body(backend_state, ui_state, content_width)
    
    input_panel = Panel(
        Align.center(content_text, vertical="middle"),
        border_style="cyan",
        padding=(1, 2),
        title=f"[bold cyan]Meshtastic {BRIDGE_SUBTITLE}[/bold cyan]",
        title_align="center"
    )
    
    layout["body"].update(Align.center(input_panel, vertical="top"))
    
    footer_text = _render_footer(ui_state)
    
    layout["footer"].update(Align.center(footer_text, vertical="bottom"))
    
    return layout


def _render_body(backend_state: BackendState, ui_state: UIState, content_width: int) -> Text:
    if ui_state.palette_open:
        return _render_palette_view(ui_state)
    if ui_state.view == "gateway":
        return _render_gateway_body(backend_state, ui_state)
    if ui_state.view == "client":
        return _render_client_body(backend_state, ui_state, content_width)
    return _render_menu_body(backend_state, ui_state)


def _render_menu_body(backend_state: BackendState, ui_state: UIState) -> Text:
    text = Text()
    text.append("Select Mode\n\n", style="bold cyan")
    for index, option in enumerate(MENU_OPTIONS):
        disabled = not backend_state.radio_detected
        prefix = "> " if index == ui_state.menu_index else "  "
        if disabled:
            style = "dim"
        else:
            style = "bold white" if index == ui_state.menu_index else "dim"
        text.append(f"{prefix}{option}\n", style=style)
    text.append("\nRadio: ", style="bold cyan")
    if backend_state.radio_detected:
        ports = ", ".join(backend_state.radio_ports)
        text.append("Detected", style="green")
        if ports:
            text.append(f" ({ports})", style="green")
    else:
        text.append("Not Found", style="red")
        if backend_state.last_error:
            text.append(" (check drivers)", style="yellow")
        text.append("\n\nConnect a radio to enable menu options.", style="dim")
    return text


def _render_gateway_body(backend_state: BackendState, ui_state: UIState) -> Text:
    text = Text()
    text.append("Gateway Running\n\n", style="bold cyan")
    if backend_state.gateway_error:
        text.append("Gateway Error: ", style="bold red")
        text.append(backend_state.gateway_error, style="red")
        text.append("\n\n")
    text.append("Connected Radio: ", style="bold cyan")
    if backend_state.radio_ports:
        text.append(backend_state.radio_ports[0], style="green")
    else:
        text.append("none", style="dim")
    text.append("\n", style="dim")
    text.append("Mode: ", style="bold cyan")
    text.append(_current_mode_label(ui_state), style="green")
    text.append("\n", style="dim")
    text.append("Local Radio ID: ", style="bold cyan")
    if backend_state.local_radio_id:
        text.append(backend_state.local_radio_id, style="green")
    elif backend_state.gateway_error:
        text.append("Search failed", style="red")
    else:
        text.append("Searching...", style="dim")
    text.append("\nConnected Radios: ", style="bold cyan")
    if backend_state.connected_radios:
        text.append(", ".join(backend_state.connected_radios), style="green")
    else:
        text.append("none", style="dim")
    text.append("\nLast RX: ", style="bold cyan")
    text.append(_format_timestamp(backend_state.last_rx_time), style="green")
    text.append("\nLast TX: ", style="bold cyan")
    text.append(_format_timestamp(backend_state.last_tx_time), style="green")
    text.append("\nSpool Depth: ", style="bold cyan")
    text.append(str(backend_state.spool_depth), style="green")
    text.append("\nLast Payload: ", style="bold cyan")
    if backend_state.gateway_last_payload:
        text.append(backend_state.gateway_last_payload, style="green")
    else:
        text.append("none", style="dim")
    if backend_state.gateway_last_chunks_total:
        text.append("\nChunks: ", style="bold cyan")
        text.append(str(backend_state.gateway_last_chunks_total), style="green")
    text.append("\n\nTraffic:\n", style="bold cyan")
    if backend_state.gateway_traffic:
        for line in backend_state.gateway_traffic:
            text.append(f"{line}\n", style="dim")
    else:
        text.append("No traffic yet\n", style="dim")
    return text


def _render_palette_view(ui_state: UIState) -> Text:
    text = Text()
    text.append("Command Palette\n\n", style="bold cyan")
    text.append(_render_palette(ui_state))
    text.append("\n\n", style="dim")
    text.append("Esc to close | Up/Down to navigate | Enter to run", style="dim")
    return text


def _render_client_body(
    backend_state: BackendState,
    ui_state: UIState,
    content_width: int,
) -> Text:
    text = Text()
    text.append("Client Mode\n\n", style="bold cyan")
    text.append("Connected Radio: ", style="bold cyan")
    if backend_state.radio_ports:
        text.append(backend_state.radio_ports[0], style="green")
    else:
        text.append("none", style="dim")
    text.append("\nMode: ", style="bold cyan")
    text.append(_current_mode_label(ui_state), style="green")
    text.append("\nLocal Radio ID: ", style="bold cyan")
    if backend_state.local_radio_id:
        text.append(backend_state.local_radio_id, style="green")
    else:
        text.append("Searching...", style="dim")
    text.append("\nLast RX: ", style="bold cyan")
    text.append(_format_timestamp(backend_state.last_rx_time), style="green")
    text.append("\nLast TX: ", style="bold cyan")
    text.append(_format_timestamp(backend_state.last_tx_time), style="green")
    text.append("\nSpool Depth: ", style="bold cyan")
    text.append(str(backend_state.spool_depth), style="green")
    text.append("\n\n", style="dim")
    fields = [
        ("Gateway ID", ui_state.client_gateway_id),
        ("URL", ui_state.client_url),
    ]
    for index, (label, value) in enumerate(fields):
        active = index == ui_state.client_active_field
        prefix = "> " if active else "  "
        style = "bold white" if active else "dim"
        display = value or "(empty)"
        text.append(f"{prefix}{label}: {display}\n", style=style)
    text.append("\nStatus: ", style="bold cyan")
    text.append(backend_state.client_status or "idle", style="green")
    text.append("\nSend: ", style="bold cyan")
    text.append(_format_progress(
        backend_state.client_send_chunks_sent,
        backend_state.client_send_chunks_total,
        backend_state.client_send_eta_seconds,
    ), style="green")
    text.append("\nReceive: ", style="bold cyan")
    text.append(_format_progress(
        backend_state.client_recv_chunks_received,
        backend_state.client_recv_chunks_total,
        backend_state.client_recv_eta_seconds,
    ), style="green")
    if backend_state.client_response:
        text.append(f"\nResponse: {backend_state.client_response}", style="green")
    if backend_state.client_error:
        text.append(f"\nError: {backend_state.client_error}", style="red")
    if backend_state.client_last_payload:
        payload_lines = _wrap_payload(backend_state.client_last_payload, content_width - 8)
        max_lines = 4
        ui_state.client_scroll = _clamp_scroll(ui_state.client_scroll, len(payload_lines), max_lines)
        start = ui_state.client_scroll
        end = min(start + max_lines, len(payload_lines))
        range_label = f"{start + 1}-{end} of {len(payload_lines)}"
        text.append(f"\nPayload ({range_label}):", style="bold cyan")
        for line in payload_lines[start:end]:
            text.append(f"\n{line}", style="green")
    if backend_state.client_history:
        text.append("\n\nRecent Responses:", style="bold cyan")
        for entry in backend_state.client_history[:3]:
            text.append(f"\n{entry}", style="dim")
    notice = _notice_text(ui_state)
    if notice:
        text.append(f"\n\n{notice}", style="yellow")
    return text


def _format_progress(sent: int, total: int, eta_seconds: float | None) -> str:
    if total <= 0:
        return "0% (0/0)"
    percent = int((sent / total) * 100)
    eta_text = ""
    if eta_seconds is not None:
        eta_text = f" ETA {eta_seconds:.1f}s"
    return f"{percent}% ({sent}/{total}){eta_text}"


def _wrap_payload(payload: str, width: int) -> list[str]:
    width = max(10, width)
    lines: list[str] = []
    for line in payload.splitlines() or [""]:
        lines.extend(textwrap.wrap(line, width=width) or [""])
    return lines


def _clamp_scroll(offset: int, total: int, window: int) -> int:
    if total <= window:
        return 0
    max_offset = max(0, total - window)
    return max(0, min(offset, max_offset))


def _build_palette_options(ui_state: UIState, backend_state: BackendState) -> list[dict]:
    options: list[dict] = []
    options.append(
        {
            "label": f"Change mode (current: { _current_mode_label(ui_state) })",
            "enabled": True,
            "action": "mode",
        }
    )
    if ui_state.view == "client":
        options.append(
            {
                "label": "Send health request",
                "enabled": bool(ui_state.client_gateway_id.strip()),
                "action": "health",
            }
        )
        payload_available = bool(backend_state.client_last_payload or backend_state.client_response)
        options.append(
            {
                "label": "Copy latest payload/response",
                "enabled": payload_available,
                "action": "copy",
            }
        )
    options.append({"label": "Close palette", "enabled": True, "action": "close"})
    return options


def _render_palette(ui_state: UIState) -> Text:
    text = Text()
    options = ui_state.palette_options or []
    total = len(options)
    if not options:
        text.append("No commands available", style="dim")
        return text
    ui_state.palette_index = _clamp_scroll(ui_state.palette_index, total, 1)
    for idx, opt in enumerate(options):
        prefix = "> " if idx == ui_state.palette_index else "  "
        style = "bold white" if idx == ui_state.palette_index else "dim"
        if not opt.get("enabled", True):
            style = "grey50"
        text.append(f"{prefix}{opt.get('label','')}\n", style=style)
    return text


def _format_timestamp(ts: float | None) -> str:
    if not ts:
        return "none"
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _cycle_mode(ui_state: UIState) -> None:
    if not ui_state.modes:
        ui_state.modes = ["general"]
    ui_state.mode_index = (ui_state.mode_index + 1) % len(ui_state.modes)


def _current_mode_label(ui_state: UIState) -> str:
    if not ui_state.modes:
        return "general"
    idx = ui_state.mode_index % len(ui_state.modes)
    return ui_state.modes[idx]


def _set_notice(ui_state: UIState, message: str) -> None:
    ui_state.client_notice = message
    ui_state.client_notice_time = time.time()


def _notice_text(ui_state: UIState, ttl_seconds: float = 3.0) -> str | None:
    if not ui_state.client_notice:
        return None
    if time.time() - ui_state.client_notice_time > ttl_seconds:
        ui_state.client_notice = None
        return None
    return ui_state.client_notice


def _copy_to_clipboard(text: str) -> bool:
    try:
        import tkinter  # type: ignore

        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
        return True
    except Exception:
        pass

    try:
        if os.name == "nt":
            import subprocess

            subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Set-Clipboard", "-Value", text],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
    except Exception:
        return False
    return False


def _handle_palette_key(key: str, ui_state: UIState, backend: BackendService) -> None:
    options = ui_state.palette_options or []
    if not options:
        ui_state.palette_open = False
        return
    if key == "up":
        ui_state.palette_index = max(0, ui_state.palette_index - 1)
        return
    if key == "down":
        ui_state.palette_index = min(len(options) - 1, ui_state.palette_index + 1)
        return
    if key in {"esc", "q"}:
        ui_state.palette_open = False
        return
    if key == "enter":
        option = options[ui_state.palette_index]
        if not option.get("enabled", True):
            return
        action = option.get("action")
        if action == "mode":
            _cycle_mode(ui_state)
            _set_notice(ui_state, f"Mode set to {_current_mode_label(ui_state)}")
        elif action == "health":
            if ui_state.client_gateway_id:
                backend.send_health_request(ui_state.client_gateway_id.strip())
                _set_notice(ui_state, "Health request sent")
        elif action == "copy":
            snapshot = backend.snapshot()
            payload = snapshot.client_last_payload or snapshot.client_response
            if payload:
                ok = _copy_to_clipboard(payload)
                _set_notice(ui_state, "Copied to clipboard" if ok else "Copy failed")
            else:
                _set_notice(ui_state, "Nothing to copy")
        ui_state.palette_open = False


def _render_footer(ui_state: UIState) -> Text:
    text = Text()
    text.append("Ctrl+C", style="bold white")
    text.append(" to exit", style="dim")
    text.append(" | ", style="dim")
    if ui_state.view == "menu":
        text.append("Arrows", style="bold white")
        text.append(" to navigate, ", style="dim")
        text.append("Enter", style="bold white")
        text.append(" to select", style="dim")
    elif ui_state.view == "gateway":
        text.append("Q/Esc", style="bold white")
        text.append(" to stop gateway", style="dim")
    elif ui_state.view == "client":
        text.append("Tab", style="bold white")
        text.append(" to switch, ", style="dim")
        text.append("Enter", style="bold white")
        text.append(" to submit", style="dim")
        text.append(", ", style="dim")
        text.append("Esc", style="bold white")
        text.append(" to menu", style="dim")
        text.append(", ", style="dim")
        text.append("PgUp/PgDn", style="bold white")
        text.append(" to scroll", style="dim")
    text.append(" | ", style="dim")
    text.append("Ctrl+P for commands", style="dim")
    text.append(" | ", style="dim")
    text.append(f"v{BRIDGE_VERSION}", style="dim")
    return text


def _handle_key(key: str, ui_state: UIState, backend: BackendService) -> None:
    if key == "ctrl+p":
        if ui_state.palette_open:
            ui_state.palette_open = False
        else:
            ui_state.palette_open = True
            ui_state.palette_options = _build_palette_options(ui_state, backend.snapshot())
            ui_state.palette_index = 0
        return
    if ui_state.palette_open:
        _handle_palette_key(key, ui_state, backend)
        return

    if ui_state.view == "menu":
        _handle_menu_key(key, ui_state, backend)
        return
    if ui_state.view == "gateway":
        if key in {"q", "esc"}:
            backend.stop_gateway()
            ui_state.view = "menu"
        return
    if ui_state.view == "client":
        _handle_client_key(key, ui_state, backend)


def _handle_menu_key(key: str, ui_state: UIState, backend: BackendService) -> None:
    if key == "up":
        ui_state.menu_index = (ui_state.menu_index - 1) % len(MENU_OPTIONS)
    elif key == "down":
        ui_state.menu_index = (ui_state.menu_index + 1) % len(MENU_OPTIONS)
    elif key == "enter":
        if not backend.snapshot().radio_detected:
            return
        selection = MENU_OPTIONS[ui_state.menu_index]
        if selection == "Start Gateway":
            backend.start_gateway()
            ui_state.view = "gateway"
        elif selection == "Open Client":
            backend.stop_gateway()
            ui_state.view = "client"
            ui_state.client_active_field = 0


def _handle_client_key(key: str, ui_state: UIState, backend: BackendService) -> None:
    if key in {"esc", "q"}:
        ui_state.view = "menu"
        return
    if key == "pgup":
        ui_state.client_scroll = max(0, ui_state.client_scroll - 1)
        return
    if key == "pgdn":
        ui_state.client_scroll += 1
        return
    if key in {"tab", "up", "down"}:
        ui_state.client_active_field = 1 - ui_state.client_active_field
        return
    if key == "enter":
        if ui_state.client_active_field == 0:
            ui_state.client_active_field = 1
            return
        if ui_state.client_gateway_id and ui_state.client_url:
            backend.send_http_request(
                ui_state.client_gateway_id.strip(),
                ui_state.client_url.strip(),
            )
        return
    if key == "backspace":
        if ui_state.client_active_field == 0:
            ui_state.client_gateway_id = ui_state.client_gateway_id[:-1]
        else:
            ui_state.client_url = ui_state.client_url[:-1]
        return
    if len(key) == 1 and key.isprintable():
        if ui_state.client_active_field == 0:
            ui_state.client_gateway_id += key
        else:
            ui_state.client_url += key


def main() -> None:
    """Main entry point."""
    console = Console()
    backend = BackendService()
    ui_state = UIState()
    key_reader = KeyReader()
    global BRIDGE_VERSION
    BRIDGE_VERSION = _load_version()
    ui_state.modes = _load_modes()
    
    try:
        console.clear()
        console.show_cursor(False)
        backend.start()
        key_reader.start()
        
        layout = render_ui(console, backend.snapshot(), ui_state)
        
        with Live(layout, console=console, screen=True, refresh_per_second=10) as live:
            while True:
                key = key_reader.get_key()
                if key:
                    _handle_key(key, ui_state, backend)
                time.sleep(0.05)
                live.update(render_ui(console, backend.snapshot(), ui_state))
                
    except KeyboardInterrupt:
        pass
    finally:
        key_reader.stop()
        backend.stop()
        console.show_cursor(True)
        console.clear()


if __name__ == "__main__":
    main()
