"""PyInstaller runtime hook(F23 精简版):把 shim 目录插到 sys.path 首位。

精简包排除真 cv2/skimage/sklearn/scipy/lxml,由 zip_version/shims/ 里的
纯 numpy/PIL 兼容层顶替。BabelDOC 在翻译子进程里 import cv2 时,冻结导入器
没有该模块,就会落到 PathFinder 按本目录找到 shim。
"""

import os
import sys

_bundle_root = getattr(sys, "_MEIPASS", "")
if _bundle_root:
    _shims_dir = os.path.join(_bundle_root, "shims")
    if os.path.isdir(_shims_dir):
        sys.path.insert(0, _shims_dir)
