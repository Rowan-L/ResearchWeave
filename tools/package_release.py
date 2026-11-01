"""安全打包脚本(B35 事故后新增,2026-10-30)。

PyInstaller 构建时会整目录删除 dist/ResearchWeave——**连同其中打包版自己的
data 目录(用户库/译文/API 设置)一起删掉**。此脚本先备份 data、构建、再
原样恢复。以后打包一律用本脚本,不要直接跑 PyInstaller:

    SD/python.exe tools/package_release.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "dist" / "ResearchWeave"
DATA_DIR = APP_DIR / "data"
BACKUP_DIR = ROOT / "dist" / "_data_backup"


def _app_running() -> bool:
    # 注意:不要用 tasklist /FI —— Git Bash 会把 "/FI" 当路径做 MSYS 转换,
    # 过滤失效导致漏检正在运行的实例(B35 当晚实际踩坑)。
    probe = subprocess.run(["tasklist"], check=False, capture_output=True, text=True)
    return "ResearchWeave.exe" in (probe.stdout or "")


def main() -> int:
    if _app_running():
        print(
            "检测到 ResearchWeave.exe 正在运行:单实例锁会让新构建的 exe 秒退,"
            "且 data 备份可能不完整。请先关闭应用(或 taskkill /F /IM ResearchWeave.exe)再打包。",
            file=sys.stderr,
        )
        return 1

    backup: Path | None = None
    if DATA_DIR.exists():
        if BACKUP_DIR.exists():
            shutil.rmtree(BACKUP_DIR)
        BACKUP_DIR.mkdir(parents=True)
        backup = BACKUP_DIR / "data"
        shutil.move(str(DATA_DIR), str(backup))
        print(f"已备份打包版数据 -> {backup}")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "PyInstaller", "SynthRecall.spec", "--noconfirm"],
            check=False,
            cwd=ROOT,
        )
        if result.returncode != 0:
            print("PyInstaller 构建失败。", file=sys.stderr)
            return result.returncode
    finally:
        if backup is not None and backup.exists():
            if DATA_DIR.exists():
                shutil.rmtree(DATA_DIR)
            shutil.move(str(backup), str(DATA_DIR))
            print(f"已恢复打包版数据 -> {DATA_DIR}")
        if BACKUP_DIR.exists():
            shutil.rmtree(BACKUP_DIR, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
