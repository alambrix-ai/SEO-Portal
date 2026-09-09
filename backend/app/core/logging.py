"""Single logging entry point so agent runs are traceable in one stream."""
from __future__ import annotations

import logging
import sys

from app.core.config import settings

_CONFIGURED = False
_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-38s | %(message)s"


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    # Log lines carry currency symbols, em-dashes and customer names. On a
    # console with a legacy code page that is an encode error raised inside a
    # log call — the least useful place for a program to fail. Unmappable
    # characters are replaced instead.
    stream = sys.stdout
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - detached stdout
            pass
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    # httpx logs every connector request at INFO; that is too chatty here.
    logging.getLogger("httpx").setLevel("WARNING")
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
