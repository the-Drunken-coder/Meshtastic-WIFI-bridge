"""Retransmission timer for reliable transport."""

import threading
import time
from typing import Callable, Optional

from common.logging_setup import get_logger

logger = get_logger(__name__)


class RetransmitTimer:
    """
    Timer for triggering retransmission checks.
    
    Runs a background thread that periodically checks for
    frames that need retransmission.
    """
    
    def __init__(
        self,
        interval_ms: int = 1000,
        callback: Optional[Callable[[], None]] = None,
    ):
        """
        Initialize retransmit timer.
        
        Args:
            interval_ms: Check interval in milliseconds
            callback: Function to call on each interval
        """
        self.interval_ms = interval_ms
        self._callback = callback
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
    
    def start(self) -> None:
        """Start the retransmit timer thread."""
        if self._running:
            return
        
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.debug(f"Retransmit timer started (interval={self.interval_ms}ms)")
    
    def stop(self) -> None:
        """Stop the retransmit timer thread."""
        self._running = False
        self._stop_event.set()
        
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        
        logger.debug("Retransmit timer stopped")
    
    def set_callback(self, callback: Callable[[], None]) -> None:
        """
        Set the callback function.
        
        Args:
            callback: Function to call on each interval
        """
        self._callback = callback
    
    def _run(self) -> None:
        """Timer thread main loop."""
        interval_s = self.interval_ms / 1000.0
        
        while self._running:
            # Wait for interval or stop signal
            if self._stop_event.wait(timeout=interval_s):
                break  # Stop signal received
            
            if self._callback:
                try:
                    self._callback()
                except Exception as e:
                    logger.error(f"Error in retransmit callback: {e}")
