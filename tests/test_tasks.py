from PySide6.QtWidgets import QDialog

from synthrecall.settings import LLMSettings
from synthrecall.tasks import (
    MAX_FINISHED_TASKS,
    TASK_CANCELLED,
    TASK_CANCELLING,
    TASK_FAILED,
    TaskManager,
)
from synthrecall.ui.external_request import (
    ExternalRequestDialog,
    confirm_external_request,
)
from synthrecall.ui.task_center import TaskCenterDialog


def test_task_manager_cancel_retry_and_task_center(qtbot):
    cancelled: list[bool] = []
    retried: list[bool] = []
    manager = TaskManager()
    task_id = manager.add(
        "Test task",
        detail="working",
        cancel=lambda: cancelled.append(True),
        retry=lambda: retried.append(True),
    )
    dialog = TaskCenterDialog(manager)
    qtbot.addWidget(dialog)

    assert manager.active_count() == 1
    assert dialog.table.rowCount() == 1
    assert manager.request_cancel(task_id)
    assert cancelled == [True]
    assert manager.get(task_id).state == TASK_CANCELLING

    manager.cancelled(task_id)
    assert manager.get(task_id).state == TASK_CANCELLED
    assert manager.active_count() == 0
    assert manager.retry(task_id)
    assert retried == [True]

    failed_id = manager.add("Failed task", retry=lambda: retried.append(False))
    manager.fail(failed_id, "network failure")
    assert manager.get(failed_id).state == TASK_FAILED
    assert manager.get(failed_id).error == "network failure"


def test_task_manager_releases_callbacks_and_bounds_finished_history(qtbot):
    manager = TaskManager()
    retained: list[str] = []

    succeeded_id = manager.add(
        "成功任务",
        cancel=lambda: retained.append("cancel"),
        retry=lambda: retained.append("retry"),
    )
    manager.succeed(succeeded_id)
    succeeded = manager._tasks[succeeded_id]
    assert succeeded.cancel_callback is None
    assert succeeded.retry_callback is None

    failed_id = manager.add(
        "失败任务",
        cancel=lambda: retained.append("cancel-failed"),
        retry=lambda: retained.append("retry-failed"),
    )
    manager.fail(failed_id, "失败")
    failed = manager._tasks[failed_id]
    assert failed.cancel_callback is None
    assert failed.retry_callback is not None
    assert manager.retry(failed_id) is True
    assert retained == ["retry-failed"]

    active_id = manager.add("长任务", cancel=lambda: None)
    for index in range(MAX_FINISHED_TASKS + 5):
        task_id = manager.add(f"历史任务 {index}")
        manager.succeed(task_id)

    records = manager.records()
    assert len(records) == MAX_FINISHED_TASKS + 1
    assert manager.get(active_id) is not None
    assert manager.get(failed_id) is None


def test_external_request_confirmation_can_be_disabled(tmp_path, qtbot, monkeypatch):
    settings_path = tmp_path / "llm_settings.json"
    LLMSettings(confirm_external_requests=True).save(settings_path)
    calls: list[str] = []

    def accept_and_remember(dialog):
        calls.append(dialog.windowTitle())
        dialog.remember_checkbox.setChecked(True)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ExternalRequestDialog, "exec", accept_and_remember)

    assert confirm_external_request(
        settings_path,
        operation="AI test",
        content_summary="fixed short text",
    )
    assert calls == ["确认外部调用"]
    assert not LLMSettings.load(settings_path).confirm_external_requests

    assert confirm_external_request(
        settings_path,
        operation="AI test again",
        content_summary="fixed short text",
    )
    assert calls == ["确认外部调用"]


def test_task_queue_runs_sequentially(qtbot):
    from synthrecall.tasks import TASK_QUEUED, TASK_RUNNING

    manager = TaskManager()
    started: list[str] = []

    def make_running_task(name: str) -> None:
        manager.add(name, detail="")

    manager.add("first", detail="")

    # 运行中入队两个任务:状态排队、不立即启动
    manager.enqueue("second", detail="b", start=lambda: make_running_task("second"))
    manager.enqueue("third", detail="c", start=lambda: make_running_task("third"))
    assert manager.pending_count() == 2
    # records() 新任务在前;队列语义按 id 升序(先入队先执行)
    queued = sorted(
        (r for r in manager.records() if r.state == TASK_QUEUED),
        key=lambda r: r.id,
    )
    assert [r.name for r in queued] == ["second", "third"]
    assert queued[0].can_cancel, "排队任务应可取消"

    # 第一个任务完成 → 队首自动启动(qtbot 驱动事件循环触发 singleShot)
    def running_id() -> int | None:
        recs = [r for r in manager.records() if r.state == TASK_RUNNING]
        return recs[0].id if recs else None

    manager.succeed(running_id())  # first
    qtbot.waitUntil(lambda: manager.active_count() == 1, timeout=2000)
    assert manager.pending_count() == 1
    running = [r for r in manager.records() if r.state == TASK_RUNNING]
    assert running and running[0].name == "second"

    # 第二个完成 → 第三个启动
    manager.succeed(running_id())
    qtbot.waitUntil(lambda: manager.pending_count() == 0, timeout=2000)
    assert manager.active_count() == 1
    running = [r for r in manager.records() if r.state == TASK_RUNNING]
    assert running and running[0].name == "third"

    # 收尾:结束最后一个任务
    manager.succeed(running_id())
    qtbot.waitUntil(lambda: manager.active_count() == 0, timeout=2000)

    # 排队任务可取消(从队列移除):先占住运行槽再入队
    manager.add("fifth", detail="")
    manager.enqueue("fourth", detail="", start=lambda: make_running_task("fourth"))
    assert manager.pending_count() == 1
    queued = sorted(
        (r for r in manager.records() if r.state == TASK_QUEUED), key=lambda r: r.id
    )
    assert manager.request_cancel(queued[0].id)
    assert manager.pending_count() == 0
    cancelled_record = manager.get(queued[0].id)
    assert cancelled_record is not None and cancelled_record.state == TASK_CANCELLED

    # 全部结束后恢复空闲(fifth 结束时队列已空,不会再启动任何任务)
    manager.succeed(
        next(r for r in manager.records() if r.state == TASK_RUNNING).id
    )
    qtbot.waitUntil(lambda: manager.active_count() == 0, timeout=2000)
    started.append("done")
    assert started == ["done"]


def test_task_center_shows_queued_state(qtbot):
    from synthrecall.tasks import TaskManager
    from synthrecall.ui.task_center import STATE_LABELS

    manager = TaskManager()
    manager.add("running", detail="")
    manager.enqueue("waiting", detail="", start=lambda: None)
    dialog = TaskCenterDialog(manager)
    qtbot.addWidget(dialog)
    states = [
        dialog.table.item(row, 1).text() for row in range(dialog.table.rowCount())
    ]
    assert "运行中" in states and "排队中" in states
    assert STATE_LABELS["queued"] == "排队中"
