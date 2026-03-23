from __future__ import annotations

import logging
import os

__all__ = ["configure_logging"]


def configure_logging(level: str, log_file: str | None = None) -> None:
    """Configure the root logger with the specified level and optional file output.

    Args:
        level: Logging level name (e.g., 'DEBUG', 'INFO', 'WARNING').
        log_file: Optional path to log file. When provided, output is written to
            this file at the requested level regardless of the level chosen.
    """
    level_name = level.upper()
    filename = os.path.expanduser(log_file) if log_file else None
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        filename=filename,
        filemode="a",
    )
