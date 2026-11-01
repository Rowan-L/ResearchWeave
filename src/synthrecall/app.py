from __future__ import annotations

import os
import sys

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from synthrecall.branding import APP_NAME, app_icon_path
from synthrecall.config import AppPaths
from synthrecall.database import create_session_factory, init_database
from synthrecall.repositories import LibraryRepository
from synthrecall.single_instance import SingleInstanceGuard, instance_key
from synthrecall.ui import theme
from synthrecall.ui.main_window import MainWindow
from synthrecall.workspaces import resolve_startup_root


def main(argv: list[str] | None = None) -> int:
    # F11 多工作区:按注册表的当前工作区选库目录;无注册时回退安装目录
    paths = AppPaths.from_root(resolve_startup_root(AppPaths.from_cwd().root_dir))
    paths.ensure()

    session_factory = create_session_factory(paths.database_path)
    init_database(session_factory)
    LibraryRepository(session_factory).ensure_default_categories()

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    # 加载 Qt 自带中文翻译,统一标准对话框(OK/Cancel/Close 等)的按钮文案
    qt_translator = QTranslator(app)
    translations_dir = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if qt_translator.load(
        QLocale(QLocale.Language.Chinese, QLocale.Country.China),
        "qt",
        "_",
        translations_dir,
    ):
        app.installTranslator(qt_translator)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    icon_path = app_icon_path(ico=True)
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    # B22 单实例防护:已有实例在运行时通知其激活窗口,本进程直接退出,
    # 避免双实例并发写同一 SQLite 库
    guard = SingleInstanceGuard(instance_key(APP_NAME))
    if not guard.acquire():
        if os.environ.get("SYNTHRECALL_SWITCHING"):
            # F11 切换交接:旧进程即将退出,短暂等待锁释放再占用
            import time

            for _ in range(10):
                time.sleep(0.5)
                if guard.acquire():
                    break
            else:
                return 0
        else:
            return 0

    repository = LibraryRepository(session_factory)
    # F10:主窗口构建前恢复外观设置(主题/字号/密度,_apply_style 读取)
    theme.configure(paths.data_dir)
    window = MainWindow(repository=repository, data_dir=paths.data_dir)
    if icon_path.exists():
        window.setWindowIcon(QIcon(str(icon_path)))
    guard.raiseRequested.connect(
        lambda: (window.showNormal(), window.raise_(), window.activateWindow())
    )
    window.resize(1320, 820)
    window.show()

    return app.exec()
