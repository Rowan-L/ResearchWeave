from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    root_dir: Path
    data_dir: Path
    paper_dir: Path
    database_path: Path

    @classmethod
    def from_cwd(cls) -> AppPaths:
        if getattr(sys, "frozen", False):
            root_dir = Path(sys.executable).resolve().parent
        else:
            root_dir = Path.cwd()
        return cls.from_root(root_dir)

    @classmethod
    def from_root(cls, root_dir: Path) -> AppPaths:
        root_dir = root_dir.expanduser().resolve()
        data_dir = root_dir / "data"
        return cls(
            root_dir=root_dir,
            data_dir=data_dir,
            paper_dir=data_dir / "papers",
            database_path=data_dir / "synthrecall_demo.db",
        )

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.paper_dir.mkdir(parents=True, exist_ok=True)
