"""TOML configuration file for the server.

Precedence: command-line option > configuration file > built-in default.
Relative paths in the file are resolved against the directory of the file.
Secrets are not accepted here: the camera password lives in the OS credential store
(see ``credentials.py``); any key containing "password" is rejected.

Example: see ``tapo-camera.example.toml`` in the repository root.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class _Key:
    param: str  # name of the corresponding main() parameter
    type: type | tuple[type, ...]
    is_path: bool = False
    choices: tuple[str, ...] | None = None


SCHEMA: dict[str, dict[str, _Key]] = {
    "server": {
        "address": _Key("ip_address", str),
        "port": _Key("port", int),
        "uuid": _Key("server_uuid", str),
        "name": _Key("server_name", str),
        "description": _Key("server_description", str),
        "insecure": _Key("insecure", bool),
        "disable_discovery": _Key("disable_discovery", bool),
        "cert_file": _Key("cert_file", str, is_path=True),
        "private_key_file": _Key("private_key_file", str, is_path=True),
        "ca_file_for_discovery": _Key("ca_file_for_discovery", str, is_path=True),
    },
    "camera": {
        "profile": _Key("camera_profile", str),
        "ptz": _Key("ptz", str, choices=("auto", "on", "off")),
        "pan_range_deg": _Key("pan_range_deg", (int, float)),
        "tilt_range_deg": _Key("tilt_range_deg", (int, float)),
    },
    "recordings": {
        "directory": _Key("recording_dir", str, is_path=True),
        "max_age_hours": _Key("max_recording_age_hours", (int, float)),
        "max_storage_mb": _Key("max_recording_storage_mb", (int, float)),
    },
    "logging": {
        "level": _Key("log_level", str, choices=("error", "warning", "info", "debug")),
    },
}


def _reject_secrets(data: Any, where: str = "") -> None:
    if isinstance(data, dict):
        for k, v in data.items():
            if "password" in str(k).lower() or "secret" in str(k).lower():
                raise ConfigError(
                    f"'{where}{k}': secrets must not be stored in the configuration file; "
                    "use scripts/set_credentials.py (OS credential store) instead"
                )
            _reject_secrets(v, f"{where}{k}.")


def load(path: Path) -> dict[str, Any]:
    """Read and validate a configuration file; return {main() parameter name: value}."""
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"configuration file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from None

    _reject_secrets(data)
    out: dict[str, Any] = {}
    for section, values in data.items():
        if section not in SCHEMA:
            raise ConfigError(f"{path}: unknown section [{section}] (allowed: {', '.join(SCHEMA)})")
        if not isinstance(values, dict):
            raise ConfigError(f"{path}: '{section}' must be a section ([{section}])")
        for key, value in values.items():
            spec = SCHEMA[section].get(key)
            if spec is None:
                raise ConfigError(
                    f"{path}: unknown key '{key}' in [{section}] (allowed: {', '.join(SCHEMA[section])})"
                )
            # bool is a subclass of int: do not accept true/false for numbers
            if not isinstance(value, spec.type) or (isinstance(value, bool) and spec.type is not bool):
                raise ConfigError(f"{path}: [{section}] {key} has the wrong type ({type(value).__name__})")
            if spec.choices and value not in spec.choices:
                raise ConfigError(f"{path}: [{section}] {key} must be one of {', '.join(spec.choices)}")
            if spec.is_path:
                p = Path(value).expanduser()
                value = str(p if p.is_absolute() else (path.parent / p).resolve())
            out[spec.param] = value
    return out
