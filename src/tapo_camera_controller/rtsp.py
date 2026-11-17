"""RTSP helpers (snapshot / fixed-duration recording) built on PyAV."""

from __future__ import annotations

import datetime as dt
import io
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlsplit, urlunsplit

import av

# Tapo cameras: stream1 = main (full resolution), stream2 = sub (low resolution)
STREAMS = ("stream1", "stream2")


def rtsp_url(host: str, user: str, password: str, stream: str = "stream1", port: int = 554) -> str:
    return f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{stream}"


def with_credentials(uri: str, user: str, password: str) -> str:
    """Insert credentials into an RTSP URI such as the one returned by ONVIF GetStreamUri."""
    parts = urlsplit(uri)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _open(url: str, timeout: float) -> av.container.InputContainer:
    return av.open(url, options={"rtsp_transport": "tcp"}, timeout=timeout)


def snapshot_jpeg(url: str, *, timeout: float = 10.0, quality: int = 2) -> tuple[bytes, int, int]:
    """Grab one decoded video frame and return (jpeg_bytes, width, height).

    ``quality`` is the MJPEG qscale (2 = best, 31 = worst).
    """
    with _open(url, timeout) as inp:
        stream = inp.streams.video[0]
        for frame in inp.decode(stream):  # first successfully decoded frame
            buf = io.BytesIO()
            with av.open(buf, "w", format="image2pipe") as out:
                enc = out.add_stream("mjpeg", rate=1)
                enc.width, enc.height = frame.width, frame.height
                enc.pix_fmt = "yuvj420p"
                enc.codec_context.qmin = enc.codec_context.qmax = quality
                for pkt in enc.encode(frame.reformat(format="yuvj420p")):
                    out.mux(pkt)
                for pkt in enc.encode(None):
                    out.mux(pkt)
            return buf.getvalue(), frame.width, frame.height
    raise RuntimeError("no video frame received")


@dataclass
class RecordingResult:
    path: Path
    started_at: dt.datetime  # wall-clock time of the first recorded key frame (UTC)
    duration: float  # seconds of stored video
    width: int
    height: int
    has_audio: bool


def record(
    url: str,
    path: Path,
    duration_s: float,
    *,
    timeout: float = 10.0,
    with_audio: bool = False,
    progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> RecordingResult:
    """Record the RTSP stream into an MP4 file at ``path`` for ``duration_s`` seconds.

    Video (H.264) is remuxed without re-encoding. Audio (Tapo: G.711 A-law, 8 kHz), if
    requested and present, is transcoded to AAC because MP4 cannot hold G.711.
    ``progress`` is called with a fraction in [0, 1] about once per second.
    """
    with _open(url, timeout) as inp, av.open(str(path), "w", format="mp4") as out:
        v_in = inp.streams.video[0]
        a_in = inp.streams.audio[0] if (with_audio and inp.streams.audio) else None
        v_out = out.add_stream_from_template(v_in)
        a_out = None
        if a_in is not None:
            a_out = out.add_stream("aac", rate=a_in.codec_context.sample_rate)
            a_out.layout = "mono"
            a_out.bit_rate = 32_000

        start_s: float | None = None  # stream time (seconds) mapped to 0 in the output
        started_at = dt.datetime.now(dt.timezone.utc)
        t0 = 0.0
        last_dts: int | None = None
        audio_next_pts: int | None = None
        last_video_s = 0.0
        last_report = 0.0
        demux_streams = [v_in] + ([a_in] if a_in is not None else [])

        for pkt in inp.demux(*demux_streams):
            # Skip flush packets and packets without timestamps (Tapo sends the very
            # first key frame with pts=None, which the MP4 muxer rejects).
            if pkt.size == 0 or pkt.pts is None or pkt.dts is None:
                continue
            tb = pkt.time_base
            if start_s is None:  # start on a video key frame so the file is decodable
                if pkt.stream.type != "video" or not pkt.is_keyframe:
                    continue
                start_s = float(pkt.dts * tb)
                started_at = dt.datetime.now(dt.timezone.utc)
                t0 = time.monotonic()

            if pkt.stream.type == "video":
                shift = round(start_s / tb)
                dts, pts = pkt.dts - shift, pkt.pts - shift
                if dts < 0:
                    continue
                # RTSP timestamps can jump backwards (e.g. when RTCP sync kicks in);
                # the muxer requires strictly increasing dts.
                if last_dts is not None and dts <= last_dts:
                    pts += last_dts + 1 - dts
                    dts = last_dts + 1
                last_dts = dts
                pkt.dts, pkt.pts = dts, max(pts, dts)
                pkt.stream = v_out
                out.mux(pkt)
                last_video_s = float(pkt.pts * tb)
            else:  # audio: decode G.711, encode AAC
                for frame in pkt.decode():
                    if audio_next_pts is None:
                        # align the first audio frame with the video start, then count
                        # samples (input audio timestamps may jump like the video ones)
                        t = float(frame.pts * frame.time_base) - start_s
                        if t < 0:
                            continue
                        audio_next_pts = round(t * frame.sample_rate)
                    frame.pts = audio_next_pts
                    frame.time_base = Fraction(1, frame.sample_rate)
                    audio_next_pts += frame.samples
                    for apkt in a_out.encode(frame):
                        out.mux(apkt)

            # stop on stream time (accurate duration); wall clock is only a safety net
            # against a stalled stream with frozen timestamps
            elapsed = time.monotonic() - t0
            if progress is not None and elapsed - last_report >= 1.0:
                last_report = elapsed
                progress(min(last_video_s / duration_s, 1.0))
            if last_video_s >= duration_s or elapsed >= duration_s + timeout:
                break
            if cancelled is not None and cancelled():
                break

        if start_s is None:
            raise RuntimeError("no video key frame received")
        if a_out is not None:
            for apkt in a_out.encode(None):
                out.mux(apkt)
        width, height = v_in.codec_context.width, v_in.codec_context.height

    if progress is not None:
        progress(1.0)
    return RecordingResult(path, started_at, round(last_video_s, 3), width, height, a_out is not None)
