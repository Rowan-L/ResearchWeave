"""B22 单实例防护:同一用户只允许一个研脉实例写同一数据库。

第二实例启动时通过本地命名通道通知既有实例把主窗口带到前台,然后自行退出
(避免此前「双实例并发写 SQLite 导致启动即脏/静默退出」的问题)。
通道名按用户区分,同机多用户互不干扰。
"""

from __future__ import annotations

import getpass

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def instance_key(app_name: str) -> str:
    """按用户区分的单实例通道名(Windows 命名管道/Linux 抽象 socket 全局可见)。"""
    user = getpass.getuser().replace("\\", "-").replace("/", "-") or "default"
    return f"{app_name}-single-instance-{user}"


class SingleInstanceGuard(QObject):
    """acquire() 返回 True 表示本进程是首个实例并已开始监听;

    返回 False 表示已有实例在运行(已请求其激活窗口),调用方应立即退出。
    首个实例在收到第二实例通知时发出 raiseRequested(主窗口自行连接)。
    """

    raiseRequested = Signal()

    def __init__(self, key: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._key = key
        self._server: QLocalServer | None = None

    def acquire(self, timeout_ms: int = 500) -> bool:
        probe = QLocalSocket()
        probe.connectToServer(self._key)
        if probe.waitForConnected(300):
            probe.write(b"raise\n")
            probe.waitForBytesWritten(timeout_ms)
            probe.disconnectFromServer()
            return False

        # 残留的 server 名(上次进程崩溃未清理)会让 listen 失败,先移除;
        # Windows 上对活动管道 removeServer 无效,不影响真正的既有实例
        QLocalServer.removeServer(self._key)
        server = QLocalServer(self)
        if not server.listen(self._key):
            return False
        server.newConnection.connect(self._on_new_connection)
        self._server = server
        return True

    def _on_new_connection(self) -> None:
        connection = self._server.nextPendingConnection() if self._server else None
        if connection is None:
            return
        # 客户端写完即断开:数据可能早于 readyRead 连接到达,断开也可能先于
        # 读信号出现,两条路径都触发(去重交给调用方,激活窗口幂等)
        def _activate() -> None:
            connection.readAll()
            connection.disconnectFromServer()
            self.raiseRequested.emit()

        if connection.bytesAvailable() > 0:
            _activate()
        else:
            connection.readyRead.connect(_activate)
            connection.disconnected.connect(_activate)

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
