"""F23 精简版构建脚本(zip_version 专用)。

用法:
    <目标环境>/python.exe zip_version/build_lite.py           # 构建+瘦身+体积报告
    <目标环境>/python.exe zip_version/build_lite.py --zip     # 额外产出精简 zip

铁律:
- 产物固定到 zip_version/dist/ResearchWeave,中间产物在 zip_version/build;
  通过显式 --distpath/--workpath 保证 **绝不写项目根 dist/**——完整版
  dist/ResearchWeave 是用户在用的实例,PyInstaller 会整目录删除同名产物
  (B35 事故),所以本脚本从不触碰根目录 SynthRecall.spec 的默认输出路径。
- 构建后删除 PySide6 中确认未使用的组件(opengl32sw 软件渲染回退、Qt 翻译、
  Quick/Qml/Pdf/OpenGL 模块——src 里无任何对应 import,单实例锁需要的
  QtNetwork 与 tls 插件保留)。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # zip_version/
ROOT = HERE.parent                              # 项目根
DIST = HERE / "dist"
BUILD = HERE / "build"
APP = DIST / "ResearchWeave"
INTERNAL = APP / "_internal"
FULL_DIST_APP = ROOT / "dist" / "ResearchWeave"  # 只读参照,绝不写入

# PySide6 中确认未使用、可安全删除的文件/目录(依据:src 全量 grep 无对应
# import;Qt6Network/QtNetwork.pyd/tls 插件被 single_instance.py 使用,保留)。
# "Qt6Qml*" / "Qt6Quick*" 为通配前缀,build 后按实际存在的 DLL 删除。
PYSIDE6_DELETE = [
    "opengl32sw.dll",
    "translations",
    "qml",
    "Qt6Qml*",
    "Qt6Quick*",
    "Qt6Pdf.dll",
    "Qt6OpenGL.dll",
    "Qt6OpenGLWidgets.dll",
]


def dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total


def fmt(n: int) -> str:
    return f"{n / 1024 / 1024:.1f}MB"


def _slim_app_running() -> bool:
    """精简版是否在运行(运行中构建会锁库且 data 备份不完整)。"""
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


# 构建期 data 备份/恢复的对账结果;非空 = 有用户数据疑似丢失
_RESTORE_PROBLEMS: list[str] = []


def backup_slim_data() -> Path | None:
    """构建前把精简版 data 目录移到备份位,构建后原样恢复。

    PyInstaller 构建会整目录删除 dist/ResearchWeave——精简包实例里的
    文献/译文/阅读记录同样是用户数据(B35 同款风险),不能随构建清掉。
    2026-11-13 曾发现 DB 完好恢复而 papers/arxiv 丢失的事故,故恢复后
    必须做文件数/字节对账(见 build())。
    """
    data = APP / "data"
    if not data.exists():
        return None
    backup_root = HERE / "_data_backup"
    if backup_root.exists():
        shutil.rmtree(backup_root)
    backup_root.mkdir(parents=True)
    backup = backup_root / "data"
    shutil.move(str(data), str(backup))
    print(f"已备份精简版数据 -> {backup}")
    return backup


def restore_slim_data(
    backup: Path | None, inventory: tuple[int, int] | None
) -> None:
    if backup is None or not backup.exists():
        if inventory is not None:
            message = "构建前存在 data 但备份目录缺失,数据无法恢复!"
            print(f"!! {message}", file=sys.stderr)
            _RESTORE_PROBLEMS.append(message)
        return
    target = APP / "data"
    if target.exists():
        shutil.rmtree(target)
    shutil.move(str(backup), str(target))
    print(f"已恢复精简版数据 -> {target}")
    backup_root = HERE / "_data_backup"
    if backup_root.exists():
        shutil.rmtree(backup_root, ignore_errors=True)
    if inventory is not None:
        restored = _dir_inventory(target)
        if restored != inventory:
            message = (
                f"数据完整性对账失败:备份前 {inventory[0]} 文件/{inventory[1]} 字节,"
                f"恢复后 {restored[0]} 文件/{restored[1]} 字节——部分数据已在构建中丢失!"
            )
            print(f"!! {message}", file=sys.stderr)
            _RESTORE_PROBLEMS.append(message)
        else:
            print(f"数据完整性对账通过({inventory[0]} 文件/{inventory[1]} 字节)。")


def build() -> None:
    if _slim_app_running():
        print(
            "检测到精简版 ResearchWeave.exe 正在运行:请先退出应用再构建,"
            "否则 data 备份不完整且文件被占用。",
            file=sys.stderr,
        )
        raise SystemExit(1)
    data = APP / "data"
    inventory = _dir_inventory(data) if data.exists() else None
    backup = backup_slim_data()
    try:
        if (DIST / "ResearchWeave").exists():
            print("清理旧精简产物 ...")
            shutil.rmtree(DIST / "ResearchWeave")
        cmd = [
            sys.executable, "-m", "PyInstaller",
            str(HERE / "ResearchWeaveLite.spec"),
            "--noconfirm",
            "--distpath", str(DIST),
            "--workpath", str(BUILD),
        ]
        print("PyInstaller:", " ".join(cmd))
        result = subprocess.run(cmd, cwd=str(ROOT), check=False)
        if result.returncode != 0:
            print("PyInstaller 构建失败", file=sys.stderr)
            raise SystemExit(result.returncode)
    finally:
        restore_slim_data(backup, inventory)
    if _RESTORE_PROBLEMS:
        # 数据疑似丢失:不许继续清理/打包/发版,先人工核实
        raise SystemExit("构建完成,但数据备份/恢复对账失败,已中止后续步骤")


def post_cleanup() -> None:
    pyside = INTERNAL / "PySide6"
    for pattern in PYSIDE6_DELETE:
        for target in pyside.glob(pattern):
            if target.is_dir():
                shutil.rmtree(target)
                print(f"已删 PySide6/{target.name}/")
            elif target.exists():
                target.unlink()
                print(f"已删 PySide6/{target.name}")
    # PyInstaller 的 pymupdf 相关 hook 会把 cv2/scipy 的二进制目录塞进包,
    # 尽管 Python 模块层面已 excludes(warn 文件可证)。运行期 import 只会
    # 命中 sys.path[0] 的 shims,这些目录是死重量,直接删除。
    # scipy.libs 是 hook 顺带收集的 scipy 依赖 DLL(scipy/sklearn/skimage 均
    # 已排除,numpy 用自己的 numpy.libs),同样无人引用。
    for leaked in ("cv2", "scipy", "scipy.libs", "skimage", "sklearn", "lxml"):
        target = INTERNAL / leaked
        if target.exists():
            shutil.rmtree(target)
            print(f"已删 hook 泄漏的 _internal/{leaked}/")
    pycache = INTERNAL / "shims" / "__pycache__"
    if pycache.exists():
        shutil.rmtree(pycache)
        print("已删 _internal/shims/__pycache__/")


def assert_sanity() -> None:
    """构建后自检:精简件就位、被排除库确实不在、根 dist 完好。"""
    problems: list[str] = []
    for problem in _RESTORE_PROBLEMS:
        problems.append(f"data 备份/恢复对账失败:{problem}")
    shims = INTERNAL / "shims"
    if not (shims / "cv2.py").is_file():
        problems.append("shims/cv2.py 未收进包")
    for heavy in ("cv2", "skimage", "sklearn", "scipy", "lxml"):
        if (INTERNAL / heavy).exists():
            problems.append(f"_internal/{heavy} 未被排除")
    if not (APP / "ResearchWeave.exe").exists() or not (APP / "ResearchWeaveBabelDOC.exe").exists():
        problems.append("exe 缺失")
    if not FULL_DIST_APP.exists() or not (FULL_DIST_APP / "data").exists():
        problems.append("完整版 dist/ResearchWeave 受影响——立即检查!")
    if problems:
        for p in problems:
            print("!!", p, file=sys.stderr)
        raise SystemExit(1)
    print("自检通过:shim 就位、重库已排除、完整版 dist 完好。")


def size_report() -> None:
    print(f"\n精简包总大小: {fmt(dir_size(APP))}  ({APP})")
    entries = []
    for p in INTERNAL.iterdir():
        if p.is_dir():
            entries.append((dir_size(p), p.name + "/"))
        else:
            entries.append((p.stat().st_size, p.name))
    for size, name in sorted(entries, reverse=True)[:16]:
        print(f"  {fmt(size):>9}  {name}")


def make_zip() -> None:
    out = DIST / "ResearchWeave-Windows-x64-slim.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in sorted(APP.rglob("*")):
            rel = p.relative_to(APP.parent)
            if "ResearchWeave/data" in p.as_posix():
                continue  # data/ 首次运行自动生成,不进 zip
            if p.is_file():
                zf.write(p, str(rel))
    print(f"\nzip 产物: {out}  ({fmt(out.stat().st_size)})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", action="store_true", help="构建后生成精简 zip")
    parser.add_argument("--no-build", action="store_true", help="跳过构建,只做清理/报告/zip")
    args = parser.parse_args()

    if not args.no_build:
        build()
    post_cleanup()
    assert_sanity()
    size_report()
    if args.zip:
        make_zip()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
