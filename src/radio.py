from __future__ import annotations

import logging
import queue
import threading
import time
from typing import TYPE_CHECKING, Any

from message import parse_chunk
from transport import InMemoryRadio, RadioInterface

if TYPE_CHECKING:
    from meshtastic import serial_interface

LOGGER = logging.getLogger(__name__)


class SerialRadioAdapter:
    def __init__(
        self,
        interface: "serial_interface.SerialInterface",
        *,
        disable_dedupe: bool = False,
    ) -> None:  # type: ignore[name-defined]
        self._interface = interface
        self._message_queue: queue.Queue[tuple[str, bytes]] = queue.Queue()
        self._subscribed = False
        self._numeric_to_user_id: dict[str, str] = {}  # Cache for numeric ID -> user ID mapping
        self._recent_messages: dict[
            tuple[str, str, int, int, int] | tuple[str, int], float
        ] = {}  # Deduplicate recent messages (sender, chunk header or payload hash)
        self._dedupe_ttl_seconds = 8.0
        self._message_lock = threading.Lock()  # Thread-safe access to recent_messages
        self._disable_dedupe = disable_dedupe
        # Periodic cleanup state (avoid O(n) scan on every message)
        self._dedupe_cleanup_counter: int = 0
        self._dedupe_cleanup_every_n: int = 25  # Cleanup every 25 messages
        self._last_dedupe_cleanup: float = 0.0

        # Check and log radio configuration
        self._check_radio_config()

        # Subscribe to receive events using pubsub
        try:
            from pubsub import pub

            # Subscribe to all receive events (will filter by portnum in callback)
            pub.subscribe(self._on_receive, "meshtastic.receive")
            self._subscribed = True
            LOGGER.debug("Subscribed to meshtastic.receive events")
        except ImportError:
            LOGGER.warning("pubsub not available, using polling mode")

    def _check_radio_config(self) -> None:
        """Check and log radio configuration settings."""
        try:
            # Get radio configuration
            if hasattr(self._interface, "getNodeInfo"):
                node_info = self._interface.getNodeInfo()
                if node_info:
                    LOGGER.info("[RADIO] Node info available")

            # Try to get radio config/prefs
            if hasattr(self._interface, "get"):
                try:
                    # Common Meshtastic config keys
                    config_keys = ["radio", "power", "hop_limit", "lora"]
                    for key in config_keys:
                        try:
                            value = self._interface.get(key)
                            if value is not None:
                                LOGGER.info("[RADIO] Config %s: %s", key, value)
                        except Exception:  # noqa: S110
                            pass
                except Exception:  # noqa: S110
                    pass

            # Get local node info
            if hasattr(self._interface, "getMyNodeInfo"):
                my_info = self._interface.getMyNodeInfo()
                if my_info:
                    LOGGER.info(
                        "[RADIO] Local node: %s", my_info.get("user", {}).get("id", "unknown")
                    )
                    # Log radio hardware info if available
                    if "radio" in my_info:
                        LOGGER.info("[RADIO] Radio hardware: %s", my_info["radio"])
        except Exception as e:
            LOGGER.debug("[RADIO] Could not check radio config: %s", e)

    def _on_receive(self, packet: dict[str, Any], interface: Any) -> None:  # type: ignore[no-untyped-def]
        """Callback for received messages via pubsub."""
        try:
            # CRITICAL: Only process messages from OUR interface
            # Both radios subscribe to pubsub, so both callbacks fire for every message
            # We must filter to only process messages from this radio's interface
            if interface is not self._interface:
                if LOGGER.isEnabledFor(logging.DEBUG):
                    LOGGER.debug(
                        "[RADIO] Ignoring packet from non-matching interface: %s",
                        interface,
                    )
                return

            decoded = packet.get("decoded")
            if not decoded:
                return

            # Only handle PRIVATE_APP messages (our chunks)
            # Portnum can be string "PRIVATE_APP" or integer 80
            portnum = decoded.get("portnum")
            if portnum not in ("PRIVATE_APP", 80):
                return

            # Get source node ID - prefer fromId (user ID format) over from (numeric)
            # Debug: log packet keys to see what's available
            if LOGGER.isEnabledFor(logging.DEBUG):
                LOGGER.debug("Packet keys: %s", list(packet.keys()))
                LOGGER.debug(
                    "Packet fromId: %s, from: %s, decoded keys: %s",
                    packet.get("fromId"),
                    packet.get("from"),
                    list(decoded.keys()) if decoded else [],
                )

            # Get source node ID - prefer fromId (user ID format) over from (numeric)
            source = packet.get("fromId")
            numeric_id = packet.get("from")

            if not source and numeric_id:
                # fromId is None - node not yet in database (normal for first packet)
                # Use _getOrCreateByNum to create placeholder entry and derive user ID
                try:
                    numeric_id_int = int(numeric_id)

                    # Use _getOrCreateByNum to ensure node entry exists (creates placeholder with derived user ID)
                    if hasattr(self._interface, "_getOrCreateByNum"):
                        node_info = self._interface._getOrCreateByNum(numeric_id_int)
                        if node_info:
                            # Extract user ID from the node info
                            if isinstance(node_info, dict):
                                user_info = node_info.get("user")
                                if user_info and isinstance(user_info, dict):
                                    source = user_info.get("id")
                            elif hasattr(node_info, "user"):
                                user_obj = node_info.user
                                if hasattr(user_obj, "id"):
                                    source = user_obj.id

                            if source:
                                LOGGER.debug(
                                    "Derived user ID %s from numeric ID %s via _getOrCreateByNum",
                                    source,
                                    numeric_id,
                                )
                                self._numeric_to_user_id[str(numeric_id)] = source

                    # Fallback: derive user ID directly from numeric ID (hex format)
                    if not source:
                        # Format numeric ID as 8-digit hex and prepend "!" (default Meshtastic format)
                        source = f"!{numeric_id_int:08x}"
                        LOGGER.debug(
                            "Derived user ID %s from numeric ID %s (presumptive format)",
                            source,
                            numeric_id,
                        )
                        self._numeric_to_user_id[str(numeric_id)] = source

                except Exception as e:
                    LOGGER.debug("Could not derive user ID from numeric ID %s: %s", numeric_id, e)
                    # Last resort: use numeric ID as-is
                    source = numeric_id
            elif source and numeric_id:
                # Both available - cache the mapping
                self._numeric_to_user_id[str(numeric_id)] = str(source)
                LOGGER.debug("Cached mapping: %s -> %s", numeric_id, source)
            if not source:
                return
            source_str = str(source)

            # Handle payload (comes as bytes for PRIVATE_APP)
            payload_bytes = decoded.get("payload", b"")
            if not payload_bytes:
                return

            if not isinstance(payload_bytes, bytes):
                payload_bytes = str(payload_bytes).encode("utf-8")

            # Deduplicate messages using chunk header when possible (id + seq + total + flags).
            # Fall back to payload hash if parsing fails.
            try:
                flags, short_id, seq, total, _ = parse_chunk(payload_bytes)
                message_key = (source_str, short_id, seq, total, flags)
            except Exception:
                # Using Python's built-in hash for non-cryptographic deduplication
                payload_hash = hash(payload_bytes)
                message_key = (source_str, payload_hash)

            if self._disable_dedupe:
                LOGGER.debug("[RADIO] Dedupe disabled; accepting message from %s", source_str)
            else:
                if LOGGER.isEnabledFor(logging.DEBUG):
                    LOGGER.debug(
                        "[RADIO] Dedupe key for %s: %s", source_str, message_key
                    )
                with self._message_lock:
                    now = time.time()
                    
                    # Periodic cleanup instead of every message (O(n) -> amortized O(1))
                    self._dedupe_cleanup_counter += 1
                    should_cleanup = (
                        self._dedupe_cleanup_counter >= self._dedupe_cleanup_every_n
                        or now - self._last_dedupe_cleanup >= self._dedupe_ttl_seconds
                    )
                    if should_cleanup:
                        expired = [
                            key
                            for key, ts in self._recent_messages.items()
                            if now - ts >= self._dedupe_ttl_seconds
                        ]
                        for key in expired:
                            self._recent_messages.pop(key, None)
                        self._dedupe_cleanup_counter = 0
                        self._last_dedupe_cleanup = now

                    if message_key in self._recent_messages:
                        LOGGER.info(
                            "[RADIO] Duplicate message from %s (ignored, key=%s)",
                            source_str,
                            message_key,
                        )
                        return
                    # Keep last 1000 message keys for deduplication
                    if len(self._recent_messages) > 1000:
                        # Clear half of old entries (simple cleanup)
                        keys = list(self._recent_messages.keys())[500:]
                        self._recent_messages = {key: self._recent_messages[key] for key in keys}
                    self._recent_messages[message_key] = now

            payload_preview = payload_bytes[:32].hex()
            LOGGER.info(
                "[RADIO] Received PRIVATE_APP message from %s (numeric: %s): %d bytes - %s",
                source_str,
                packet.get("from"),
                len(payload_bytes),
                payload_preview,
            )
            self._message_queue.put((source_str, payload_bytes))
        except Exception as e:
            LOGGER.debug("Error processing received message: %s", e)

    def send(self, destination: str, payload: bytes) -> None:  # type: ignore[override]
        # Use sendData with PRIVATE_APP port for private messages
        try:
            from meshtastic import portnums_pb2

            portnum = portnums_pb2.PRIVATE_APP
        except ImportError:
            # Fallback: PRIVATE_APP = 80
            portnum = 80

        # Convert destination to proper format
        # Meshtastic sendData accepts both numeric IDs and user IDs - both work equivalently
        # We prefer user ID format for clarity, but numeric IDs are also valid
        if destination:
            # Remove ! prefix if present to check if it's numeric
            dest_clean = destination.lstrip("!")
            if dest_clean.isdigit():
                # Convert numeric ID to user ID format for consistency
                # This uses _getOrCreateByNum or derives from hex format
                converted = self._convert_numeric_to_user_id(dest_clean)
                if converted:
                    destination = converted
                    LOGGER.debug(
                        "Converted numeric destination %s to user ID %s before sending",
                        dest_clean,
                        converted,
                    )
                else:
                    # Fallback: use numeric ID as-is (Meshtastic accepts this)
                    destination = dest_clean
            elif not destination.startswith("!"):
                # User ID without ! prefix - add it
                destination = "!" + destination
            # If it already starts with ! and is hex, use as-is

        payload_bytes = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
        send_start = time.time()
        LOGGER.info(
            "[RADIO] Sending %d bytes to %s via PRIVATE_APP port", len(payload_bytes), destination
        )

        # Use wantAck=True for reliable delivery with automatic retries
        # Meshtastic will automatically retry failed messages
        self._interface.sendData(
            payload_bytes,
            destinationId=destination,
            wantAck=True,  # Reliable delivery with retries
            portNum=portnum,
        )
        send_time = time.time() - send_start
        LOGGER.info(
            "[RADIO] Sent %d bytes to %s in %.3fs", len(payload_bytes), destination, send_time
        )

    def _convert_numeric_to_user_id(self, numeric_id: str) -> str | None:
        """Convert a numeric node ID to user ID format.

        Uses _getOrCreateByNum to ensure node entry exists, then derives user ID.
        Falls back to presumptive hex format (!{8-digit-hex}) if lookup fails.
        This matches Meshtastic's default behavior where user ID = hex of numeric ID.
        """
        # Check cache first
        if numeric_id in self._numeric_to_user_id:
            return self._numeric_to_user_id[numeric_id]

        try:
            numeric_id_int = int(numeric_id)

            # Use _getOrCreateByNum to ensure node entry exists (creates placeholder with derived user ID)
            if hasattr(self._interface, "_getOrCreateByNum"):
                node_info = self._interface._getOrCreateByNum(numeric_id_int)
                if node_info:
                    # Extract user ID from the node info
                    if isinstance(node_info, dict):
                        user_info = node_info.get("user")
                        if user_info and isinstance(user_info, dict):
                            user_id = user_info.get("id")
                            if user_id:
                                self._numeric_to_user_id[numeric_id] = user_id
                                return user_id
                    elif hasattr(node_info, "user"):
                        user_obj = node_info.user
                        if hasattr(user_obj, "id"):
                            user_id = user_obj.id
                            if user_id:
                                self._numeric_to_user_id[numeric_id] = user_id
                                return user_id

            # Fallback: derive user ID directly from numeric ID (hex format)
            # This is the default Meshtastic format: !{8-digit-hex}
            # In virtually all cases, user ID = hex representation of numeric ID
            user_id = f"!{numeric_id_int:08x}"
            self._numeric_to_user_id[numeric_id] = user_id
            LOGGER.debug(
                "Derived user ID %s from numeric ID %s (presumptive format)", user_id, numeric_id
            )
            return user_id

        except Exception as e:
            LOGGER.debug("Could not convert numeric ID %s to user ID: %s", numeric_id, e)
            return None

    def receive(self, timeout: float) -> tuple[str, bytes] | None:  # type: ignore[override]
        # If pubsub is available, use the queue
        if self._subscribed:
            try:
                if LOGGER.isEnabledFor(logging.DEBUG):
                    LOGGER.debug(
                        "[RADIO] Queue size before receive: %d",
                        self._message_queue.qsize(),
                    )
                sender, payload = self._message_queue.get(timeout=timeout)
                LOGGER.debug(
                    "Retrieved message from queue: sender=%s (isdigit=%s)",
                    sender,
                    sender.isdigit() if sender else False,
                )
                # Try to convert numeric ID to user ID if needed
                if sender and sender.isdigit():
                    LOGGER.debug("Attempting to convert numeric ID %s to user ID", sender)
                    converted = self._convert_numeric_to_user_id(sender)
                    if converted:
                        LOGGER.debug(
                            "Converted numeric ID %s to user ID %s on receive", sender, converted
                        )
                        return (converted, payload)
                    else:
                        LOGGER.debug("Could not convert numeric ID %s to user ID", sender)
                return (sender, payload)
            except queue.Empty:
                return None

        # Fallback: poll the interface (may not work for all versions)
        # This is a workaround - ideally we'd use pubsub
        time.sleep(timeout)
        return None

    def close(self) -> None:
        """Close the underlying serial interface."""
        try:
            # Unsubscribe from pubsub
            if self._subscribed:
                try:
                    from pubsub import pub

                    pub.unsubscribe(self._on_receive, "meshtastic.receive")
                except Exception:  # noqa: S110
                    pass

            # Close the serial interface
            if hasattr(self._interface, "close"):
                self._interface.close()
                LOGGER.info("[RADIO] Closed serial interface")
        except Exception as e:
            LOGGER.warning("[RADIO] Error closing interface: %s", e)


def build_radio(
    simulate: bool,
    port: str | None,
    node_id: str | None,
    *,
    disable_dedupe: bool = False,
) -> RadioInterface:
    if simulate:
        return InMemoryRadio(node_id or "node-0")
    try:
        from meshtastic import serial_interface
    except ImportError as exc:
        raise RuntimeError(
            "Meshtastic serial interface is not installed; install meshtastic-python"
        ) from exc
    if port is None:
        interface = serial_interface.SerialInterface()
    else:
        interface = serial_interface.SerialInterface(port)
    return SerialRadioAdapter(interface, disable_dedupe=disable_dedupe)
