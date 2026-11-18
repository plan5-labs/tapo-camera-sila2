# Tapo Camera Controller (SiLA 2)

> ⚠️ **Disclaimer**: This software is under active development. It comes with
> **no warranty** and the authors accept **no liability** for any results or
> damages. Use at your own risk, and verify safety before connecting to real
> hardware.

A Python SiLA 2 server for TP-Link Tapo network cameras: still snapshots, fixed-duration
video recording, and pan/tilt with presets.

- Control path: ONVIF (TCP 2020) + RTSP (TCP 554), authenticated with the **camera account**
  only. No TP-Link cloud account/password is used, and the camera is reached on the local network only.
- Verified model: **Tapo C232** (firmware 1.9.1). Other Tapo cameras exposing the same ONVIF/RTSP
  interface (e.g. C200, C210, C220, C225, C230) are expected to work but are untested.
  Cameras without pan/tilt are supported; the PanTiltController feature is then not offered.

## Features

Feature definitions: [features/](features/) (the descriptions are written so that a connected
client or agent can understand coordinates, units and limits).

| Feature | Commands | Properties |
|---|---|---|
| TapoCameraController | Reset | Status (0 Not Connected / 1 Idle / 2 Running / 3 Error), DeviceInformation |
| VideoCaptureController | TakeSnapshot, RecordVideo (observable), GetRecording, ListRecordings, DeleteRecording | – |
| PanTiltController (v2) | MoveAbsolute, MoveRelative, MoveHome, GotoPreset (observable), GetPresets, SavePreset, RemovePreset | CurrentPosition, MovementRange |
| CameraSettingsController | SetImageSettings, ResetImageSettings, SetMainStreamResolution | ImageSettings, MainStreamResolution, AvailableMainStreamResolutions |

Key behavior:

- **Streams**: `Main` (C232: 2880×1620) or `Sub` (640×360). Video is stored without re-encoding
  (MP4/H.264); optional audio is converted to AAC.
- **Recordings** are identified by a random UUID v4 and downloaded with `GetRecording`;
  file paths are never exposed. Old recordings are deleted automatically (see options).
- **Pan/tilt angles** are in degrees from the power-up front (logo side). Pan + = left (seen
  from behind), Tilt + = up. C232 range: pan −170…+170, tilt −34.5…+34.5 (`MovementRange`);
  a target beyond it stops at the limit. Angles are converted linearly from the camera's
  normalized ONVIF coordinates (−1…+1) and are approximate (within several degrees).
  Movement commands return when the camera has stopped.
  (PanTiltController v1 used the normalized coordinates directly.)
- **Snapshot time**: `CapturedAt` is when the server received the frame (server clock, UTC);
  the time overlaid by the camera is typically a few seconds earlier.
- Snapshots and recordings may run concurrently with each other and with movements;
  only one movement at a time (`MovementInProgress`).
- Camera limits (C232): at most 8 presets; preset names ≤ 32 bytes UTF-8 without `& < > " '`.
- **Camera settings** are stored in the camera and affect every client (including the Tapo app).
  Effective on C232: color saturation, contrast, sharpness (weak), main-stream resolution
  (2880×1620 / 1920×1080). Not offered because the C232 ignores them over ONVIF: brightness,
  bit rate, encoding quality. Day/night mode, exposure, white balance and flip are not available
  via ONVIF. Changing the resolution restarts the streams (~4 s) and is rejected while a
  snapshot or recording is running.

## Setup

Prerequisites: [uv](https://docs.astral.sh/uv/), a Tapo camera on the same network, and a
camera account (Tapo app: Camera Settings > Advanced Settings > Camera Account).

```ps1
uv sync
uv run scripts/set_credentials.py     # run in your own terminal; stores host/user/password
```

Credentials are stored in the OS credential store (Windows Credential Manager) via `keyring`,
never in the repository. `--profile <name>` manages several cameras; `--delete` removes a profile.
`TAPO_HOST` / `TAPO_USER` / `TAPO_PASSWORD` environment variables override the stored values
(e.g. for containers). The password is masked in all log output.

Note for WSL2: `keyring` has no backend there by default; use the environment variables instead.

## Running the server

```ps1
uv run tapo-camera-controller --insecure -a 127.0.0.1 -p 50052 --verbose
```

Camera-specific options (in addition to the standard sila2 options such as `-a`, `-p`,
`--insecure`, `-c/-k` for TLS, `--server-uuid`):

| Option | Default | Meaning |
|---|---|---|
| `--camera-profile` | `default` | Credential profile to use |
| `--recording-dir` | `%LOCALAPPDATA%\tapo-camera-controller\recordings` | Where recordings are stored |
| `--max-recording-age-hours` | 168 | Delete older recordings (0 = keep forever) |
| `--max-recording-storage-mb` | 2048 | Delete the oldest recordings above this total (0 = no limit) |
| `--ptz` | `auto` | Offer PanTiltController: `auto` / `on` / `off` |
| `--pan-range-deg` | 340 | Mechanical pan range of the model, for the conversion to degrees |
| `--tilt-range-deg` | 69 | Mechanical tilt range of the model, for the conversion to degrees |

If the camera is unreachable at startup, the server still starts with status Not Connected;
call `Reset` to reconnect.

### Configuration file

All of the options above (and address, port, TLS files, server UUID/name, log level) can be
set in a TOML file:

```ps1
copy tapo-camera.example.toml tapo-camera.toml   # tapo-camera.toml is git-ignored
uv run tapo-camera-controller --config tapo-camera.toml
```

- Precedence: command-line option > configuration file > built-in default.
- Relative paths are resolved against the directory of the configuration file.
- Unknown sections/keys and wrong types are rejected at startup.
- Secrets are not accepted (any key containing `password` is rejected); credentials stay in the
  OS credential store.

The startup log states where recordings are stored, e.g.
`Recordings: D:\lab-data\tapo\recordings (file name <RecordingId>.mp4; keep 168 h, limit 2048 MB)`.
A recording with ID `<id>` is the file `<directory>/<id>.mp4`, with its metadata
(start time, duration, stream, resolution, size) in `<directory>/<id>.json`.

## Client example

```python
import time
from sila2.client import SilaClient


def wait(cmd):  # observable commands run asynchronously
    while not cmd.done:
        time.sleep(0.3)
    return cmd.get_responses()


c = SilaClient("127.0.0.1", 50052, insecure=True)
img = c.VideoCaptureController.TakeSnapshot("Main").Image                # JPEG bytes
pos = wait(c.PanTiltController.MoveRelative(90.0, 0.0)).Position        # 90° left
rec = wait(c.VideoCaptureController.RecordVideo("Sub", 10.0, False)).Recording
mp4 = c.VideoCaptureController.GetRecording(rec.RecordingId).Video
```

## Development

- `src/tapo_camera_controller/generated/` is produced by `sila2-codegen` from `features/*.sila.xml`.
  After editing or adding a feature definition run `uv run scripts/codegen.py` (replaces
  `generated/` and adds implementation stubs for new features; hand-written code is untouched).
- Device layer: `camera.py` (status, locking, waiting for movements), `onvif.py` (minimal SOAP client),
  `rtsp.py` (PyAV snapshot/recording), `recordings.py` (UUID-addressed storage and retention).
- `scripts/probe.py` checks what a camera supports via ONVIF/RTSP (`--move` also moves it).
- `scripts/smoke_test.py` runs an end-to-end test against a running server (the camera moves;
  presets and recordings it creates are removed again).

## License

MIT License (see [LICENSE](LICENSE)). Copyright (c) 2026 RIKEN. This project is not affiliated with or endorsed by TP-Link.
Dependencies are distributed under their own licenses (e.g. sila2: MIT, PyAV: BSD-3-Clause with bundled FFmpeg libraries).
