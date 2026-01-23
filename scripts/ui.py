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
        Layout(name="top_spacer"),
        Layout(name="header", size=9),
        Layout(name="body", size=8),
        Layout(name="footer", size=3),
        Layout(name="bottom_spacer"),
    )
    
    return layout


def render_ui(console: Console) -> Layout:
    """Render the beautiful UI."""
    layout = create_ui_layout()
    
    # Top spacer for vertical centering
    layout["top_spacer"].update("")
    
    # Header with logo
    logo_text = create_gradient_text(MESHTASTIC_LOGO, "#00ffff", "#0033ff")
    layout["header"].update(Align.center(logo_text, vertical="middle"))
    
    # Body with status panel
    content_text = Text()
    content_text.append("Input Methods: ", style="bold cyan")
    content_text.append("WIP", style="yellow bold")
    content_text.append("\n\nStatus: ", style="bold cyan")
    content_text.append("Bridge Ready", style="green")
    content_text.append("\n\n", style="dim")
    content_text.append("Note: Input functionality coming soon", style="dim italic")
    
    input_panel = Panel(
        Align.center(content_text, vertical="middle"),
        border_style="cyan",
        padding=(1, 2),
        title="[bold cyan]Meshtastic WiFi Bridge[/bold cyan]",
        title_align="center"
    )
    
    layout["body"].update(Align.center(input_panel, vertical="middle"))
    
    # Footer with hints
    footer_text = Text()
    footer_text.append("Press ", style="dim")
    footer_text.append("Ctrl+C", style="bold white")
    footer_text.append(" to exit", style="dim")
    
    layout["footer"].update(Align.center(footer_text, vertical="middle"))
    
    # Bottom spacer for vertical centering
    layout["bottom_spacer"].update("")
    
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
