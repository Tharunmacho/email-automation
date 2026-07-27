"""Single place to configure logging for every entrypoint (CLI, API, worker)."""
from __future__ import annotations

import logging
import sys

from app.config import settings

_CONFIGURED = False


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    root.handlers[:] = [handler]
    # Quiet noisy third-party libraries.
    for noisy in ("googleapiclient", "google_auth_httplib2", "urllib3", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
