"""Range test for Meshtastic bridge.

This script monitors signal strength (RSSI/SNR) as radios are physically separated.

Setup:
1. Connect the "base" radio to your laptop (default: COM9)
2. Power the "remote" radio with a battery pack
3. Run this script
4. Walk away with the remote radio and watch the readings

The script will:
- Send periodic pings from base to remote
- Record RSSI, SNR, and round-trip time
- Show real-time signal quality
- Log results for analysis
"""

from __future__ import annotations

import argparse
import csv
import datetime
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add parent directory to path for imports
# Add project root to path for bridge imports
BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

import pytest

# Skip this entire module if meshtastic is not available
pytest.importorskip("meshtastic")
pytest.importorskip("pubsub")

from meshtastic import serial_interface
from pubsub import pub

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


@dataclass
class PingResult:
    """Result of a single ping."""

    timestamp: datetime.datetime
    success: bool
    round_trip_ms: float
    rssi: Optional[int] = None
    snr: Optional[float] = None
    error: Optional[str] = None


@dataclass
class RangeTestStats:
    """Statistics for the range test."""

    results: List[PingResult] = field(default_factory=list)
    start_time: datetime.datetime = field(default_factory=datetime.datetime.now)

    @property
    def total_pings(self) -> int:
        return len(self.results)

    @property
    def successful_pings(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def success_rate(self) -> float:
        if self.total_pings == 0:
            return 0.0
        return (self.successful_pings / self.total_pings) * 100

    @property
    def avg_rssi(self) -> Optional[float]:
        rssi_values = [r.rssi for r in self.results if r.rssi is not None]
        if not rssi_values:
            return None
        return sum(rssi_values) / len(rssi_values)

    @property
    def avg_snr(self) -> Optional[float]:
        snr_values = [r.snr for r in self.results if r.snr is not None]
        if not snr_values:
            return None
        return sum(snr_values) / len(snr_values)

    @property
    def avg_round_trip(self) -> Optional[float]:
        successful = [r.round_trip_ms for r in self.results if r.success]
        if not successful:
            return None
        return sum(successful) / len(successful)


class RangeTester:
    """Performs range testing using Meshtastic radios."""

    def __init__(self, base_port: str, remote_node_id: str):
        self.base_port = base_port
        self.remote_node_id = remote_node_id
        self.interface: Optional[serial_interface.SerialInterface] = None
        self.stats = RangeTestStats()
        self._last_rssi: Optional[int] = None
        self._last_snr: Optional[float] = None
        self._waiting_for_ack = False
        self._ack_received = False
        self._ping_start_time: Optional[float] = None

    def connect(self) -> bool:
        """Connect to the base radio."""
        try:
            logger.info(f"Connecting to base radio on {self.base_port}...")
            self.interface = serial_interface.SerialInterface(self.base_port)
            time.sleep(2)

            # Subscribe to receive callbacks
            pub.subscribe(self._on_receive, "meshtastic.receive")

            # Get local node info
            my_info = self.interface.getMyNodeInfo()
            if my_info:
                my_id = my_info.get("user", {}).get("id", "unknown")
                logger.info(f"Connected! Base node ID: {my_id}")

            return True
        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            return False

    def disconnect(self):
        """Disconnect from the radio."""
        if self.interface:
            try:
                pub.unsubscribe(self._on_receive, "meshtastic.receive")
            except Exception:  # noqa: S110
                pass
            try:
                self.interface.close()
            except Exception:  # noqa: S110
                pass
            self.interface = None

    def _on_receive(self, packet: Dict[str, Any], interface: Any) -> None:
        """Handle received packets."""
        if interface is not self.interface:
            return

        # Extract signal info
        self._last_rssi = packet.get("rxRssi") or packet.get("rssi")
        self._last_snr = packet.get("rxSnr") or packet.get("snr")

        # Check if this is from our remote node
        from_id = packet.get("fromId", "")
        if not from_id:
            from_num = packet.get("from")
            if from_num:
                from_id = f"!{from_num:08x}"

        # Normalize remote node ID for comparison
        remote_normalized = self.remote_node_id.lower().replace("!", "")
        from_normalized = from_id.lower().replace("!", "")

        if from_normalized == remote_normalized:
            self._ack_received = True
            logger.debug(
                f"Received response from {from_id}, RSSI={self._last_rssi}, SNR={self._last_snr}"
            )

    def send_ping(self, timeout: float = 30.0) -> PingResult:
        """Send a ping and wait for response."""
        if not self.interface:
            return PingResult(
                timestamp=datetime.datetime.now(),
                success=False,
                round_trip_ms=0,
                error="Not connected",
            )

        self._waiting_for_ack = True
        self._ack_received = False
        self._last_rssi = None
        self._last_snr = None

        ping_time = time.time()
        timestamp = datetime.datetime.now()

        try:
            # Send a simple text ping with ACK requested
            # Using sendText for simplicity - it's more reliable for range testing
            self.interface.sendText(
                f"PING {int(ping_time)}",
                destinationId=self.remote_node_id,
                wantAck=True,
                wantResponse=False,
            )

            # Wait for ACK or timeout
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self._ack_received:
                    round_trip = (time.time() - ping_time) * 1000
                    return PingResult(
                        timestamp=timestamp,
                        success=True,
                        round_trip_ms=round_trip,
                        rssi=self._last_rssi,
                        snr=self._last_snr,
                    )
                time.sleep(0.1)

            # Timeout
            return PingResult(
                timestamp=timestamp,
                success=False,
                round_trip_ms=(time.time() - ping_time) * 1000,
                rssi=self._last_rssi,
                snr=self._last_snr,
                error="Timeout",
            )

        except Exception as e:
            return PingResult(
                timestamp=timestamp,
                success=False,
                round_trip_ms=(time.time() - ping_time) * 1000,
                error=str(e),
            )
        finally:
            self._waiting_for_ack = False

    def run_continuous_test(
        self,
        interval: float = 10.0,
        duration: Optional[float] = None,
        output_file: Optional[str] = None,
    ) -> RangeTestStats:
        """
        Run continuous range testing.

        Args:
            interval: Seconds between pings
            duration: Total test duration in seconds (None = run until Ctrl+C)
            output_file: Optional CSV file to save results
        """
        self.stats = RangeTestStats()

        print("\n" + "=" * 70)
        print("RANGE TEST STARTED")
        print("=" * 70)
        print(f"Base station: {self.base_port}")
        print(f"Remote node: {self.remote_node_id}")
        print(f"Ping interval: {interval}s")
        if duration:
            print(f"Duration: {duration}s")
        else:
            print("Duration: Until Ctrl+C")
        print("=" * 70)
        print("\nMove the remote radio away and watch the signal strength!")
        print("Signal quality guide:")
        print("  RSSI: -50 to -90 = Good, -90 to -110 = Weak, < -110 = Very weak")
        print("  SNR:  > 5 = Good, 0 to 5 = OK, < 0 = Weak")
        print("=" * 70)
        print()

        start_time = time.time()
        ping_num = 0

        try:
            while True:
                if duration and (time.time() - start_time) >= duration:
                    break

                ping_num += 1
                print(f"\n[Ping #{ping_num}]", end=" ")

                result = self.send_ping()
                self.stats.results.append(result)

                if result.success:
                    rssi_str = f"{result.rssi} dBm" if result.rssi else "N/A"
                    snr_str = f"{result.snr:.1f} dB" if result.snr else "N/A"

                    # Color-code signal quality
                    signal_quality = self._get_signal_quality(result.rssi, result.snr)

                    print(
                        f"OK - {result.round_trip_ms:.0f}ms | RSSI: {rssi_str} | SNR: {snr_str} | {signal_quality}"
                    )
                else:
                    print(f"FAIL - {result.error}")

                # Print running stats every 5 pings
                if ping_num % 5 == 0:
                    self._print_running_stats()

                # Wait for next ping
                if duration is None or (time.time() - start_time) < duration:
                    time.sleep(interval)

        except KeyboardInterrupt:
            print("\n\nTest stopped by user.")

        # Save results to CSV if requested
        if output_file:
            self._save_results_csv(output_file)

        return self.stats

    def _get_signal_quality(self, rssi: Optional[int], snr: Optional[float]) -> str:
        """Get a human-readable signal quality indicator."""
        if rssi is None and snr is None:
            return "[UNKNOWN]"

        # Score based on RSSI
        rssi_score = 0
        if rssi is not None:
            if rssi > -70:
                rssi_score = 3  # Excellent
            elif rssi > -90:
                rssi_score = 2  # Good
            elif rssi > -110:
                rssi_score = 1  # Weak
            else:
                rssi_score = 0  # Very weak

        # Score based on SNR
        snr_score = 0
        if snr is not None:
            if snr > 10:
                snr_score = 3  # Excellent
            elif snr > 5:
                snr_score = 2  # Good
            elif snr > 0:
                snr_score = 1  # OK
            else:
                snr_score = 0  # Weak

        # Average scores
        if rssi is not None and snr is not None:
            avg_score = (rssi_score + snr_score) / 2
        elif rssi is not None:
            avg_score = rssi_score
        else:
            avg_score = snr_score

        if avg_score >= 2.5:
            return "[EXCELLENT]"
        elif avg_score >= 1.5:
            return "[GOOD]"
        elif avg_score >= 0.5:
            return "[WEAK]"
        else:
            return "[VERY WEAK]"

    def _print_running_stats(self):
        """Print running statistics."""
        print(
            f"\n  --- Stats: {self.stats.successful_pings}/{self.stats.total_pings} success ({self.stats.success_rate:.0f}%)"
        )
        if self.stats.avg_rssi:
            print(
                f"       Avg RSSI: {self.stats.avg_rssi:.0f} dBm, Avg SNR: {self.stats.avg_snr:.1f} dB"
            )

    def _save_results_csv(self, filename: str):
        """Save results to CSV file."""
        try:
            with open(filename, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp", "success", "round_trip_ms", "rssi", "snr", "error"])
                for r in self.stats.results:
                    writer.writerow(
                        [
                            r.timestamp.isoformat(),
                            r.success,
                            r.round_trip_ms,
                            r.rssi or "",
                            r.snr or "",
                            r.error or "",
                        ]
                    )
            logger.info(f"Results saved to {filename}")
        except Exception as e:
            logger.error(f"Failed to save results: {e}")


def print_final_summary(stats: RangeTestStats):
    """Print final test summary."""
    print("\n" + "=" * 70)
    print("RANGE TEST SUMMARY")
    print("=" * 70)
    print(f"Total pings: {stats.total_pings}")
    print(f"Successful: {stats.successful_pings} ({stats.success_rate:.1f}%)")
    print(f"Failed: {stats.total_pings - stats.successful_pings}")

    if stats.avg_round_trip:
        print(f"\nAverage round-trip: {stats.avg_round_trip:.0f} ms")
    if stats.avg_rssi:
        print(f"Average RSSI: {stats.avg_rssi:.0f} dBm")
    if stats.avg_snr:
        print(f"Average SNR: {stats.avg_snr:.1f} dB")

    # Find best/worst readings
    successful = [r for r in stats.results if r.success]
    if successful:
        best_rssi = max((r for r in successful if r.rssi), key=lambda x: x.rssi, default=None)
        worst_rssi = min((r for r in successful if r.rssi), key=lambda x: x.rssi, default=None)

        if best_rssi:
            print(f"\nBest RSSI: {best_rssi.rssi} dBm")
        if worst_rssi:
            print(f"Worst RSSI: {worst_rssi.rssi} dBm")

    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Meshtastic range test")
    parser.add_argument("--port", default="COM9", help="Serial port for base radio")
    parser.add_argument("--remote", required=True, help="Remote node ID (e.g., !db583ef4)")
    parser.add_argument("--interval", type=float, default=10.0, help="Seconds between pings")
    parser.add_argument(
        "--duration", type=float, help="Test duration in seconds (default: until Ctrl+C)"
    )
    parser.add_argument("--output", help="CSV file to save results")
    args = parser.parse_args()

    # Normalize remote node ID
    remote_id = args.remote
    if not remote_id.startswith("!"):
        remote_id = f"!{remote_id}"

    tester = RangeTester(args.port, remote_id)

    if not tester.connect():
        print("Failed to connect. Exiting.")
        return 1

    try:
        stats = tester.run_continuous_test(
            interval=args.interval,
            duration=args.duration,
            output_file=args.output,
        )
        print_final_summary(stats)
    finally:
        tester.disconnect()

    return 0


if __name__ == "__main__":
    sys.exit(main())
