"""Read logs of systemd-managed nunchaku processes from the journal.

The dashboard writes its own log files only for processes it starts. When
systemd runs the services and gateway, their output goes to journald, so the
dashboard reads it back here with journalctl.
"""
from __future__ import annotations

import json
import shutil
import subprocess

# Enough recent output to be useful without shipping megabytes to the browser.
MAX_LINES = 400
MAX_SCAN = 4000
TIMEOUT_SECONDS = 5


class JournalUnavailable(Exception):
    """journalctl is missing, failed, or this account can't read the journal."""


def read_unit(unit: str, tag: str | None = None, lines: int = MAX_LINES) -> str:
    """Recent journal lines for a unit, oldest first.

    With ``tag``, keep only lines carrying ``[tag]``: under ``serve-all`` every
    service shares one unit, and each service's step logs are prefixed with its name.
    """
    executable = shutil.which("journalctl")
    if executable is None:
        raise JournalUnavailable("journalctl not found")
    scan = MAX_SCAN if tag else lines
    try:
        completed = subprocess.run(
            [executable, "--unit", unit, "--lines", str(scan), "--output", "json", "--no-pager", "--quiet"],
            capture_output=True, timeout=TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise JournalUnavailable(f"journalctl failed: {exc}") from exc
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    if completed.returncode != 0 or "insufficient permissions" in stderr:
        raise JournalUnavailable(stderr or f"journalctl exited {completed.returncode}")

    marker = f"[{tag}]" if tag else None
    result: list[str] = []
    for raw in completed.stdout.splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        message = entry.get("MESSAGE")
        if isinstance(message, list):  # journald stores non-UTF-8 messages as byte arrays
            message = bytes(message).decode("utf-8", errors="replace")
        if not isinstance(message, str) or (marker and marker not in message):
            continue
        result.append(message)
    return "\n".join(result[-lines:])
