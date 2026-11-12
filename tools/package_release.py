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

# 构建期 data 备份/恢复的对账结果;非空 = 有用户数据疑似丢失
_RESTORE_PROBLEMS: list[str] = []


def _app_running() -> bool:
    # 注意:不要用 tasklist /FI —— Git Bash 会把 "/FI" 当路径做 MSYS 转换,
    # 过滤失效导致漏检正在运行的实例(B35 当晚实际踩坑)。
    probe = subprocess.run(["tasklist"], check=False, capture_output=True, text=True)
    return "ResearchWeave.exe" in (probe.stdout or "")


def _dir_inventory(path: Path) -> tuple[int, int]:
    """返回(文件数, 总字节数),用于备份/恢复完整性对账。"""
    count = 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
            count += 1
    return count, total


def main() -> int:
    if _app_running():
        print(
            "检测到 ResearchWeave.exe 正在运行:单实例锁会让新构建的 exe 秒退,"
            "且 data 备份可能不完整。请先关闭应用(或 taskkill /F /IM ResearchWeave.exe)再打包。",
            file=sys.stderr,
        )
        return 1

    backup: Path | None = None
    inventory: tuple[int, int] | None = None
    if DATA_DIR.exists():
        inventory = _dir_inventory(DATA_DIR)
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
            if inventory is not None:
                restored = _dir_inventory(DATA_DIR)
                if restored != inventory:
                    message = (
                        f"数据完整性对账失败:备份前 {inventory[0]} 文件/{inventory[1]} 字节,"
                        f"恢复后 {restored[0]} 文件/{restored[1]} 字节——部分数据已在构建中丢失!"
                    )
                    print(f"!! {message}", file=sys.stderr)
                    _RESTORE_PROBLEMS.append(message)
                else:
                    print(f"数据完整性对账通过({inventory[0]} 文件/{inventory[1]} 字节)。")
        elif inventory is not None:
            message = "构建前存在 data 但备份目录缺失,数据无法恢复!"
            print(f"!! {message}", file=sys.stderr)
            _RESTORE_PROBLEMS.append(message)
        if BACKUP_DIR.exists():
            shutil.rmtree(BACKUP_DIR, ignore_errors=True)
    if _RESTORE_PROBLEMS:
        # 数据疑似丢失:明确报错,让人工核实后再发版
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
