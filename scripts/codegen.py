"""Regenerate src/tapo_camera_controller/generated from features/*.sila.xml.

    uv run scripts/codegen.py

`sila2-codegen update/add-features` do not support the src/ layout, so this script
generates a complete package into a temporary directory and then
  - replaces src/tapo_camera_controller/generated/ entirely (never edit it by hand), and
  - copies implementation stubs only for features that have no implementation yet.
Hand-written files (feature_implementations/*, server.py, __main__.py, ...) are never touched.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = "tapo_camera_controller"
DEST = ROOT / "src" / PKG


def main() -> int:
    features = sorted((ROOT / "features").glob("*.sila.xml"))
    with tempfile.TemporaryDirectory() as tmp:
        cmd = ["sila2-codegen", "new-package", "-n", PKG, "-o", tmp, *map(str, features)]
        if subprocess.run(cmd).returncode != 0:
            return 1
        gen = Path(tmp) / PKG
        shutil.rmtree(DEST / "generated")
        shutil.copytree(gen / "generated", DEST / "generated")
        print(f"replaced {DEST / 'generated'}")
        for stub in sorted((gen / "feature_implementations").glob("*_impl.py")):
            target = DEST / "feature_implementations" / stub.name
            if not target.exists():
                shutil.copy2(stub, target)
                print(f"NEW implementation stub: {target} (register it in server.py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
