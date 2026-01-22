from __future__ import annotations

import logging
import os


def configure_logging(level: str, log_file: str | None = None) -> None:
    level_name = level.upper()
    filename = os.path.expanduser(log_file) if log_file and level_name == "DEBUG" else None
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        filename=filename,
        filemode="a",
    )
