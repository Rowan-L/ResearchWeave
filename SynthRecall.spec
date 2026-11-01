# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules


# 构建机 conda 环境路径:构建脚本须用目标环境的 python 运行 PyInstaller,
# 默认取 sys.prefix(即当前解释器所在环境);也可用环境变量 RESEARCHWEAVE_CONDA_ENV 覆盖
CONDA_ENV = Path(os.environ.get('RESEARCHWEAVE_CONDA_ENV', sys.prefix))
CONDA_DLL_DIR = CONDA_ENV / 'Library' / 'bin'
SITE_PACKAGES_DIR = CONDA_ENV / 'Lib' / 'site-packages'
CONDA_DLLS = [
    'ffi.dll',
    'libbz2.dll',
    'libcrypto-3-x64.dll',
    'libexpat.dll',
    'liblzma.dll',
    'libssl-3-x64.dll',
    'sqlite3.dll',
]
extra_binaries = [
    (str(CONDA_DLL_DIR / name), '.')
    for name in CONDA_DLLS
    if (CONDA_DLL_DIR / name).exists()
]
extra_binaries += [
    (str(path), '.')
    for path in (SITE_PACKAGES_DIR / 'hyperscan.libs').glob('*.dll')
]

babeldoc_hiddenimports = collect_submodules('babeldoc')
babeldoc_hiddenimports += collect_submodules('bitstring')
babeldoc_hiddenimports += collect_submodules('bitarray')
babeldoc_hiddenimports += collect_submodules('tiktoken')
babeldoc_hiddenimports += collect_submodules('tiktoken_ext')
babeldoc_datas = collect_data_files('babeldoc')
app_datas = [
    ('src/synthrecall/assets/researchweave_icon.ico', 'synthrecall/assets'),
    ('src/synthrecall/assets/researchweave_icon.png', 'synthrecall/assets'),
]
# SD 环境的新版 PyMuPDF 自带 layout 分析子模块,import 时即加载 onnx 模型
# 描述文件;PyInstaller 默认不收集,缺它启动即 FileNotFoundError
pymupdf_datas = collect_data_files('pymupdf', include_py_files=False)
app_datas += pymupdf_datas

app_a = Analysis(
    ['run_demo.py'],
    pathex=['src'],
    binaries=extra_binaries,
    datas=app_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['pyi_runtime_qt_paths.py'],
    excludes=['alembic', 'pytest', 'pytest_cov', 'pytestqt', 'ruff'],
    noarchive=False,
    optimize=0,
)
# This PySide6 build links Qt6Core against the Windows system ICU DLL.
# A build host may expose an incompatible Poppler icuuc.dll on PATH; if
# PyInstaller collects it, Qt fails at startup with WinError 127 because that
# DLL exports version-suffixed ICU symbols. Never ship that accidental copy.
incompatible_system_icu = {'icuuc.dll', 'icudt78.dll'}
app_a.binaries = [
    entry for entry in app_a.binaries
    if entry[0].replace('\\', '/').rsplit('/', 1)[-1].lower()
    not in incompatible_system_icu
]
app_pyz = PYZ(app_a.pure)
app_exe = EXE(
    app_pyz,
    app_a.scripts,
    [],
    exclude_binaries=True,
    name='ResearchWeave',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='src/synthrecall/assets/researchweave_icon.ico',
)

babeldoc_a = Analysis(
    ['babeldoc_runner.py'],
    pathex=['BabelDOC-main'],
    binaries=extra_binaries,
    datas=babeldoc_datas,
    hiddenimports=babeldoc_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['pyi_runtime_qt_paths.py'],
    excludes=['alembic', 'pytest', 'pytest_cov', 'pytestqt', 'ruff'],
    noarchive=False,
    optimize=0,
)
babeldoc_a.binaries = [
    entry for entry in babeldoc_a.binaries
    if entry[0].replace('\\', '/').rsplit('/', 1)[-1].lower()
    not in incompatible_system_icu
]
babeldoc_pyz = PYZ(babeldoc_a.pure)
babeldoc_exe = EXE(
    babeldoc_pyz,
    babeldoc_a.scripts,
    [],
    exclude_binaries=True,
    name='ResearchWeaveBabelDOC',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    app_exe,
    babeldoc_exe,
    app_a.binaries,
    babeldoc_a.binaries,
    app_a.datas,
    babeldoc_a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ResearchWeave',
)
