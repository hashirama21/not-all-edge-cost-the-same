"""Utilitaires temporels et traçabilité (UTC, ISO8601)."""

from __future__ import annotations

import os
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso(timespec: str = "seconds") -> str:
    # timespec: 'auto', 'hours', 'minutes', 'seconds', 'milliseconds', 'microseconds'
    return utc_now().isoformat(timespec=timespec)


def git_sha(root: Path | None = None) -> str | None:
    try:
        root = root or Path(__file__).resolve().parents[2]
        res = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            return res.stdout.strip()[:40] or None
    except Exception:
        pass
    return None


def hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


def run_meta(root: Path | None = None) -> dict:
    root = root or Path(__file__).resolve().parents[2]
    return {
        "started_at_utc": utc_now_iso("seconds"),
        "git_sha": git_sha(root),
        "hostname": hostname(),
        "pid": os.getpid(),
        "python": os.sys.version.split()[0],
    }
