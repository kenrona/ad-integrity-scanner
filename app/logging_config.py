"""Centralized logging setup.

Emits one line per event with stable `key=value` fields (scan_id, url_hash,
domain, ...) so logs are greppable now and easy to ship to JSON later.
"""
from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root = logging.getLogger("ai")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ai.{name}")


def kv(**fields: object) -> str:
    """Render fields as a stable `k=v` suffix for log messages."""
    return " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
