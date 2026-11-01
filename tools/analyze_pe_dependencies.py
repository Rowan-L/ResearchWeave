from __future__ import annotations

from collections import deque
from pathlib import Path
import sys

import pefile


def imported_dlls(path: Path) -> list[str]:
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(
        directories=[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
        ]
    )
    names: list[str] = []
    for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
        names.append(entry.dll.decode(errors="replace"))
    for entry in getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", []):
        names.append(entry.dll.decode(errors="replace"))
    return names


def main() -> int:
    bundle = Path(sys.argv[1]).resolve()
    start = (bundle / "PySide6" / "QtGui.pyd").resolve()
    search_dirs = [
        start.parent,
        bundle / "shiboken6",
        bundle,
        Path("C:/Windows/System32"),
        Path("C:/Windows/System"),
        Path("C:/Windows"),
    ]
    indexes = [
        {item.name.casefold(): item for item in directory.glob("*") if item.is_file()}
        if directory.is_dir()
        else {}
        for directory in search_dirs
    ]
    queue = deque([start])
    visited: set[Path] = set()
    missing: dict[str, set[str]] = {}
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        try:
            dependencies = imported_dlls(current)
        except pefile.PEFormatError as exc:
            print(f"INVALID {current}: {exc}")
            continue
        for name in dependencies:
            resolved = next(
                (index[name.casefold()] for index in indexes if name.casefold() in index),
                None,
            )
            if resolved is None:
                if not name.casefold().startswith(("api-ms-win-", "ext-ms-win-")):
                    missing.setdefault(name, set()).add(str(current))
                continue
            if bundle in resolved.parents:
                queue.append(resolved.resolve())

    print(f"SCANNED={len(visited)}")
    if missing:
        for name, parents in sorted(missing.items()):
            print(f"MISSING={name}")
            for parent in sorted(parents):
                print(f"  REQUIRED_BY={parent}")
        return 1
    print("MISSING=NONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
