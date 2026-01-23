"""Terminal UI entrypoint for Meshtastic bridge."""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
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


def create_gradient_text(text: str, start_color: str, end_color: str) -> Text:
    """Create text with gradient color effect."""
    result = Text()
    lines = text.split('\n')
    
    # Define gradient colors from cyan to blue
    colors = [
        "#00ffff",  # Cyan
        "#00d4ff",
        "#00aaff",
        "#0080ff",
        "#0055ff",
        "#0033ff",  # Blue
    ]
    
    for i, line in enumerate(lines):
        if line.strip():
            color_idx = min(i, len(colors) - 1)
            result.append(line + "\n", style=Style(color=colors[color_idx], bold=True))
        else:
            result.append("\n")
    
    return result


def create_ui_layout() -> Layout:
    """Create the main UI layout."""
    layout = Layout()
    
    layout.split_column(
        Layout(name="top_spacer"),
        Layout(name="header", size=9),
        Layout(name="body", size=12),
        Layout(name="footer", size=4),
        Layout(name="bottom_spacer", size=1),
    )
    
    return layout


@dataclass
class UIState:
    view: str = "menu"
    menu_index: int = 0
    client_gateway_id: str = ""
    client_url: str = ""
    client_active_field: int = 0


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
        try:
            tty.setraw(fd)
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
        mapping = {"H": "up", "P": "down", "K": "left", "M": "right"}
        return mapping.get(ch2)
    if ch == "\r":
        return "enter"
    if ch == "\x08":
        return "backspace"
    if ch == "\x1b":
        return "esc"
    if ch == "\t":
        return "tab"
    return ch


def _read_key_posix() -> str | None:
    ch = sys.stdin.read(1)
    if ch == "\x1b":
        nxt = sys.stdin.read(2)
        mapping = {"[A": "up", "[B": "down", "[C": "right", "[D": "left"}
        return mapping.get(nxt, "esc")
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
    
    # Top spacer for vertical centering
    layout["top_spacer"].update("")
    
    # Header with logo
    logo_text = create_gradient_text(MESHTASTIC_LOGO, "#00ffff", "#0033ff")
    layout["header"].update(Align.center(logo_text, vertical="middle"))
    
    content_text = _render_body(backend_state, ui_state)
    
    input_panel = Panel(
        Align.center(content_text, vertical="middle"),
        border_style="cyan",
        padding=(1, 2),
        title=f"[bold cyan]Meshtastic {BRIDGE_SUBTITLE}[/bold cyan]",
        title_align="center"
    )
    
    layout["body"].update(Align.center(input_panel, vertical="middle"))
    
    footer_text = _render_footer(ui_state)
    
    layout["footer"].update(Align.center(footer_text, vertical="bottom"))
    
    # Bottom spacer for vertical centering
    layout["bottom_spacer"].update("")
    
    return layout


def _render_body(backend_state: BackendState, ui_state: UIState) -> Text:
    if ui_state.view == "gateway":
        return _render_gateway_body(backend_state)
    if ui_state.view == "client":
        return _render_client_body(backend_state, ui_state)
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


def _render_gateway_body(backend_state: BackendState) -> Text:
    text = Text()
    text.append("Gateway Running\n\n", style="bold cyan")
    if backend_state.gateway_error:
        text.append("Gateway Error: ", style="bold red")
        text.append(backend_state.gateway_error, style="red")
        text.append("\n\n")
    text.append("Local Radio ID: ", style="bold cyan")
    text.append(backend_state.local_radio_id or "unknown", style="green")
    text.append("\nConnected Radios: ", style="bold cyan")
    if backend_state.connected_radios:
        text.append(", ".join(backend_state.connected_radios), style="green")
    else:
        text.append("none", style="dim")
    text.append("\n\nTraffic:\n", style="bold cyan")
    if backend_state.gateway_traffic:
        for line in backend_state.gateway_traffic:
            text.append(f"{line}\n", style="dim")
    else:
        text.append("No traffic yet\n", style="dim")
    return text


def _render_client_body(backend_state: BackendState, ui_state: UIState) -> Text:
    text = Text()
    text.append("Client Mode\n\n", style="bold cyan")
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
    if backend_state.client_response:
        text.append(f"\nResponse: {backend_state.client_response}", style="green")
    if backend_state.client_error:
        text.append(f"\nError: {backend_state.client_error}", style="red")
    return text


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
    return text


def _handle_key(key: str, ui_state: UIState, backend: BackendService) -> None:
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
