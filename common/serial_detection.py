"""Automatic serial port detection for Meshtastic devices."""

import sys
import threading
from typing import Optional

from common.logging_setup import get_logger

logger = get_logger(__name__)

# Timeout for port detection (seconds)
PORT_DETECTION_TIMEOUT = 3


def detect_meshtastic_port() -> Optional[str]:
    """
    Automatically detect a Meshtastic device serial port.
    
    Returns:
        Serial port path if found, None otherwise
    """
    try:
        import serial.tools.list_ports
    except ImportError:
        logger.warning("pyserial not available for port detection")
        return None
    
    # Get list of available serial ports
    ports = serial.tools.list_ports.comports()
    
    if not ports:
        logger.debug("No serial ports found")
        return None
    
    logger.debug(f"Found {len(ports)} serial port(s), testing for Meshtastic device...")
    
    # Try each port to see if it's a Meshtastic device
    for port_info in ports:
        port_path = port_info.device
        logger.debug(f"Testing port: {port_path}")
        
        try:
            # Try to connect and verify it's a Meshtastic device with timeout
            from meshtastic.serial_interface import SerialInterface
            
            detection_result = {"success": False, "port": None, "node_id": None, "interface": None}
            detection_error = {"error": None}
            
            def test_port():
                """Test a single port for Meshtastic device."""
                try:
                    # Try to connect
                    interface = SerialInterface(port_path, debugOut=None)
                    detection_result["interface"] = interface
                    
                    # Check if we got node info (indicates it's a Meshtastic device)
                    if interface.myInfo:
                        node_id = interface.myInfo.my_node_num
                        detection_result["success"] = True
                        detection_result["port"] = port_path
                        detection_result["node_id"] = node_id
                except Exception as e:
                    detection_error["error"] = e
                finally:
                    interface = detection_result.get("interface")
                    if interface:
                        try:
                            interface.close()
                        except Exception:
                            pass
            
            # Run detection in a thread with timeout
            thread = threading.Thread(target=test_port, daemon=True)
            thread.start()
            thread.join(timeout=PORT_DETECTION_TIMEOUT)
            
            if thread.is_alive():
                # Thread is still running - timeout occurred
                logger.debug(
                    f"Port {port_path} detection timed out after {PORT_DETECTION_TIMEOUT}s"
                )
                continue
            
            # Check result
            if detection_result["success"]:
                node_id = detection_result.get("node_id", 0)
                logger.info(f"Found Meshtastic device on {port_path} (node ID: {node_id:#x})")
                return port_path
            elif detection_error["error"]:
                logger.debug(f"Port {port_path} is not a Meshtastic device: {detection_error['error']}")
            
        except Exception as e:
            logger.debug(f"Port {port_path} detection failed: {e}")
            continue
    
    logger.warning("No Meshtastic device found on any serial port")
    return None


def get_default_serial_port() -> str:
    """
    Get default serial port based on platform.
    
    Returns:
        Default serial port path for the current platform
    """
    if sys.platform.startswith("win"):
        return "COM1"  # Windows default
    elif sys.platform.startswith("darwin"):
        return "/dev/tty.usbserial"  # macOS default
    else:
        return "/dev/ttyUSB0"  # Linux default


def find_serial_port(requested_port: Optional[str] = None) -> str:
    """
    Find serial port for Meshtastic device.
    
    If a port is requested, use it. Otherwise, try to auto-detect.
    Falls back to platform default if auto-detection fails.
    
    Args:
        requested_port: Explicitly requested serial port (optional)
        
    Returns:
        Serial port path to use
    """
    if requested_port:
        logger.info(f"Using requested serial port: {requested_port}")
        return requested_port
    
    logger.info("Auto-detecting Meshtastic serial port...")
    detected_port = detect_meshtastic_port()
    
    if detected_port:
        return detected_port
    
    default_port = get_default_serial_port()
    logger.warning(
        f"Could not auto-detect Meshtastic device, using default: {default_port}"
    )
    logger.warning(
        "If this is incorrect, specify --serial <port> explicitly"
    )
    return default_port
