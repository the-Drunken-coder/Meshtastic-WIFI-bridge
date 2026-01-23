"""Terminal UI entrypoint for Meshtastic bridge."""

from __future__ import annotations

import sys
import time
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich.layout import Layout
from rich.live import Live
from rich.style import Style


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
        Layout(name="header", size=12),
        Layout(name="body", size=7),
        Layout(name="footer", size=3),
    )
    
    return layout


def render_ui(console: Console) -> Layout:
    """Render the beautiful UI."""
    layout = create_ui_layout()
    
    # Header with logo
    logo_text = create_gradient_text(MESHTASTIC_LOGO, "#00ffff", "#0033ff")
    layout["header"].update(Align.center(logo_text, vertical="middle"))
    
    # Body with status panel
    input_text = Text()
    input_text.append("WiFi Bridge ", style="dim")
    input_text.append("Initializing...", style="white")
    
    status_text = Text()
    status_text.append("\nStatus: ", style="bold cyan")
    status_text.append("Ready to Connect ", style="white")
    status_text.append("(WIP)", style="dim")
    
    input_text.append(status_text)
    
    input_panel = Panel(
        Align.left(input_text),
        border_style="blue",
        padding=(1, 2),
        style="on black"
    )
    
    layout["body"].update(Align.center(input_panel, vertical="middle"))
    
    # Footer with hints
    footer_text = Text()
    footer_text.append("ctrl+c", style="dim")
    footer_text.append(" to exit  ", style="dim")
    footer_text.append("Work in Progress", style="bold dim")
    
    layout["footer"].update(Align.center(footer_text, vertical="middle"))
    
    return layout


def main() -> None:
    """Main entry point."""
    console = Console()
    
    try:
        # Clear screen and hide cursor
        console.clear()
        console.show_cursor(False)
        
        # Render the UI
        layout = render_ui(console)
        
        with Live(layout, console=console, screen=True, refresh_per_second=4):
            while True:
                time.sleep(0.25)
                
    except KeyboardInterrupt:
        pass
    finally:
        console.show_cursor(True)
        console.clear()


if __name__ == "__main__":
    main()
