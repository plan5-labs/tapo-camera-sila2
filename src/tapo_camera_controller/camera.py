"""Device layer: one Tapo camera reached via ONVIF (control) and RTSP (media).

This module knows nothing about SiLA. It raises the exceptions defined below, which
the feature implementations translate into SiLA defined execution errors.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import enum
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator

from . import rtsp
from .credentials import Credentials
from .onvif import NS, OnvifCamera, OnvifError, Preset, PtzVector

logger = logging.getLogger(__name__)


class Status(enum.IntEnum):
    NotConnected = 0
    Idle = 1
    Running = 2
    Error = 3


# ------------------------------------------------------------------ exceptions
class CameraError(Exception):
    """Base class."""


class CommunicationError(CameraError):
    pass


class InvalidStatusError(CameraError):
    pass


class MovementInProgressError(CameraError):
    pass


class PresetNotFoundError(CameraError):
    pass


class PresetLimitError(CameraError):
    pass


class PresetNameError(CameraError):
    pass


class BusyError(CameraError):
    """A snapshot/recording is running, so an exclusive operation cannot start."""


class UnsupportedResolutionError(CameraError):
    pass


@dataclass
class DeviceInfo:
    manufacturer: str = ""
    model: str = ""
    firmware_version: str = ""
    serial_number: str = ""
    hardware_id: str = ""


STREAM_NAMES = ("Main", "Sub")
PRESET_NAME_MAX_BYTES = 32


class TapoCamera:
    def __init__(
        self,
        creds: Credentials,
        *,
        on_status: Callable[[Status], None] | None = None,
        onvif_port: int = 2020,
        rtsp_port: int = 554,
        timeout: float = 10.0,
    ):
        self._creds = creds
        self._on_status = on_status
        self._timeout = timeout
        self._rtsp_port = rtsp_port
        self.onvif = OnvifCamera(creds.host, creds.user, creds.password, port=onvif_port, timeout=timeout)

        self._lock = threading.Condition()  # protects the fields below
        self._status = Status.NotConnected
        self._running = 0
        self._exclusive = False  # an exclusive operation (stream restart) is in progress
        self._move_lock = threading.Lock()

        self.device_info = DeviceInfo()
        self.has_ptz = False
        self._ptz_profile: str | None = None
        self._main_profile: str | None = None
        self._main_encoder: str | None = None
        self._video_source: str | None = None
        self._stream_paths: dict[str, str] = {"Main": "stream1", "Sub": "stream2"}

    # ------------------------------------------------------------------ status
    @property
    def status(self) -> Status:
        return self._status

    def _set_status(self, status: Status) -> None:
        # caller holds self._lock
        if status != self._status:
            logger.info("status: %s -> %s", self._status.name, status.name)
            self._status = status
            if self._on_status is not None:
                self._on_status(status)

    def redact(self, text: str) -> str:
        return self._creds.redact(text)

    @contextlib.contextmanager
    def operation(self, name: str, *, exclusive: bool = False) -> Iterator[None]:
        """Mark a camera operation as running; map failures to Error status.

        An ``exclusive`` operation (e.g. a stream restart) requires that nothing else is
        running; other operations started meanwhile wait until it has finished.
        """
        with self._lock:
            if not self._lock.wait_for(lambda: not self._exclusive, timeout=30):
                raise BusyError(f"cannot run {name}: a camera setting change is still in progress")
            if self._status in (Status.NotConnected, Status.Error):
                raise InvalidStatusError(f"cannot run {name}: status is {self._status.name}; use Reset")
            if exclusive and self._running:
                raise BusyError(f"cannot run {name}: {self._running} snapshot/recording/movement operation(s) running")
            self._running += 1
            self._exclusive = exclusive
            self._set_status(Status.Running)
        failed = False
        try:
            yield
        except CommunicationError:
            failed = True
            raise
        except CameraError:
            raise  # usage errors (e.g. preset not found) do not change the status
        except Exception as e:  # noqa: BLE001 - unexpected failure talking to the camera
            failed = True
            raise CommunicationError(self.redact(f"{name} failed: {type(e).__name__}: {e}")) from None
        finally:
            with self._lock:
                self._running -= 1
                if exclusive:
                    self._exclusive = False
                    self._lock.notify_all()
                if failed:
                    self._set_status(Status.Error)
                elif self._status == Status.Running and self._running == 0:
                    self._set_status(Status.Idle)

    # -------------------------------------------------------------- connection
    def connect(self) -> None:
        """(Re)connect: query capabilities, profiles and device information."""
        try:
            self.onvif.sync_clock()
            caps = self.onvif.get_capabilities()
            info = self.onvif.get_device_information()
            profiles = self.onvif.get_profiles()
            paths = {}
            for p in profiles:
                try:
                    uri = self.onvif.get_stream_uri(p.token)
                except OnvifError:
                    continue
                path = uri.split("/", 3)[-1] if uri.count("/") >= 3 else ""
                if path:
                    paths[p.token] = path
        except OnvifError as e:
            with self._lock:
                self._set_status(Status.NotConnected)
            raise CommunicationError(self.redact(f"cannot connect to camera {self._creds.host}: {e}")) from None

        self.device_info = DeviceInfo(
            manufacturer=info.get("Manufacturer", ""),
            model=info.get("Model", ""),
            firmware_version=info.get("FirmwareVersion", ""),
            serial_number=info.get("SerialNumber", ""),
            hardware_id=info.get("HardwareId", ""),
        )
        # Tapo: profile_1 = mainStream (stream1), profile_2 = minorStream (stream2)
        if len(profiles) >= 1 and profiles[0].token in paths:
            self._stream_paths["Main"] = paths[profiles[0].token]
        if len(profiles) >= 2 and profiles[1].token in paths:
            self._stream_paths["Sub"] = paths[profiles[1].token]
        if profiles:
            self._main_profile = profiles[0].token
            self._main_encoder = profiles[0].encoder_token or None
            self._video_source = profiles[0].video_source_token or None
        ptz_profiles = [p.token for p in profiles if p.has_ptz]
        self.has_ptz = "PTZ" in caps and bool(ptz_profiles)
        self._ptz_profile = ptz_profiles[0] if ptz_profiles else None
        logger.info(
            "connected: %s %s (fw %s), PTZ=%s, streams=%s",
            self.device_info.manufacturer,
            self.device_info.model,
            self.device_info.firmware_version,
            self.has_ptz,
            self._stream_paths,
        )
        with self._lock:
            self._set_status(Status.Running if self._running else Status.Idle)

    # ------------------------------------------------------------------- media
    def _stream_url(self, stream: str) -> str:
        if stream not in STREAM_NAMES:
            raise ValueError(f"unknown stream {stream!r}")
        return rtsp.rtsp_url(
            self._creds.host, self._creds.user, self._creds.password, self._stream_paths[stream], self._rtsp_port
        )

    def snapshot(self, stream: str) -> tuple[bytes, int, int, dt.datetime, PtzVector | None]:
        """(jpeg, width, height, time the frame was received, pan/tilt position at that time or None).

        The position is read right after the frame has been decoded; None without PTZ or if the
        camera does not report it (the snapshot itself still succeeds).
        """
        positions: list[PtzVector | None] = []

        def at_frame() -> None:
            pos = None
            if self.has_ptz:
                try:
                    pos = self.position()
                except CameraError as e:
                    logger.warning("snapshot: could not read the pan/tilt position: %s", e)
            positions.append(pos)

        with self.operation("TakeSnapshot"):
            data, w, h, received_at = rtsp.snapshot_jpeg(
                self._stream_url(stream), timeout=self._timeout, on_frame=at_frame
            )
            return data, w, h, received_at, positions[0] if positions else None

    def record(self, stream: str, path, duration_s: float, with_audio: bool, progress=None) -> rtsp.RecordingResult:
        with self.operation("RecordVideo"):
            return rtsp.record(
                self._stream_url(stream),
                path,
                duration_s,
                timeout=self._timeout,
                with_audio=with_audio,
                progress=progress,
            )

    # --------------------------------------------------------------------- PTZ
    def _profile(self) -> str:
        if not self.has_ptz or self._ptz_profile is None:
            raise CommunicationError("this camera does not support pan/tilt")
        return self._ptz_profile

    def _onvif(self, fn, *args):
        try:
            return fn(*args)
        except OnvifError as e:
            raise CommunicationError(self.redact(str(e))) from None

    def position(self) -> PtzVector:
        pos = self._onvif(self.onvif.get_status, self._profile())
        if pos is None:
            raise CommunicationError("camera did not report a position")
        return pos

    @contextlib.contextmanager
    def _movement(self, name: str) -> Iterator[None]:
        if not self._move_lock.acquire(blocking=False):
            raise MovementInProgressError("another movement is in progress")
        try:
            with self.operation(name):
                yield
        finally:
            self._move_lock.release()

    def _wait_until_stopped(
        self,
        start: PtzVector,
        target: PtzVector | None,
        progress: Callable[[float], None] | None,
        timeout: float = 40.0,
        poll: float = 0.4,
    ) -> PtzVector:
        """Poll the position until it has been unchanged for 3 consecutive polls.

        Right after a command the camera may not have started yet, so a stable
        position only counts once it differs from the start, is at the target,
        or 2 s have passed.
        """
        t0 = time.monotonic()
        total = math.dist((start.pan, start.tilt), (target.pan, target.tilt)) if target else 0.0
        prev, same = None, 0
        while True:
            time.sleep(poll)
            cur = self.position()
            key = (round(cur.pan, 3), round(cur.tilt, 3))
            same = same + 1 if key == prev else 0
            prev = key
            elapsed = time.monotonic() - t0
            if progress is not None and target is not None and total > 1e-6:
                left = math.dist((cur.pan, cur.tilt), (target.pan, target.tilt))
                progress(max(0.0, min(1.0, 1.0 - left / total)))
            moved = abs(cur.pan - start.pan) > 1e-3 or abs(cur.tilt - start.tilt) > 1e-3
            at_target = target is not None and abs(cur.pan - target.pan) < 2e-3 and abs(cur.tilt - target.tilt) < 2e-3
            if same >= 2 and (moved or at_target or elapsed >= 2.0):
                return cur
            if elapsed >= timeout:
                logger.warning("movement did not settle within %.0f s; returning current position", timeout)
                return cur

    # The movement methods return (position before, position after).

    def move_absolute(self, pan: float, tilt: float, progress=None) -> tuple[PtzVector, PtzVector]:
        with self._movement("MoveAbsolute"):
            start = self.position()
            target = PtzVector(pan, tilt)
            self._onvif(self.onvif.absolute_move, self._profile(), target)
            return start, self._wait_until_stopped(start, target, progress)

    def move_relative(self, d_pan: float, d_tilt: float, progress=None) -> tuple[PtzVector, PtzVector]:
        with self._movement("MoveRelative"):
            start = self.position()
            target = PtzVector(max(-1.0, min(1.0, start.pan + d_pan)), max(-1.0, min(1.0, start.tilt + d_tilt)))
            self._onvif(self.onvif.relative_move, self._profile(), PtzVector(d_pan, d_tilt))
            return start, self._wait_until_stopped(start, target, progress)

    def presets(self) -> list[Preset]:
        with self.operation("GetPresets"):
            return self._onvif(self.onvif.get_presets, self._profile())

    def _find_preset(self, token: str) -> Preset:
        for p in self._onvif(self.onvif.get_presets, self._profile()):
            if p.token == token:
                return p
        raise PresetNotFoundError(f"no preset with token {token!r}")

    def goto_preset(self, token: str, progress=None) -> tuple[PtzVector, PtzVector]:
        with self._movement("GotoPreset"):
            self._find_preset(token)
            start = self.position()
            self._onvif(self.onvif.goto_preset, self._profile(), token)
            # the preset position is not known in advance -> no target / progress
            return start, self._wait_until_stopped(start, None, None)

    def save_preset(self, name: str) -> Preset:
        if len(name.encode("utf-8")) > PRESET_NAME_MAX_BYTES:
            raise PresetNameError(f"preset name is {len(name.encode('utf-8'))} bytes in UTF-8; maximum is 32")
        if not name or any(c in name for c in "&<>\"'"):
            raise PresetNameError("preset name must not be empty or contain & < > \" '")
        with self.operation("SavePreset"):
            existing = next((p for p in self._onvif(self.onvif.get_presets, self._profile()) if p.name == name), None)
            try:
                token = self.onvif.set_preset(self._profile(), name, existing.token if existing else None)
            except OnvifError as e:
                if e.has_subcode("TooManyPresets"):
                    raise PresetLimitError("the camera cannot store more presets; remove one first") from None
                raise CommunicationError(self.redact(str(e))) from None
            return Preset(token=token or (existing.token if existing else ""), name=name)

    def remove_preset(self, token: str) -> None:
        with self.operation("RemovePreset"):
            self._find_preset(token)
            self._onvif(self.onvif.remove_preset, self._profile(), token)

    # ---------------------------------------------------------------- settings
    IMAGE_KEYS = ("ColorSaturation", "Contrast", "Sharpness")

    def _require(self, value: str | None, what: str) -> str:
        if not value:
            raise CommunicationError(f"camera did not report a {what}; use Reset")
        return value

    def image_settings(self) -> dict[str, int]:
        src = self._require(self._video_source, "video source")
        values = self._onvif(self.onvif.get_imaging_settings, src)
        missing = [k for k in self.IMAGE_KEYS if k not in values]
        if missing:
            raise CommunicationError(f"camera does not report {', '.join(missing)}")
        return {k: int(round(values[k])) for k in self.IMAGE_KEYS}

    def set_image_settings(self, saturation: int, contrast: int, sharpness: int) -> dict[str, int]:
        src = self._require(self._video_source, "video source")
        with self.operation("SetImageSettings"):
            self._onvif(
                self.onvif.set_imaging_settings,
                src,
                {"ColorSaturation": saturation, "Contrast": contrast, "Sharpness": sharpness},
            )
            return self.image_settings()

    def main_resolution(self) -> tuple[int, int]:
        cfg = self._onvif(self.onvif.get_video_encoder_configuration, self._require(self._main_encoder, "main encoder"))
        return (
            int(cfg.findtext("tt:Resolution/tt:Width", namespaces=NS)),
            int(cfg.findtext("tt:Resolution/tt:Height", namespaces=NS)),
        )

    def main_resolutions(self) -> list[tuple[int, int]]:
        return self._onvif(self.onvif.get_encoder_resolutions, self._require(self._main_profile, "main profile"))

    def set_main_resolution(self, width: int, height: int, restart_wait: float = 4.0) -> tuple[int, int]:
        token = self._require(self._main_encoder, "main encoder")
        available = self.main_resolutions()
        if (width, height) not in available:
            raise UnsupportedResolutionError(
                f"{width}x{height} is not supported; available: " + ", ".join(f"{w}x{h}" for w, h in available)
            )
        with self.operation("SetMainStreamResolution", exclusive=True):
            cfg = self._onvif(self.onvif.get_video_encoder_configuration, token)
            if (int(cfg.findtext("tt:Resolution/tt:Width", namespaces=NS)),
                    int(cfg.findtext("tt:Resolution/tt:Height", namespaces=NS))) == (width, height):
                return (width, height)  # unchanged: no stream restart
            cfg.find("tt:Resolution/tt:Width", NS).text = str(width)
            cfg.find("tt:Resolution/tt:Height", NS).text = str(height)
            self._onvif(self.onvif.set_video_encoder_configuration, cfg)
            time.sleep(restart_wait)  # the camera restarts its streams
            result = self.main_resolution()
        if result != (width, height):
            raise CommunicationError(f"camera reports {result[0]}x{result[1]} after the change")
        return result
