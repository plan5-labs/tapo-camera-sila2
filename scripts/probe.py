"""Probe what a Tapo camera supports via ONVIF and RTSP (camera account only).

Credentials come from the OS credential store (store them once with
`uv run scripts/set_credentials.py`); TAPO_HOST / TAPO_USER / TAPO_PASSWORD
environment variables override them. The camera account is created in the Tapo app:
Camera Settings > Advanced Settings > Camera Account.

Usage:
    uv run scripts/probe.py            # read-only checks + snapshot + 5 s recording
    uv run scripts/probe.py --move     # additionally try PTZ moves (camera WILL move)

Results are written to ./probe_output/ (git-ignored). The password is masked in all
console output and in the report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

from tapo_camera_controller import credentials, rtsp
from tapo_camera_controller.onvif import OnvifCamera, PtzVector

OUT = Path("probe_output")
report: dict[str, dict] = {"onvif": {}, "rtsp": {}}
_redact = lambda s: s  # replaced in main() once credentials are loaded


def out(msg: str) -> None:
    print(_redact(msg))


def step(section: str, name: str, fn, *args, **kwargs):
    """Run one check, record ok/error, print a one-line summary."""
    try:
        value = fn(*args, **kwargs)
        report[section][name] = {"ok": True, "value": value}
        shown = json.dumps(value, ensure_ascii=False, default=str)
        out(f"  [ OK ] {name}: {shown[:160]}")
        return value
    except Exception as e:  # noqa: BLE001 - probing, we want every failure recorded
        report[section][name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        out(f"  [FAIL] {name}: {type(e).__name__}: {e}")
        if os.environ.get("PROBE_DEBUG"):
            out(traceback.format_exc())
        return None


def probe_onvif(host, user, password, move: bool):
    out("== ONVIF (tcp/2020) ==")
    cam = OnvifCamera(host, user, password)
    step("onvif", "clock_offset_s", lambda: cam.sync_clock().total_seconds())
    step("onvif", "device_information", cam.get_device_information)
    caps = step("onvif", "capabilities", cam.get_capabilities)
    profiles = step("onvif", "profiles", lambda: [p.__dict__ for p in cam.get_profiles()]) or []
    if not profiles:
        return
    tok = next((p["token"] for p in profiles if p["has_ptz"]), profiles[0]["token"])
    for p in profiles:
        step("onvif", f"stream_uri[{p['token']}]", cam.get_stream_uri, p["token"])
    step("onvif", "snapshot_uri", cam.get_snapshot_uri, tok)
    if not caps or "PTZ" not in caps:
        out("  (no PTZ service advertised)")
        return
    step("onvif", "ptz_spaces", cam.get_ptz_spaces)
    step("onvif", "presets", lambda: [p.__dict__ for p in cam.get_presets(tok)])
    step("onvif", "status_before", lambda: cam.get_status(tok).__dict__ if cam.get_status(tok) else None)
    if not move:
        return

    out("  -- moving camera (ONVIF) --")

    def cont(v: PtzVector, sec=1.0):
        cam.continuous_move(tok, v)
        time.sleep(sec)
        cam.stop(tok)
        time.sleep(1.0)

    step("onvif", "continuous_move_right", cont, PtzVector(0.5, 0.0))
    step("onvif", "status_after_continuous", lambda: (s := cam.get_status(tok)) and s.__dict__)
    step("onvif", "continuous_move_left", cont, PtzVector(-0.5, 0.0))
    step("onvif", "continuous_move_up", cont, PtzVector(0.0, 0.5))
    step("onvif", "continuous_move_down", cont, PtzVector(0.0, -0.5))

    def rel(v: PtzVector):
        cam.relative_move(tok, v)
        time.sleep(2.0)

    step("onvif", "relative_move_right", rel, PtzVector(0.1, 0.0))
    step("onvif", "status_after_relative", lambda: (s := cam.get_status(tok)) and s.__dict__)
    step("onvif", "relative_move_left", rel, PtzVector(-0.1, 0.0))

    def absmove(v: PtzVector):
        cam.absolute_move(tok, v)
        time.sleep(3.0)

    step("onvif", "absolute_move_center", absmove, PtzVector(0.0, 0.0))
    step("onvif", "status_after_absolute", lambda: (s := cam.get_status(tok)) and s.__dict__)

    def preset_roundtrip():
        token = cam.set_preset(tok, "sila_probe")
        cont(PtzVector(0.5, 0.0))
        cam.goto_preset(tok, token)
        time.sleep(3.0)
        return {"token": token, "presets": [p.__dict__ for p in cam.get_presets(tok)]}

    step("onvif", "preset_set_and_goto", preset_roundtrip)


def probe_rtsp(host, user, password):
    out("== RTSP (tcp/554) ==")
    OUT.mkdir(exist_ok=True)
    for s in rtsp.STREAMS:
        url = rtsp.rtsp_url(host, user, password, s)

        def snap(url=url, s=s):
            t = time.monotonic()
            data, w, h = rtsp.snapshot_jpeg(url)
            (OUT / f"snapshot_{s}.jpg").write_bytes(data)
            return {"width": w, "height": h, "bytes": len(data), "seconds": round(time.monotonic() - t, 2)}

        step("rtsp", f"snapshot[{s}]", snap)

    def rec():
        t = time.monotonic()
        p = rtsp.record(rtsp.rtsp_url(host, user, password, "stream2"), OUT / "record_stream2.mp4", 5, with_audio=True)
        return {"file": str(p.path), "bytes": p.path.stat().st_size, "duration": p.duration, "seconds": round(time.monotonic() - t, 2)}

    step("rtsp", "record_5s[stream2]", rec)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--move", action="store_true", help="try PTZ moves (the camera will physically move)")
    ap.add_argument("--profile", default="default", help="credential profile name")
    ap.add_argument("--skip", nargs="*", default=[], choices=["onvif", "rtsp"])
    args = ap.parse_args()

    global _redact
    try:
        creds = credentials.load(args.profile)
    except LookupError as e:
        sys.exit(str(e))
    _redact = creds.redact
    host, user, password = creds.host, creds.user, creds.password
    out(f"target: {creds}")

    if "onvif" not in args.skip:
        probe_onvif(host, user, password, args.move)
    if "rtsp" not in args.skip:
        probe_rtsp(host, user, password)

    OUT.mkdir(exist_ok=True)
    path = OUT / "report.json"
    text = creds.redact(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    path.write_text(text, encoding="utf-8")
    out(f"\nreport written to {path}")


if __name__ == "__main__":
    main()
