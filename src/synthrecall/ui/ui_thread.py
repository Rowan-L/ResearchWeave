"""UI 线程调度助手:从任意后台线程把回调安全地派发回 Qt 主线程执行。

背景:QTimer.singleShot(0, fn) 在非 Qt 线程里调用时,0ms 定时器会挂在调用
线程(无 Qt 事件循环)上,回调被静默丢弃。信号发射才是跨线程安全的:
AutoConnection 下从工作线程 emit 会排队到接收者所在的主线程。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, Signal


class _UiThreadInvoker(QObject):
    _dispatch = Signal(object)

    def __init__(self) -> None:
        super().__init__(None)
        self._dispatch.connect(lambda fn: fn())


_invoker = _UiThreadInvoker()


def run_on_ui_thread(fn: Callable[[], None]) -> None:
    """把 fn 派发到 Qt 主线程执行;可在任意线程调用(含主线程,直接排队)。"""
    _invoker._dispatch.emit(fn)
