from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)

from PySide6.QtCore import QObject, QTimer, Signal

TASK_QUEUED = "queued"
TASK_RUNNING = "running"
TASK_CANCELLING = "cancelling"
TASK_SUCCEEDED = "succeeded"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"
MAX_FINISHED_TASKS = 200


@dataclass(frozen=True)
class TaskRecord:
    id: int
    name: str
    state: str
    detail: str
    error: str
    created_at: datetime
    finished_at: datetime | None
    can_cancel: bool
    can_retry: bool


@dataclass
class _MutableTask:
    id: int
    name: str
    state: str
    detail: str
    error: str
    created_at: datetime
    finished_at: datetime | None = None
    cancel_callback: Callable[[], None] | None = None
    retry_callback: Callable[[], None] | None = None
    start_callback: Callable[[], None] | None = None

    def snapshot(self) -> TaskRecord:
        return TaskRecord(
            id=self.id,
            name=self.name,
            state=self.state,
            detail=self.detail,
            error=self.error,
            created_at=self.created_at,
            finished_at=self.finished_at,
            can_cancel=self.cancel_callback is not None
            and self.state in {TASK_RUNNING, TASK_CANCELLING}
            or (self.state == TASK_QUEUED and self.start_callback is not None),
            can_retry=self.retry_callback is not None
            and self.state in {TASK_FAILED, TASK_CANCELLED},
        )


class TaskManager(QObject):
    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._next_id = 1
        self._tasks: dict[int, _MutableTask] = {}

    def add(
        self,
        name: str,
        *,
        detail: str = "",
        cancel: Callable[[], None] | None = None,
        retry: Callable[[], None] | None = None,
    ) -> int:
        task_id = self._next_id
        self._next_id += 1
        self._tasks[task_id] = _MutableTask(
            id=task_id,
            name=name,
            state=TASK_RUNNING,
            detail=detail,
            error="",
            created_at=utc_now(),
            cancel_callback=cancel,
            retry_callback=retry,
        )
        self.changed.emit()
        return task_id

    def enqueue(
        self,
        name: str,
        *,
        detail: str = "",
        start: Callable[[], None],
    ) -> int:
        """F16:任务队列——已有任务运行时新任务排队,空闲时由 _drain_queue 启动。"""
        task_id = self._next_id
        self._next_id += 1
        self._tasks[task_id] = _MutableTask(
            id=task_id,
            name=name,
            state=TASK_QUEUED,
            detail=detail,
            error="",
            created_at=utc_now(),
            start_callback=start,
        )
        self.changed.emit()
        self._drain_queue()
        return task_id

    def pending_count(self) -> int:
        return sum(task.state == TASK_QUEUED for task in self._tasks.values())

    def records(self) -> list[TaskRecord]:
        return [
            task.snapshot()
            for task in sorted(
                self._tasks.values(),
                key=lambda item: item.id,
                reverse=True,
            )
        ]

    def get(self, task_id: int | None) -> TaskRecord | None:
        if task_id is None:
            return None
        task = self._tasks.get(task_id)
        return task.snapshot() if task is not None else None

    def active_count(self) -> int:
        return sum(
            task.state in {TASK_RUNNING, TASK_CANCELLING}
            for task in self._tasks.values()
        )


    def succeed(self, task_id: int | None, detail: str = "") -> None:
        self._finish(task_id, TASK_SUCCEEDED, detail=detail)

    def fail(self, task_id: int | None, error: str) -> None:
        self._finish(task_id, TASK_FAILED, error=error)

    def cancelled(self, task_id: int | None, detail: str = "用户已取消") -> None:
        self._finish(task_id, TASK_CANCELLED, detail=detail)

    def request_cancel(self, task_id: int | None) -> bool:
        task = self._task(task_id)
        if task is None:
            return False
        if task.state == TASK_QUEUED:
            if task.start_callback is None:
                return False
            self.cancelled(task_id, detail="已从队列移除")
            return True
        if task.cancel_callback is None or task.state != TASK_RUNNING:
            return False
        task.state = TASK_CANCELLING
        task.detail = "正在取消；若已有网络请求，会在当前请求返回后停止。"
        self.changed.emit()
        task.cancel_callback()
        return True

    def retry(self, task_id: int | None) -> bool:
        task = self._task(task_id)
        if (
            task is None
            or task.retry_callback is None
            or task.state not in {TASK_FAILED, TASK_CANCELLED}
        ):
            return False
        task.retry_callback()
        return True

    def clear_finished(self) -> None:
        self._tasks = {
            task_id: task
            for task_id, task in self._tasks.items()
            if task.state in {TASK_QUEUED, TASK_RUNNING, TASK_CANCELLING}
        }
        self.changed.emit()

    def _finish(
        self,
        task_id: int | None,
        state: str,
        *,
        detail: str = "",
        error: str = "",
    ) -> None:
        task = self._task(task_id)
        if task is None:
            return
        task.state = state
        if detail:
            task.detail = detail
        task.error = error
        task.finished_at = utc_now()
        task.cancel_callback = None
        if state == TASK_SUCCEEDED:
            task.retry_callback = None
        self._trim_finished_history()
        self.changed.emit()
        # F16:任务结束后让出执行槽,启动队列中的下一个任务
        QTimer.singleShot(0, self._drain_queue)

    def _drain_queue(self) -> None:
        if self.active_count() > 0:
            return
        for task in sorted(self._tasks.values(), key=lambda item: item.id):
            if task.state == TASK_QUEUED and task.start_callback is not None:
                self._tasks.pop(task.id, None)
                self.changed.emit()
                # B35:启动回调异常不能悄悄吞掉执行槽——失败要可见,队列继续排空
                QTimer.singleShot(
                    0,
                    lambda cb=task.start_callback, name=task.name: self._safe_start(
                        cb, name
                    ),
                )
                return

    def _safe_start(self, start: Callable[[], None], name: str) -> None:
        try:
            start()
        except Exception as exc:  # noqa: BLE001 — 启动失败以失败任务呈现,不阻塞后续
            task_id = self._next_id
            self._next_id += 1
            self._tasks[task_id] = _MutableTask(
                id=task_id,
                name=name,
                state=TASK_FAILED,
                detail="队列启动失败",
                error=str(exc),
                created_at=utc_now(),
            )
            self.changed.emit()
            QTimer.singleShot(0, self._drain_queue)

    def _trim_finished_history(self) -> None:
        finished_ids = [
            task.id
            for task in self._tasks.values()
            if task.state not in {TASK_QUEUED, TASK_RUNNING, TASK_CANCELLING}
        ]
        overflow = len(finished_ids) - MAX_FINISHED_TASKS
        for task_id in sorted(finished_ids)[: max(0, overflow)]:
            self._tasks.pop(task_id, None)

    def _task(self, task_id: int | None) -> _MutableTask | None:
        if task_id is None:
            return None
        return self._tasks.get(task_id)
