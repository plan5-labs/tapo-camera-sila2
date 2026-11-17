"""On-disk store of recordings, addressed only by a random UUID (never by path)."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sys
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def default_directory() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "tapo-camera-controller" / "recordings"


class RecordingNotFoundError(KeyError):
    pass


@dataclass
class RecordingMeta:
    recording_id: str
    started_at: str  # ISO 8601, UTC
    duration: float
    stream: str
    include_audio: bool
    width: int
    height: int
    file_size: int

    @property
    def started_at_dt(self) -> dt.datetime:
        return dt.datetime.fromisoformat(self.started_at)


class RecordingStore:
    """Recordings are ``<dir>/<uuid>.mp4`` with a ``<uuid>.json`` metadata sidecar.

    The UUID is validated against a strict pattern before any file access, so a
    client-supplied ID can never address a file outside the store directory.
    """

    def __init__(self, directory: Path, *, max_age: dt.timedelta | None = None, max_total_bytes: int | None = None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_age = max_age
        self.max_total_bytes = max_total_bytes
        self._lock = threading.Lock()
        self._cleanup_partials()
        self.enforce_retention()

    # ----------------------------------------------------------------- paths
    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())

    def _paths(self, recording_id: str) -> tuple[Path, Path]:
        if not _UUID4.match(recording_id):
            raise RecordingNotFoundError(recording_id)
        return self.directory / f"{recording_id}.mp4", self.directory / f"{recording_id}.json"

    def partial_path(self, recording_id: str) -> Path:
        video, _ = self._paths(recording_id)
        return video.with_suffix(".mp4.part")

    def _cleanup_partials(self) -> None:
        for p in self.directory.glob("*.mp4.part"):
            p.unlink(missing_ok=True)

    # ----------------------------------------------------------------- write
    def commit(self, meta: RecordingMeta) -> RecordingMeta:
        """Move the finished partial file into place and write its metadata."""
        video, side = self._paths(meta.recording_id)
        with self._lock:
            os.replace(self.partial_path(meta.recording_id), video)
            meta.file_size = video.stat().st_size
            side.write_text(json.dumps(asdict(meta)), encoding="utf-8")
        self.enforce_retention()
        return meta

    def discard_partial(self, recording_id: str) -> None:
        self.partial_path(recording_id).unlink(missing_ok=True)

    # ------------------------------------------------------------------ read
    def list(self) -> list[RecordingMeta]:
        out = []
        with self._lock:
            for side in self.directory.glob("*.json"):
                try:
                    meta = RecordingMeta(**json.loads(side.read_text(encoding="utf-8")))
                except (ValueError, TypeError) as e:
                    logger.warning("ignoring broken metadata %s: %s", side.name, e)
                    continue
                if (self.directory / f"{meta.recording_id}.mp4").exists():
                    out.append(meta)
        return sorted(out, key=lambda m: m.started_at)

    def read(self, recording_id: str) -> bytes:
        video, _ = self._paths(recording_id)
        try:
            return video.read_bytes()
        except FileNotFoundError:
            raise RecordingNotFoundError(recording_id) from None

    def delete(self, recording_id: str) -> None:
        video, side = self._paths(recording_id)
        with self._lock:
            if not video.exists():
                raise RecordingNotFoundError(recording_id)
            video.unlink(missing_ok=True)
            side.unlink(missing_ok=True)

    # ------------------------------------------------------------- retention
    def enforce_retention(self) -> None:
        recs = self.list()
        now = dt.datetime.now(dt.timezone.utc)
        if self.max_age is not None:
            for m in [m for m in recs if now - m.started_at_dt > self.max_age]:
                logger.info("retention: deleting expired recording %s", m.recording_id)
                self.delete(m.recording_id)
                recs.remove(m)
        if self.max_total_bytes is not None:
            total = sum(m.file_size for m in recs)
            while recs and total > self.max_total_bytes:
                m = recs.pop(0)  # oldest first
                logger.info("retention: deleting recording %s (storage limit)", m.recording_id)
                self.delete(m.recording_id)
                total -= m.file_size
