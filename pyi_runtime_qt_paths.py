from __future__ import annotations

import os
from pathlib import Path
import sys


# Keep the handles alive for the whole process. On Windows, closing a handle
# returned by add_dll_directory immediately removes that directory again.
_DLL_DIRECTORY_HANDLES: list[object] = []


def _register_bundled_dll_directories() -> None:
    bundle_root_value = getattr(sys, "_MEIPASS", "")
    if not bundle_root_value:
        return

    bundle_root = Path(bundle_root_value)
    candidates = (
        bundle_root,
        bundle_root / "PySide6",
        bundle_root / "shiboken6",
    )
    existing = [path for path in candidates if path.is_dir()]
    if not existing:
        return

    os.environ["PATH"] = os.pathsep.join(
        [*(str(path) for path in existing), os.environ.get("PATH", "")]
    )
    add_dll_directory = getattr(os, "add_dll_directory", None)
    if add_dll_directory is None:
        return
    for path in existing:
        _DLL_DIRECTORY_HANDLES.append(add_dll_directory(str(path)))


_register_bundled_dll_directories()
