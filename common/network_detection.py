"""Automatic network interface detection for internet connectivity."""

import sys
import socket
import subprocess
from typing import Optional

from common.logging_setup import get_logger

logger = get_logger(__name__)


def detect_internet_interface() -> Optional[str]:
    """
    Automatically detect the network interface with internet connectivity.
    
    Returns:
        Interface name if found, None otherwise
    """
    if sys.platform.startswith("win"):
        return _detect_windows_interface()
    else:
        return _detect_unix_interface()


def _detect_windows_interface() -> Optional[str]:
    """Detect internet interface on Windows."""
    try:
        # Use PowerShell to get the interface with the default route
        # This finds the interface used for the default gateway
        ps_script = (
            "$route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | "
            "Where-Object { $_.NextHop -ne $null } | Select-Object -First 1; "
            "if ($route) { $route.InterfaceAlias }"
        )
        cmd = ["powershell", "-Command", ps_script]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        
        if result.returncode == 0 and result.stdout.strip():
            interface = result.stdout.strip()
            # Filter out empty lines and errors
            if interface and not interface.startswith("Get-NetRoute"):
                logger.info(f"Detected internet interface on Windows: {interface}")
                return interface
        
    except Exception as e:
        logger.debug(f"Failed to detect Windows interface via PowerShell route: {e}")
    
    # Fallback: Try to get active network adapters (excluding loopback)
    try:
        ps_script = (
            "$adapter = Get-NetAdapter | Where-Object { "
            "$_.Status -eq 'Up' -and $_.InterfaceDescription -notlike '*Loopback*' "
            "} | Select-Object -First 1; "
            "if ($adapter) { $adapter.Name }"
        )
        cmd = ["powershell", "-Command", ps_script]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        
        if result.returncode == 0 and result.stdout.strip():
            interface = result.stdout.strip()
            if interface:
                logger.info(f"Detected active network interface on Windows: {interface}")
                return interface
            
    except Exception as e:
        logger.debug(f"Failed to detect Windows interface via fallback: {e}")
    
    return None


def _detect_unix_interface() -> Optional[str]:
    """Detect internet interface on Linux/macOS."""
    # Try to get default route interface
    try:
        # Check /proc/net/route on Linux
        if sys.platform.startswith("linux"):
            with open("/proc/net/route", "r") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[1] == "00000000":
                        # Found default route
                        interface = parts[0]
                        logger.info(f"Detected internet interface on Linux: {interface}")
                        return interface
        
        # Try 'ip route' command (Linux)
        if sys.platform.startswith("linux"):
            result = subprocess.run(
                ["ip", "route", "show", "default"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                # Parse output like "default via 192.168.1.1 dev eth0"
                for line in result.stdout.splitlines():
                    if "dev" in line:
                        parts = line.split()
                        try:
                            dev_index = parts.index("dev")
                            if dev_index + 1 < len(parts):
                                interface = parts[dev_index + 1]
                                logger.info(f"Detected internet interface via ip route: {interface}")
                                return interface
                        except (ValueError, IndexError):
                            continue
        
        # Try 'route' command (macOS/Linux)
        result = subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # Parse output for "interface: eth0" or similar
            for line in result.stdout.splitlines():
                if "interface:" in line.lower():
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        interface = parts[1].strip()
                        logger.info(f"Detected internet interface via route: {interface}")
                        return interface
        
    except Exception as e:
        logger.debug(f"Failed to detect Unix interface: {e}")
    
    # Fallback: Try to find any up interface (excluding loopback)
    try:
        import netifaces
        gateways = netifaces.gateways()
        if "default" in gateways:
            default_gw = gateways["default"]
            if default_gw:
                # Get the first default gateway
                interface = default_gw[netifaces.AF_INET][1]
                logger.info(f"Detected internet interface via netifaces: {interface}")
                return interface
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"Failed to detect interface via netifaces: {e}")
    
    return None


def get_default_interface() -> str:
    """
    Get default network interface name based on platform.
    
    Returns:
        Default interface name for the current platform
    """
    if sys.platform.startswith("win"):
        return "Ethernet"  # Windows default
    elif sys.platform.startswith("darwin"):
        return "en0"  # macOS default
    else:
        return "eth0"  # Linux default


def find_internet_interface(requested_iface: Optional[str] = None) -> str:
    """
    Find network interface with internet connectivity.
    
    If an interface is requested, use it. Otherwise, try to auto-detect.
    Falls back to platform default if auto-detection fails.
    
    Args:
        requested_iface: Explicitly requested interface name (optional)
        
    Returns:
        Interface name to use
    """
    if requested_iface:
        logger.info(f"Using requested internet interface: {requested_iface}")
        return requested_iface
    
    logger.info("Auto-detecting internet interface...")
    detected_iface = detect_internet_interface()
    
    if detected_iface:
        return detected_iface
    
    default_iface = get_default_interface()
    logger.warning(
        f"Could not auto-detect internet interface, using default: {default_iface}"
    )
    logger.warning(
        "If this is incorrect, specify --internet-iface <name> explicitly"
    )
    return default_iface
