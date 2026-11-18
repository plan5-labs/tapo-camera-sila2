"""End-to-end smoke test against a running server (the camera WILL move).

    uv run python -m tapo_camera_controller --insecure -a 127.0.0.1 -p 50052 &
    uv run scripts/smoke_test.py --port 50052 [--out smoke_output]

Exercises every command/property of the three features, checks errors, cleans up the
presets and recordings it created, and returns the camera to home.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from sila2.client import SilaClient
from sila2.framework import CommandExecutionStatus
from sila2.framework.errors.defined_execution_error import DefinedExecutionError
from sila2.framework.errors.validation_error import ValidationError

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{' OK ' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}", flush=True)


def expect_error(name: str, fn, err_id: str | None) -> None:
    try:
        fn()
    except DefinedExecutionError as e:
        ok = err_id is not None and e.identifier == err_id
        check(name, ok, f"{e.identifier}: {e.message}")
    except ValidationError as e:
        check(name, err_id is None, f"ValidationError: {e.message}")
    except Exception as e:  # noqa: BLE001
        check(name, False, f"{type(e).__name__}: {e}")
    else:
        check(name, False, "no error raised")


def wait(cmd, label: str):
    """Wait for an observable command, printing progress; return its responses."""
    last = -1.0
    while not cmd.done:
        p = cmd.progress
        if p is not None and p - last >= 0.25:
            print(f"      {label}: progress {p:.2f}", flush=True)
            last = p
        time.sleep(0.3)
    if cmd.status != CommandExecutionStatus.finishedSuccessfully:
        return cmd.get_responses()  # raises the error
    return cmd.get_responses()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=50052)
    ap.add_argument("--out", default="smoke_output")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(exist_ok=True)

    client = SilaClient(args.host, args.port, insecure=True)
    cam = client.TapoCameraController
    vid = client.VideoCaptureController
    ptz = client.PanTiltController

    print("== TapoCameraController ==")
    st = cam.Status.get()
    check("Status is Idle", st == 1, str(st))
    info = cam.DeviceInformation.get()
    check("DeviceInformation", bool(info.Model), f"{info.Manufacturer} {info.Model} fw {info.FirmwareVersion}")
    cam.Reset()
    check("Reset", cam.Status.get() == 1)

    print("== VideoCaptureController: snapshots ==")
    for s in ("Main", "Sub"):
        r = vid.TakeSnapshot(s)
        (out / f"snapshot_{s}.jpg").write_bytes(r.Image)
        check(f"TakeSnapshot({s})", r.Image[:2] == b"\xff\xd8", f"{r.Width}x{r.Height}, {len(r.Image)} B, {r.CapturedAt}")
    expect_error("TakeSnapshot(invalid stream) -> ValidationError", lambda: vid.TakeSnapshot("Foo"), None)

    print("== PanTiltController ==")
    # angles in degrees; the camera quantizes its normalized position (about 0.01 = 1.7 deg pan, 0.35 deg tilt)
    def near(pos, pan, tilt):
        return abs(pos.Pan - pan) < 1.5 and abs(pos.Tilt - tilt) < 0.5

    rng = ptz.MovementRange.get()
    check("MovementRange (C232 defaults)", (rng.PanMin, rng.PanMax, rng.TiltMin, rng.TiltMax) == (-170, 170, -34.5, 34.5), str(rng))
    p = ptz.CurrentPosition.get()
    check("CurrentPosition", rng.PanMin <= p.Pan <= rng.PanMax and rng.TiltMin <= p.Tilt <= rng.TiltMax, f"{p.Pan}, {p.Tilt}")
    r = wait(ptz.MoveAbsolute(50.0, 7.0), "MoveAbsolute")
    check("MoveAbsolute(50, 7)", near(r.Position, 50, 7), str(r.Position))
    r = wait(ptz.MoveRelative(-17.0, -3.5), "MoveRelative")
    check("MoveRelative(-17, -3.5)", near(r.Position, 33, 3.5), str(r.Position))
    check("MoveRelative StartPosition", near(r.StartPosition, 50, 7), str(r.StartPosition))
    s = vid.TakeSnapshot("Sub")
    check("TakeSnapshot Position", s.Position.PositionAvailable and near(s.Position, 33, 3.5), str(s.Position))
    check("CapturedAt has the local UTC offset", s.CapturedAt.utcoffset() == s.CapturedAt.astimezone().utcoffset(), s.CapturedAt.isoformat())
    r = wait(ptz.MoveRelative(360.0, 0.0), "MoveRelative to limit")
    check("MoveRelative(+360, 0) stops at PanMax", abs(r.Position.Pan - rng.PanMax) < 1.5, str(r.Position))
    r = wait(ptz.MoveAbsolute(-175.0, 0.0), "MoveAbsolute beyond range")
    check("MoveAbsolute(-175, 0) stops at PanMin", abs(r.Position.Pan - rng.PanMin) < 1.5, str(r.Position))
    expect_error("MoveAbsolute(200, 0) -> ValidationError", lambda: ptz.MoveAbsolute(200.0, 0.0), None)

    # concurrent movement is rejected
    first = ptz.MoveAbsolute(-85.0, 0.0)
    time.sleep(0.5)
    second = ptz.MoveHome()
    try:
        second.get_responses() if second.done else wait(second, "second move")
        check("concurrent move -> MovementInProgress", False, "second move succeeded")
    except DefinedExecutionError as e:
        check("concurrent move -> MovementInProgress", e.identifier == "MovementInProgress", e.identifier)
    wait(first, "first move")

    # snapshot during movement
    mv = ptz.MoveAbsolute(85.0, 0.0)
    time.sleep(0.5)
    r = vid.TakeSnapshot("Sub")
    check("TakeSnapshot during movement", r.Image[:2] == b"\xff\xd8")
    wait(mv, "move")

    # presets
    before = ptz.GetPresets().Presets
    saved = ptz.SavePreset("smoke テスト").Preset
    check("SavePreset", bool(saved.PresetToken), str(saved))
    names = {x.PresetToken: x.Name for x in ptz.GetPresets().Presets}
    check("GetPresets contains saved", names.get(saved.PresetToken) == "smoke テスト", str(names))
    wait(ptz.MoveHome(), "MoveHome")
    again = ptz.SavePreset("smoke テスト").Preset  # same name -> overwrite
    n_after = len(ptz.GetPresets().Presets)
    check("SavePreset same name overwrites", again.PresetToken == saved.PresetToken and n_after == len(before) + 1, str(again))
    wait(ptz.MoveAbsolute(68.0, 0.0), "move away")
    r = wait(ptz.GotoPreset(saved.PresetToken), "GotoPreset")
    check("GotoPreset returns to overwritten position (home)", near(r.Position, 0, 0), str(r.Position))
    expect_error("GotoPreset(unknown) -> PresetNotFound", lambda: wait(ptz.GotoPreset("9999"), "x"), "PresetNotFound")
    expect_error("RemovePreset(unknown) -> PresetNotFound", lambda: ptz.RemovePreset("9999"), "PresetNotFound")
    expect_error("SavePreset('a&b') -> ValidationError", lambda: ptz.SavePreset("a&b"), None)
    expect_error("SavePreset(33 chars) -> ValidationError", lambda: ptz.SavePreset("x" * 33), None)
    expect_error("SavePreset(12 JP chars = 36 bytes) -> InvalidPresetName", lambda: ptz.SavePreset("あ" * 12), "InvalidPresetName")
    ptz.RemovePreset(saved.PresetToken)
    check("RemovePreset", saved.PresetToken not in {x.PresetToken for x in ptz.GetPresets().Presets})

    print("== VideoCaptureController: recordings ==")
    n0 = len(vid.ListRecordings().Recordings)
    # record while moving
    rec_cmd = vid.RecordVideo("Sub", 6.0, True)
    time.sleep(1.0)
    mv = ptz.MoveRelative(51.0, 0.0)
    wait(mv, "move during recording")
    rec = wait(rec_cmd, "RecordVideo").Recording
    check("RecordVideo(Sub, 6 s, audio)", abs(rec.Duration - 6.0) < 0.3 and rec.IncludeAudio, f"{rec.Duration:.2f} s, {rec.Width}x{rec.Height}, {rec.FileSize} B, id {rec.RecordingId}")
    rec2 = wait(vid.RecordVideo("Main", 3.0, False), "RecordVideo main").Recording
    check("RecordVideo(Main, 3 s, no audio)", not rec2.IncludeAudio and rec2.Width > 1000, f"{rec2.Duration:.2f} s, {rec2.Width}x{rec2.Height}, {rec2.FileSize} B")
    lst = vid.ListRecordings().Recordings
    check("ListRecordings", len(lst) == n0 + 2, f"{len(lst)} recordings")
    data = vid.GetRecording(rec2.RecordingId).Video
    (out / "recording_main.mp4").write_bytes(data)
    check("GetRecording (> 2 MB uses binary transfer if needed)", len(data) == rec2.FileSize, f"{len(data)} B")
    data = vid.GetRecording(rec.RecordingId).Video
    (out / "recording_sub_audio.mp4").write_bytes(data)
    check("GetRecording (sub)", len(data) == rec.FileSize, f"{len(data)} B")
    expect_error("GetRecording(unknown uuid) -> RecordingNotFound",
                 lambda: vid.GetRecording("00000000-0000-4000-8000-000000000000"), "RecordingNotFound")
    expect_error("GetRecording('../etc') -> ValidationError", lambda: vid.GetRecording("../../etc/passwd"), None)
    for r_ in (rec, rec2):
        vid.DeleteRecording(r_.RecordingId)
    check("DeleteRecording", len(vid.ListRecordings().Recordings) == n0)
    expect_error("DeleteRecording(deleted) -> RecordingNotFound", lambda: vid.DeleteRecording(rec.RecordingId), "RecordingNotFound")

    print("== CameraSettingsController ==")
    cs = client.CameraSettingsController
    orig_img = cs.ImageSettings.get()
    orig_res = cs.MainStreamResolution.get()
    avail = [(r.Width, r.Height) for r in cs.AvailableMainStreamResolutions.get()]
    check("ImageSettings", 0 <= orig_img.ColorSaturation <= 100, str(orig_img))
    check("MainStreamResolution / Available", (orig_res.Width, orig_res.Height) in avail, f"{orig_res} in {avail}")
    try:
        r = cs.SetImageSettings(0, 60, 40).ImageSettings
        check("SetImageSettings(0, 60, 40)", (r.ColorSaturation, r.Contrast, r.Sharpness) == (0, 60, 40), str(r))
        r = cs.ResetImageSettings().ImageSettings
        check("ResetImageSettings -> 50/50/50", (r.ColorSaturation, r.Contrast, r.Sharpness) == (50, 50, 50), str(r))
        expect_error("SetImageSettings(101, ...) -> ValidationError", lambda: cs.SetImageSettings(101, 50, 50), None)
        expect_error("SetMainStreamResolution(1280x720) -> UnsupportedResolution",
                     lambda: cs.SetMainStreamResolution(1280, 720), "UnsupportedResolution")
        other = next((wh for wh in avail if wh != (orig_res.Width, orig_res.Height)), None)
        if other:
            r = cs.SetMainStreamResolution(*other).Resolution
            check(f"SetMainStreamResolution{other}", (r.Width, r.Height) == other, str(r))
            snap = vid.TakeSnapshot("Main")
            check("Main snapshot uses new resolution", (snap.Width, snap.Height) == other, f"{snap.Width}x{snap.Height}")
            rec_cmd = vid.RecordVideo("Main", 4.0, False)
            time.sleep(1.5)
            expect_error("SetMainStreamResolution during recording -> OperationInProgress",
                         lambda: cs.SetMainStreamResolution(orig_res.Width, orig_res.Height), "OperationInProgress")
            rec = wait(rec_cmd, "RecordVideo during settings").Recording
            check("recording at new resolution", (rec.Width, rec.Height) == other, f"{rec.Width}x{rec.Height}")
            vid.DeleteRecording(rec.RecordingId)
    finally:
        cs.SetImageSettings(orig_img.ColorSaturation, orig_img.Contrast, orig_img.Sharpness)
        r = cs.SetMainStreamResolution(orig_res.Width, orig_res.Height).Resolution
        check("settings restored", (r.Width, r.Height) == (orig_res.Width, orig_res.Height), str(r))

    wait(ptz.MoveHome(), "MoveHome")
    check("final Status Idle", cam.Status.get() == 1)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for name, _, detail in failed:
        print(f"  FAILED: {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
