"""Estimate real-world range for each Meshtastic modem mode.

This script measures signal strength at low TX power and calculates
estimated range for each modem preset using link budget analysis.

Physics:
- Free Space Path Loss: FSPL(dB) = 20*log10(d_km) + 20*log10(f_MHz) + 32.44
- For 915 MHz (US): FSPL ~ 20*log10(d_km) + 91.5
- Link margin determines how much extra loss we can tolerate

Each modem mode has different receiver sensitivity (ability to decode weak signals):
- Longer range modes use slower data rates = better sensitivity
- Shorter range modes use faster data rates = worse sensitivity
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

# Add parent directory to path for imports
# Add project root to path for bridge imports
BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

import pytest

# Skip this entire module if meshtastic is not available
pytest.importorskip("meshtastic")

from meshtastic import config_pb2, serial_interface

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Receiver sensitivities for each modem mode (dBm)
# These are approximate values - actual values depend on hardware
# More negative = can hear weaker signals = longer range
RX_SENSITIVITIES = {
    "SHORT_TURBO": -108,  # Fastest, shortest range
    "SHORT_FAST": -112,
    "SHORT_SLOW": -115,
    "MEDIUM_FAST": -119,
    "MEDIUM_SLOW": -122,
    "LONG_FAST": -127,  # Default mode
    "LONG_MODERATE": -131,
    "LONG_SLOW": -134,
    "VERY_LONG_SLOW": -137,  # Slowest, longest range
}

# Modem presets enum values
MODEM_PRESETS = {
    "LONG_FAST": config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST,
    "LONG_SLOW": config_pb2.Config.LoRaConfig.ModemPreset.LONG_SLOW,
    "LONG_MODERATE": config_pb2.Config.LoRaConfig.ModemPreset.LONG_MODERATE,
    "VERY_LONG_SLOW": config_pb2.Config.LoRaConfig.ModemPreset.VERY_LONG_SLOW,
    "SHORT_FAST": config_pb2.Config.LoRaConfig.ModemPreset.SHORT_FAST,
    "SHORT_SLOW": config_pb2.Config.LoRaConfig.ModemPreset.SHORT_SLOW,
    "SHORT_TURBO": config_pb2.Config.LoRaConfig.ModemPreset.SHORT_TURBO,
    "MEDIUM_FAST": config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_FAST,
    "MEDIUM_SLOW": config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_SLOW,
}

# TX Power levels (dBm) - typical Meshtastic radios
MIN_TX_POWER = 1  # 1 mW - minimum
MAX_TX_POWER = 30  # 1 W - maximum (1000 mW)
TEST_TX_POWER = 1  # Use minimum for testing

# Frequency (MHz) - US band
FREQUENCY_MHZ = 915

# Antenna gain (dBi) - YOUR ANTENNAS
# Stock antennas: ~0 to 2 dBi
# Upgraded antennas: 3-6 dBi typical
ANTENNA_GAIN_DBI = 4  # User's 4 dBi antennas

# Link margin (dB) - safety factor for real-world conditions
# Accounts for: multipath, obstacles, weather, antenna imperfections
LINK_MARGIN_OPEN = 10  # Open terrain (fields, water)
LINK_MARGIN_SUBURBAN = 20  # Suburban (some trees, buildings)
LINK_MARGIN_URBAN = 30  # Urban (buildings, interference)
LINK_MARGIN_INDOOR = 40  # Indoor/obstructed


@dataclass
class RangeEstimate:
    mode_name: str
    rx_sensitivity: int
    max_path_loss: float
    range_open_km: float
    range_suburban_km: float
    range_urban_km: float
    range_indoor_km: float
    measured_rssi: Optional[float] = None
    measured_snr: Optional[float] = None


def calculate_realistic_distance(
    path_loss_db: float, frequency_mhz: float = 915, environment: str = "suburban"
) -> float:
    """Calculate distance using realistic path loss model calibrated to real Meshtastic data.

    Calibrated against real-world Meshtastic community performance:
    - World record VERY_LONG_SLOW: ~254 km over water (elevated antennas, ideal)
    - Typical LONG_FAST suburban: 5-15 km (ground level antennas)
    - Typical SHORT_TURBO suburban: 1-3 km

    Uses log-distance path loss model with environment-specific exponents.
    CONSERVATIVELY calibrated to match real community reports.
    """
    # Path loss exponents calibrated to match REAL Meshtastic community reports
    # Higher exponents = more loss = shorter range
    path_loss_exponents = {
        "open": 2.6,  # Open terrain - still has ground bounce, Fresnel issues
        "suburban": 4.0,  # Trees, houses, fences, cars - REALISTIC for ground level
        "urban": 4.8,  # Dense buildings, heavy obstructions
        "indoor": 5.5,  # Through multiple walls
    }

    n = path_loss_exponents.get(environment, 4.0)

    # Reference path loss at 1 km calibrated to real-world Meshtastic data
    # Based on: 915 MHz, ground effects, typical antenna heights (~1-2m),
    # Fresnel zone violations, real antenna patterns, atmospheric absorption
    pl_1km = 105.0  # Realistic base loss at 1km

    # Calculate distance: d = 10^((PL - PL_1km) / (10*n))
    distance_km = 10 ** ((path_loss_db - pl_1km) / (10 * n))

    return max(0.01, distance_km)  # Minimum 10m


def calculate_max_range(
    tx_power_dbm: float,
    rx_sensitivity_dbm: float,
    link_margin_db: float,
    frequency_mhz: float = 915,
    antenna_gain_db: float = ANTENNA_GAIN_DBI,  # Use configured antenna gain
    environment: str = "suburban",
) -> float:
    """Calculate maximum range in km using realistic path loss model.

    Based on real-world Meshtastic community performance data:
    - VERY_LONG_SLOW has achieved 254 km (world record, elevated antennas)
    - LONG_FAST typically gets 5-15 km suburban (ground level)
    - SHORT_TURBO typically gets 1-3 km suburban
    """
    # Total antenna gain (TX + RX antennas)
    # 4 dBi each side = 8 dB total system gain
    total_antenna_gain = antenna_gain_db * 2

    # Maximum allowable path loss
    max_path_loss = tx_power_dbm + total_antenna_gain - rx_sensitivity_dbm - link_margin_db

    # Convert to distance using realistic model
    distance_km = calculate_realistic_distance(max_path_loss, frequency_mhz, environment)

    return distance_km


def get_interface(port: str) -> serial_interface.SerialInterface:
    """Open a serial interface to a Meshtastic radio."""
    return serial_interface.SerialInterface(port)


def set_tx_power(interface: serial_interface.SerialInterface, power_dbm: int) -> bool:
    """Set the TX power on a radio."""
    try:
        config = interface.localNode.localConfig
        config.lora.tx_power = power_dbm
        interface.localNode.writeConfig("lora")
        time.sleep(2)  # Wait for config to apply
        return True
    except Exception as e:
        logger.error(f"Failed to set TX power: {e}")
        return False


def set_modem_preset(interface: serial_interface.SerialInterface, preset: int) -> bool:
    """Set the modem preset on a radio."""
    try:
        config = interface.localNode.localConfig
        config.lora.modem_preset = preset
        interface.localNode.writeConfig("lora")
        time.sleep(2)
        return True
    except Exception as e:
        logger.error(f"Failed to set modem preset: {e}")
        return False


def get_rssi_snr_from_nodes(
    interface: serial_interface.SerialInterface, target_node_id: str
) -> Tuple[Optional[float], Optional[float]]:
    """Get RSSI and SNR for a specific node from the node database."""
    try:
        nodes = interface.nodes
        for node_id, node_info in nodes.items():
            # Check if this is our target node
            if target_node_id in str(node_id) or (
                node_info.get("user", {}).get("id", "") == target_node_id
            ):
                node_info.get("snr")  # Actually this might be wrong field
                # The actual RSSI is usually in the last received packet info
                # Let's try to get it from hopsAway or other fields
                node_info.get("position", {})
                node_info.get("lastHeard")  # Not RSSI but timestamp

                # Actually, the RSSI/SNR is stored differently
                # We need to look at the raw packet data
                break
        return None, None
    except Exception as e:
        logger.error(f"Error getting RSSI/SNR: {e}")
        return None, None


def measure_signal_strength(
    tx_interface: serial_interface.SerialInterface,
    rx_interface: serial_interface.SerialInterface,
    tx_node_id: str,
) -> Tuple[Optional[float], Optional[float]]:
    """Measure signal strength by sending a message and checking received signal.

    Returns (RSSI, SNR) or (None, None) if measurement failed.
    """
    import threading

    from pubsub import pub

    rssi_result = [None]
    snr_result = [None]
    message_received = threading.Event()

    def on_receive(packet, interface):
        """Callback for received packets."""
        if interface is not rx_interface:
            return

        # Check if packet has RSSI/SNR info
        if "rxRssi" in packet:
            rssi_result[0] = packet.get("rxRssi")
        if "rxSnr" in packet:
            snr_result[0] = packet.get("rxSnr")

        # Also check in the raw packet
        if rssi_result[0] is None and "rssi" in packet:
            rssi_result[0] = packet.get("rssi")
        if snr_result[0] is None and "snr" in packet:
            snr_result[0] = packet.get("snr")

        message_received.set()

    # Subscribe to receive events
    pub.subscribe(on_receive, "meshtastic.receive")

    try:
        # Send a test message
        logger.info("Sending test message for signal measurement...")
        tx_interface.sendText("RSSI_TEST", wantAck=True)

        # Wait for response
        if message_received.wait(timeout=30):
            logger.info(f"Measured RSSI: {rssi_result[0]} dBm, SNR: {snr_result[0]} dB")
        else:
            logger.warning("No message received for signal measurement")

    except Exception as e:
        logger.error(f"Error during signal measurement: {e}")
    finally:
        try:
            pub.unsubscribe(on_receive, "meshtastic.receive")
        except (AttributeError, RuntimeError) as e:
            logger.debug(f"Exception during pubsub cleanup: {e}")

    return rssi_result[0], snr_result[0]


def estimate_ranges_for_all_modes(
    tx_power_dbm: float = MAX_TX_POWER,
    frequency_mhz: float = FREQUENCY_MHZ,
    antenna_gain_dbi: float = ANTENNA_GAIN_DBI,
    measured_rssi: Optional[float] = None,
    measured_snr: Optional[float] = None,
) -> List[RangeEstimate]:
    """Calculate range estimates for all modem modes."""

    results = []

    for mode_name, rx_sensitivity in RX_SENSITIVITIES.items():
        # Calculate ranges for different environments
        # Use smaller margins since we're already using realistic path loss model
        range_open = calculate_max_range(
            tx_power_dbm,
            rx_sensitivity,
            6,
            frequency_mhz,
            antenna_gain_db=antenna_gain_dbi,
            environment="open",
        )
        range_suburban = calculate_max_range(
            tx_power_dbm,
            rx_sensitivity,
            10,
            frequency_mhz,
            antenna_gain_db=antenna_gain_dbi,
            environment="suburban",
        )
        range_urban = calculate_max_range(
            tx_power_dbm,
            rx_sensitivity,
            15,
            frequency_mhz,
            antenna_gain_db=antenna_gain_dbi,
            environment="urban",
        )
        range_indoor = calculate_max_range(
            tx_power_dbm,
            rx_sensitivity,
            20,
            frequency_mhz,
            antenna_gain_db=antenna_gain_dbi,
            environment="indoor",
        )

        # Max path loss for reference
        max_path_loss = tx_power_dbm - rx_sensitivity - 6

        results.append(
            RangeEstimate(
                mode_name=mode_name,
                rx_sensitivity=rx_sensitivity,
                max_path_loss=max_path_loss,
                range_open_km=range_open,
                range_suburban_km=range_suburban,
                range_urban_km=range_urban,
                range_indoor_km=range_indoor,
                measured_rssi=measured_rssi,
                measured_snr=measured_snr,
            )
        )

    # Sort by range (longest first)
    results.sort(key=lambda x: x.range_open_km, reverse=True)

    return results


def format_distance(km: float) -> str:
    """Format distance nicely."""
    if km >= 1.0:
        return f"{km:.1f} km"
    else:
        return f"{km * 1000:.0f} m"


def format_distance_miles(km: float) -> str:
    """Format distance in miles."""
    miles = km * 0.621371
    if miles >= 1.0:
        return f"{miles:.1f} mi"
    else:
        feet = miles * 5280
        return f"{feet:.0f} ft"


def main():
    print("=" * 80)
    print("Meshtastic Range Estimation")
    print("=" * 80)
    print()
    print("This tool estimates real-world range for each Meshtastic modem mode")
    print("based on link budget analysis and receiver sensitivity.")
    print()
    print("Assumptions:")
    print(f"  - TX Power: {MAX_TX_POWER} dBm (1W)")
    print(f"  - Frequency: {FREQUENCY_MHZ} MHz (US band)")
    print(
        f"  - Antenna gain: {ANTENNA_GAIN_DBI} dBi each ({ANTENNA_GAIN_DBI * 2} dB total system gain)"
    )
    print("  - Calibrated to match real community reports (conservative)")
    print("=" * 80)

    # Try to measure actual signal strength if radios are connected
    measured_rssi = None
    measured_snr = None

    print("\n[1/2] Checking for connected radios...")
    try:
        com8_interface = get_interface("COM8")
        com9_interface = get_interface("COM9")
        time.sleep(2)

        print("  Radios connected! Measuring signal strength...")

        # Get node IDs
        com8_node = com8_interface.getMyNodeInfo()
        com9_node = com9_interface.getMyNodeInfo()
        com8_raw_id = com8_node["user"]["id"] if com8_node else "unknown"
        com9_raw_id = com9_node["user"]["id"] if com9_node else "unknown"

        print(f"  COM8: {com8_raw_id}")
        print(f"  COM9: {com9_raw_id}")

        # Set minimum TX power to measure path loss
        print(f"\n  Setting TX power to minimum ({TEST_TX_POWER} dBm) for measurement...")
        set_tx_power(com8_interface, TEST_TX_POWER)
        set_tx_power(com9_interface, TEST_TX_POWER)
        time.sleep(3)

        # Measure signal
        measured_rssi, measured_snr = measure_signal_strength(
            com8_interface, com9_interface, com8_raw_id
        )

        # If we got RSSI, calculate current path loss
        if measured_rssi is not None:
            path_loss = TEST_TX_POWER - measured_rssi
            print(f"\n  Measured RSSI: {measured_rssi} dBm")
            print(f"  Measured SNR: {measured_snr} dB" if measured_snr else "  SNR: Not available")
            print(f"  Current path loss: {path_loss} dB (at {TEST_TX_POWER} dBm TX)")
        else:
            print("\n  Could not measure RSSI (will use theoretical values)")

        # Restore power and close
        print("\n  Restoring TX power...")
        set_tx_power(com8_interface, MAX_TX_POWER)
        set_tx_power(com9_interface, MAX_TX_POWER)

        com8_interface.close()
        com9_interface.close()

    except Exception as e:
        print(f"  Could not connect to radios: {e}")
        print("  Using theoretical calculations only.")

    # Calculate range estimates
    print("\n[2/2] Calculating range estimates...")
    estimates = estimate_ranges_for_all_modes(
        tx_power_dbm=MAX_TX_POWER,
        frequency_mhz=FREQUENCY_MHZ,
        measured_rssi=measured_rssi,
        measured_snr=measured_snr,
    )

    # Print results
    print("\n" + "=" * 80)
    print("ESTIMATED RANGE BY MODEM MODE")
    print("=" * 80)
    print()
    print(
        f"{'Mode':<18} {'Sensitivity':<12} {'Open':<12} {'Suburban':<12} {'Urban':<12} {'Indoor':<10}"
    )
    print(
        f"{'':18} {'(dBm)':<12} {'(line-of-sight)':<12} {'(trees)':<12} {'(buildings)':<12} {'(walls)':<10}"
    )
    print("-" * 80)

    for est in estimates:
        print(
            f"{est.mode_name:<18} {est.rx_sensitivity:<12} "
            f"{format_distance(est.range_open_km):<12} "
            f"{format_distance(est.range_suburban_km):<12} "
            f"{format_distance(est.range_urban_km):<12} "
            f"{format_distance(est.range_indoor_km):<10}"
        )

    print("-" * 80)

    # Print in miles too
    print("\n" + "=" * 80)
    print("ESTIMATED RANGE (MILES)")
    print("=" * 80)
    print()
    print(f"{'Mode':<18} {'Open':<12} {'Suburban':<12} {'Urban':<12} {'Indoor':<10}")
    print("-" * 80)

    for est in estimates:
        print(
            f"{est.mode_name:<18} "
            f"{format_distance_miles(est.range_open_km):<12} "
            f"{format_distance_miles(est.range_suburban_km):<12} "
            f"{format_distance_miles(est.range_urban_km):<12} "
            f"{format_distance_miles(est.range_indoor_km):<10}"
        )

    print("-" * 80)

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    best_range = estimates[0]
    fastest_mode = "SHORT_TURBO"

    print(
        f"""
Best for RANGE: {best_range.mode_name}
  - Open terrain: {format_distance(best_range.range_open_km)} ({format_distance_miles(best_range.range_open_km)})
  - Suburban: {format_distance(best_range.range_suburban_km)} ({format_distance_miles(best_range.range_suburban_km)})

Best for SPEED: {fastest_mode}
  - Open terrain: {format_distance(estimates[-1].range_open_km)} ({format_distance_miles(estimates[-1].range_open_km)})
  - But ~10x faster data rate than VERY_LONG_SLOW

RECOMMENDED for most uses: LONG_FAST (default)
  - Good balance of range and speed
  - Open terrain: ~{format_distance([e for e in estimates if e.mode_name == 'LONG_FAST'][0].range_open_km)}
"""
    )

    # If we measured signal, show equivalent distance calculation
    if measured_rssi is not None:
        print("=" * 80)
        print("YOUR RADIOS - SIMULATED RANGE TEST")
        print("=" * 80)

        # At minimum TX power, we measured a certain path loss
        # At full TX power, that same path loss would occur at a much greater distance
        path_loss_at_min_power = TEST_TX_POWER - measured_rssi

        # The extra power available at max TX
        extra_power = MAX_TX_POWER - TEST_TX_POWER  # 29 dB more power

        # This means we can tolerate 29dB more path loss
        # Which translates to much greater distance

        print(
            f"""
Your measured signal at minimum power ({TEST_TX_POWER} dBm):
  - RSSI: {measured_rssi} dBm
  - SNR: {measured_snr} dB
  - Path loss: {path_loss_at_min_power} dB

At full power ({MAX_TX_POWER} dBm), you'd have {extra_power} dB more link budget.
This simulates being {2**(extra_power/10):.1f}x farther away!

EQUIVALENT DISTANCES if you maintained connection at min power:
(Your radios side-by-side at {TEST_TX_POWER} dBm ~ these distances at {MAX_TX_POWER} dBm)
"""
        )

        # At full power, what would the RSSI be at your current distance?
        rssi_at_full_power = MAX_TX_POWER - path_loss_at_min_power

        print(
            f"{'Mode':<18} {'Sensitivity':<12} {'Margin at Full Power':<22} {'Max Range This Mode'}"
        )
        print("-" * 80)

        for est in estimates:
            # How much margin do we have above the sensitivity at full power?
            margin = rssi_at_full_power - est.rx_sensitivity

            if margin > 0:
                # Calculate max path loss for this mode
                max_path_loss = MAX_TX_POWER - est.rx_sensitivity - 6  # 6dB safety margin

                # Calculate max range
                max_range_open = calculate_realistic_distance(max_path_loss, FREQUENCY_MHZ, "open")
                max_range_suburban = calculate_realistic_distance(
                    max_path_loss, FREQUENCY_MHZ, "suburban"
                )

                margin_str = f"+{margin} dB"
                dist_str = f"{format_distance(max_range_suburban)} sub / {format_distance(max_range_open)} open"
            else:
                margin_str = f"{margin} dB FAIL"
                dist_str = "Below sensitivity!"

            print(f"{est.mode_name:<18} {est.rx_sensitivity} dBm    {margin_str:<22} {dist_str}")

        print("-" * 80)
        print(
            f"""
INTERPRETATION:
- At minimum power ({TEST_TX_POWER} dBm), measured RSSI: {measured_rssi} dBm
- Path loss between your radios: {path_loss_at_min_power} dB
- At full power ({MAX_TX_POWER} dBm), RSSI would be: {rssi_at_full_power} dBm (very strong!)
- All modes have huge margin - your radios are VERY close together

This is expected when testing on a desk. In real deployment:
- Path loss increases ~{20/2.3:.0f}x for every 10x distance (open terrain)
- Path loss increases ~{20/3.2:.0f}x for every 10x distance (suburban)

Your {path_loss_at_min_power} dB path loss at {TEST_TX_POWER} dBm is equivalent to testing at full power
from ~{format_distance(calculate_realistic_distance(path_loss_at_min_power + extra_power, FREQUENCY_MHZ, 'suburban'))} in suburban or ~{format_distance(calculate_realistic_distance(path_loss_at_min_power + extra_power, FREQUENCY_MHZ, 'open'))} open terrain.
"""
        )

    print(
        """
Note: Real-world range varies significantly based on:
  - Antenna height and quality (higher = better)
  - Terrain and obstacles (hills, buildings)
  - Weather conditions
  - RF interference in your area
  - Fresnel zone clearance
"""
    )

    print("Test complete!")


if __name__ == "__main__":
    main()
