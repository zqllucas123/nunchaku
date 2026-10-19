"""Shared logging setup.

Logs go to stderr on purpose: the bridge speaks MCP over stdout.
"""
from __future__ import annotations

import io
import logging
import sys

LOGGER_NAME = "nunchaku"
_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
_DATEFMT = "%H:%M:%S"


class _Utf8Stream(logging.StreamHandler):
    """Write UTF-8 regardless of the console/file code page (Windows defaults to GBK)."""

    def __init__(self) -> None:
        super().__init__(_utf8(sys.stderr))

    def setStream(self, stream):  # logging re-binds sys.stderr at reconfigure
        return super().setStream(_utf8(stream))


def _utf8(stream):
    encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
    if encoding == "utf8":
        return stream
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        return stream
    return io.TextIOWrapper(buffer, encoding="utf-8", errors="replace", line_buffering=True)


def logger(suffix: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}")


def configure(level: str = "INFO") -> None:
    """Attach one stderr handler to the nunchaku logger tree; safe to call twice."""
    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(level.upper())
    root.propagate = False
    if any(getattr(handler, "_nunchaku", False) for handler in root.handlers):
        return
    handler = _Utf8Stream()
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    handler._nunchaku = True  # type: ignore[attr-defined]
    root.addHandler(handler)


def preview(text: str, limit: int = 60) -> str:
    """One-line task preview for logs."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
