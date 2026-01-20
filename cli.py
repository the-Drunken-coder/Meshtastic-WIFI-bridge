"""Interactive CLI for LoRa WiFi Forwarder."""

import sys
import signal
import subprocess
from typing import Optional, List

from common.serial_detection import detect_meshtastic_port, get_default_serial_port
from common.network_detection import detect_internet_interface, get_default_interface
from common.logging_setup import get_logger

logger = get_logger(__name__)


def clear_screen():
    """Clear the terminal screen."""
    import os
    os.system('cls' if os.name == 'nt' else 'clear')


def print_header(title: str):
    """Print a formatted header."""
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60 + "\n")


def print_menu(options: List[tuple], prompt: str = "Select an option") -> int:
    """
    Display a menu and get user selection.
    
    Args:
        options: List of (number, description) tuples
        prompt: Prompt text
        
    Returns:
        Selected option number (1-based)
    """
    for num, desc in options:
        print(f"  {num}. {desc}")
    print()
    
    while True:
        try:
            choice = input(f"{prompt} (1-{len(options)}): ").strip()
            num = int(choice)
            if 1 <= num <= len(options):
                return num
            print(f"Please enter a number between 1 and {len(options)}")
        except ValueError:
            print("Please enter a valid number")
        except KeyboardInterrupt:
            print("\n\nCancelled.")
            sys.exit(0)


def get_text_input(prompt: str, default: Optional[str] = None, required: bool = True) -> str:
    """
    Get text input from user.
    
    Args:
        prompt: Prompt text
        default: Default value (optional)
        required: Whether input is required
        
    Returns:
        User input or default
    """
    if default:
        full_prompt = f"{prompt} [{default}]: "
    else:
        full_prompt = f"{prompt}: "
    
    while True:
        try:
            value = input(full_prompt).strip()
            if value:
                return value
            elif default:
                return default
            elif not required:
                return ""
            else:
                print("This field is required. Please enter a value.")
        except KeyboardInterrupt:
            print("\n\nCancelled.")
            sys.exit(0)


def get_int_input(prompt: str, default: Optional[int] = None, min_val: Optional[int] = None, max_val: Optional[int] = None) -> int:
    """Get integer input from user."""
    if default is not None:
        full_prompt = f"{prompt} [{default}]: "
    else:
        full_prompt = f"{prompt}: "
    
    while True:
        try:
            value = input(full_prompt).strip()
            if not value and default is not None:
                return default
            num = int(value)
            if min_val is not None and num < min_val:
                print(f"Value must be at least {min_val}")
                continue
            if max_val is not None and num > max_val:
                print(f"Value must be at most {max_val}")
                continue
            return num
        except ValueError:
            print("Please enter a valid number")
        except KeyboardInterrupt:
            print("\n\nCancelled.")
            sys.exit(0)


def list_serial_ports() -> List[str]:
    """List available serial ports."""
    ports = []
    try:
        import serial.tools.list_ports
        port_list = serial.tools.list_ports.comports()
        ports = [port.device for port in port_list]
    except ImportError:
        pass
    except Exception:
        pass
    return ports


def list_network_interfaces() -> List[str]:
    """List available network interfaces."""
    interfaces = []
    try:
        if sys.platform.startswith("win"):
            # Use PowerShell to list network adapters
            cmd = [
                "powershell",
                "-Command",
                "Get-NetAdapter | Where-Object Status -eq 'Up' | Select-Object -ExpandProperty Name"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                interfaces = [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
        else:
            # Linux/macOS - list interfaces from /sys/class/net or ifconfig
            import os
            if os.path.exists("/sys/class/net"):
                interfaces = [d for d in os.listdir("/sys/class/net") if d != "lo"]
    except Exception:
        pass
    return interfaces


def select_serial_port() -> str:
    """Interactive serial port selection (auto-selects if detected)."""
    # Try to detect Meshtastic port (with timeout built into detection function)
    print("Detecting Meshtastic device...", end="", flush=True)
    try:
        detected = detect_meshtastic_port()
        print("\r" + " " * 50 + "\r", end="")  # Clear the "Detecting..." message
        if detected:
            print(f"✓ Auto-detected Meshtastic device: {detected}")
            return detected
    except Exception as e:
        print("\r" + " " * 50 + "\r", end="")  # Clear the "Detecting..." message
        logger.debug(f"Port detection error: {e}")
    
    # No detection - show selection menu
    
    # No detection - show selection menu
    print_header("Serial Port Selection")
    print("No Meshtastic device auto-detected. Please select manually:\n")
    
    # List all available ports
    ports = list_serial_ports()
    
    options = []
    if ports:
        for i, port in enumerate(ports, start=1):
            options.append((i, port))
    
    default_port = get_default_serial_port()
    options.append((len(options) + 1, f"Use default: {default_port}"))
    options.append((len(options) + 1, "Enter custom port"))
    
    choice = print_menu(options, "Select serial port")
    
    if ports and choice <= len(ports):
        return ports[choice - 1]
    elif choice == len(options) - 1:
        return default_port
    else:
        return get_text_input("Enter serial port", default_port)


def select_network_interface() -> str:
    """Interactive network interface selection (auto-selects if detected)."""
    # Try to detect internet interface
    detected = detect_internet_interface()
    if detected:
        print(f"✓ Auto-detected internet interface: {detected}")
        return detected
    
    # No detection - show selection menu
    print_header("Network Interface Selection")
    print("No internet interface auto-detected. Please select manually:\n")
    
    # List available interfaces
    interfaces = list_network_interfaces()
    
    options = []
    if interfaces:
        for i, iface in enumerate(interfaces, start=1):
            options.append((i, iface))
    
    default_iface = get_default_interface()
    options.append((len(options) + 1, f"Use default: {default_iface}"))
    options.append((len(options) + 1, "Enter custom interface"))
    
    choice = print_menu(options, "Select network interface")
    
    if interfaces and choice <= len(interfaces):
        return interfaces[choice - 1]
    elif choice == len(options) - 1:
        return default_iface
    else:
        return get_text_input("Enter network interface", default_iface)


def configure_gateway() -> List[str]:
    """Interactive gateway configuration."""
    print_header("Gateway Configuration")
    
    args = []
    
    # Serial port
    serial_port = select_serial_port()
    args.extend(["--serial", serial_port])
    
    # Network interface
    internet_iface = select_network_interface()
    args.extend(["--internet-iface", internet_iface])
    
    # Log level
    print_header("Advanced Options")
    log_levels = [
        (1, "DEBUG - Detailed debugging information"),
        (2, "INFO - General information (recommended)"),
        (3, "WARNING - Warnings and errors only"),
        (4, "ERROR - Errors only"),
    ]
    log_choice = print_menu(log_levels, "Select log level")
    log_level = ["DEBUG", "INFO", "WARNING", "ERROR"][log_choice - 1]
    args.extend(["--log-level", log_level])
    
    # Log file (optional)
    log_file = get_text_input("Log file path (optional, press Enter to skip)", required=False)
    if log_file:
        args.extend(["--log-file", log_file])
    
    # Window size
    window_size = get_int_input("Sliding window size", default=4, min_val=1, max_val=32)
    args.extend(["--window-size", str(window_size)])
    
    # Retransmit timeout
    retransmit_timeout = get_int_input("Retransmit timeout (ms)", default=5000, min_val=1000)
    args.extend(["--retransmit-timeout", str(retransmit_timeout)])
    
    return args


def configure_client() -> List[str]:
    """Interactive client configuration."""
    print_header("Client Configuration")
    
    args = []
    
    # Serial port
    serial_port = select_serial_port()
    args.extend(["--serial", serial_port])
    
    # Gateway node ID
    print_header("Gateway Node ID")
    print("You need the gateway node ID from the gateway daemon.")
    print("It will be displayed when the gateway starts (e.g., 0x12345678 or !12345678)\n")
    gateway_id = get_text_input("Gateway node ID (hex with ! or 0x prefix, or decimal)", required=True)
    args.extend(["--gateway-node-id", gateway_id])
    
    # Listen address
    print_header("Proxy Settings")
    listen_host = get_text_input("Listen host", default="0.0.0.0")
    listen_port = get_int_input("Listen port", default=3128, min_val=1, max_val=65535)
    args.extend(["--listen", f"{listen_host}:{listen_port}"])
    
    # Log level
    print_header("Advanced Options")
    log_levels = [
        (1, "DEBUG - Detailed debugging information"),
        (2, "INFO - General information (recommended)"),
        (3, "WARNING - Warnings and errors only"),
        (4, "ERROR - Errors only"),
    ]
    log_choice = print_menu(log_levels, "Select log level")
    log_level = ["DEBUG", "INFO", "WARNING", "ERROR"][log_choice - 1]
    args.extend(["--log-level", log_level])
    
    # Log file (optional)
    log_file = get_text_input("Log file path (optional, press Enter to skip)", required=False)
    if log_file:
        args.extend(["--log-file", log_file])
    
    # Window size
    window_size = get_int_input("Sliding window size", default=4, min_val=1, max_val=32)
    args.extend(["--window-size", str(window_size)])
    
    # Retransmit timeout
    retransmit_timeout = get_int_input("Retransmit timeout (ms)", default=5000, min_val=1000)
    args.extend(["--retransmit-timeout", str(retransmit_timeout)])
    
    return args


def main():
    """Main interactive CLI entry point."""
    try:
        clear_screen()
        print_header("LoRa WiFi Forwarder - Interactive Setup")
        
        mode_options = [
            (1, "Gateway Mode - Bridge LoRa mesh to Internet"),
            (2, "Client Mode - Provide WiFi proxy for local devices"),
        ]
        
        mode_choice = print_menu(mode_options, "Select mode")
        
        if mode_choice == 1:
            # Gateway mode
            args = configure_gateway()
            cmd = ["gatewayd"] + args
        else:
            # Client mode
            args = configure_client()
            cmd = ["clientd"] + args
        
        # Show summary
        print_header("Configuration Summary")
        print("Command to run:")
        print(f"  {' '.join(cmd)}\n")
        
        confirm = get_text_input("Start daemon now? (yes/no)", default="yes", required=False)
        if confirm.lower() not in ["yes", "y"]:
            print("\nConfiguration cancelled.")
            sys.exit(0)
        
        # Run the daemon
        print("\n" + "=" * 60)
        print("Starting daemon...")
        print("Press Ctrl+C to stop")
        print("=" * 60 + "\n")
        
        try:
            subprocess.run(cmd, check=False)
        except KeyboardInterrupt:
            print("\n\nStopping daemon...")
            sys.exit(0)
        except FileNotFoundError:
            print(f"\nError: Command '{cmd[0]}' not found.")
            print("Make sure the package is installed: pip install -e .")
            sys.exit(1)
            
    except KeyboardInterrupt:
        print("\n\nCancelled.")
        sys.exit(0)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
