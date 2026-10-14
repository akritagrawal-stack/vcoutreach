"""Append-only send journal used for dedupe."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, Union

from .types import OutreachError

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

_MODES = ("test", "live")
_LOCK_TIMEOUT_SECONDS = 15.0
_STALE_LOCK_SECONDS = 60.0


def _check_mode(mode: str) -> None:
    if mode not in _MODES:
        raise ValueError("journal mode must be 'test' or 'live', got %r" % (mode,))


def _entry_date(ts: Any) -> Union[date, None]:
    """UTC calendar date of an ISO timestamp, or None when it cannot be read."""
    if not isinstance(ts, str) or len(ts) < 10:
        return None
    text = ts.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.date()


@contextlib.contextmanager
def _lockfile(lock_path: str) -> Iterator[None]:
    """Fallback exclusive lock for platforms without fcntl (create-exclusive lock file)."""
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    fd = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - os.stat(lock_path).st_mtime > _STALE_LOCK_SECONDS:
                    os.unlink(lock_path)
                    continue
            except OSError:
                continue
            if time.monotonic() > deadline:
                raise OutreachError("could not lock the send journal (%s)" % lock_path)
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(lock_path)


class Journal:
    """Append-only JSONL record of sends, keyed by recipient and lane per mode."""

    path: Path

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @staticmethod
    def key(recipient: str, lane: str) -> str:
        lane_text = lane.value if isinstance(lane, Enum) else str(lane)
        raw = recipient.lower() + "|" + lane_text
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # -- reading ---------------------------------------------------------

    def _entries(self) -> Iterator[Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except FileNotFoundError:
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # a torn line from a crashed writer is ignored
            if isinstance(entry, dict):
                yield entry

    def has(self, key: str, mode: str) -> bool:
        _check_mode(mode)
        return any(e.get("key") == key and e.get("mode") == mode for e in self._entries())

    def count_today(self, mode: str, today: Union[date, None] = None) -> int:
        _check_mode(mode)
        if today is None:
            day = datetime.now(timezone.utc).date()
        elif isinstance(today, datetime):
            day = today.date()
        else:
            day = today
        return sum(
            1 for e in self._entries() if e.get("mode") == mode and _entry_date(e.get("ts")) == day
        )

    # -- writing ---------------------------------------------------------

    def record(self, key: str, mode: str, **fields: Union[str, int]) -> None:
        """Append one entry atomically under an exclusive lock."""
        _check_mode(mode)
        if "key" in fields or "mode" in fields:
            raise ValueError("'key' and 'mode' cannot be passed as extra journal fields")
        entry: Dict[str, Any] = {
            "key": key,
            "mode": mode,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        entry.update(fields)
        line = (json.dumps(entry, separators=(",", ":")) + "\n").encode("utf-8")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        fd = os.open(str(self.path), flags, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    self._append(fd, line)
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            else:  # pragma: no cover - Windows
                with _lockfile(str(self.path) + ".lock"):
                    self._append(fd, line)
        finally:
            os.close(fd)

    @staticmethod
    def _append(fd: int, line: bytes) -> None:
        # If a previous writer died mid-line, start on a fresh line so this entry parses.
        size = os.fstat(fd).st_size
        if size > 0:
            os.lseek(fd, -1, os.SEEK_END)
            if os.read(fd, 1) != b"\n":
                line = b"\n" + line
        written = os.write(fd, line)
        while written < len(line):  # a single write is expected; finish a short one
            written += os.write(fd, line[written:])
        os.fsync(fd)
