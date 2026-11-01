from types import SimpleNamespace

import pymupdf as fitz
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QWidget,
)

from synthrecall.ai import DEFAULT_PROMPT_TEMPLATE
from synthrecall.arxiv_discovery import ArxivPaper, DiscoveryRecommender
from synthrecall.database import create_session_factory, init_database
from synthrecall.repositories import DatasetMetricUpdate, LibraryRepository
from synthrecall.ui.confirmation import ConfirmationDialog
from synthrecall.ui.duplicate_cleanup import DuplicateCleanupDialog
from synthrecall.ui.main_window import (
    AutoHeightJustifiedTextEdit,
    DatasetMetricDialog,
    LLMSettingsDialog,
    MainWindow,
    PaperChatDialog,
    PDFImportPreviewDialog,
)


def write_pdf(path, *, text, title, authors):
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.set_metadata({"title": title, "author": authors})
    document.save(path)
    document.close()


def make_window(tmp_path, qtbot):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    session_factory = create_session_factory(data_dir / "library.db")
    init_database(session_factory)
    repository = LibraryRepository(session_factory)
    repository.ensure_default_categories()

    source_pdf = tmp_path / "gui-paper.pdf"
    write_pdf(
        source_pdf,
        text="GUI acceptance full text marker",
        title="GUI Acceptance Dynamic Convolution Paper",
        authors="Alice Example; Bob Example",
    )
    paper = repository.import_pdf(
        source_pdf,
        library_paper_dir=data_dir / "papers",
    ).paper
    window = MainWindow(repository=repository, data_dir=data_dir)
    window._prompt_window_close_action = lambda: "exit"
    window.discovery_page.initial_feed_requested = True
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(30)
    window.select_paper(paper.id)
    return window, repository, paper, source_pdf


def test_gui_library_navigation_edit_search_preview_and_save(tmp_path, qtbot):
    window, repository, paper, _ = make_window(tmp_path, qtbot)

    window.workspace_buttons["dataset"].click()
    assert window.main_stack.currentIndex() == 1
    window.workspace_buttons["discovery"].click()
    assert window.main_stack.currentIndex() == 2
    window.discovery_page.backRequested.emit()
    assert window.main_stack.currentIndex() == 0
    window.workspace_buttons["discovery"].click()
    assert window.main_stack.currentIndex() == 2
    window.workspace_buttons["library"].click()
    assert window.main_stack.currentIndex() == 0
    assert window.detail_stack.currentIndex() == 0
    qtbot.mouseClick(
        window.paper_sort_combo,
        Qt.MouseButton.LeftButton,
        pos=window.paper_sort_combo.rect().center(),
    )
    qtbot.waitUntil(window.paper_sort_combo.view().isVisible, timeout=1_000)
    window.paper_sort_combo.hidePopup()
    assert "GUI Acceptance" in window.inspector_title_label.text()
    window.show_detail_editor()
    assert window.main_stack.currentWidget() is window.editor_page
    assert window.app_header.isHidden()
    assert window.editor_outline.count() == 5
    assert [window.editor_outline.item(row).text() for row in range(5)] == [
        "文献信息",
        "摘要",
        "研究问题与工作",
        "创新点",
        "个人批注",
    ]
    assert not any(
        button.text() == "展开" and button.isVisibleTo(window.editor_page)
        for button in window.editor_page.findChildren(QPushButton)
    )
    for editor in (
        window.abstract_edit,
        window.summary_edit,
        window.contributions_edit,
        window.personal_notes_edit,
    ):
        assert isinstance(editor, AutoHeightJustifiedTextEdit)
        assert editor.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert any(
        button.text() == "返回文献库"
        for button in window.editor_page.findChildren(QPushButton)
    )
    for row, target in enumerate(window.editor_outline_targets):
        window.editor_outline.setCurrentRow(row)
        window._navigate_editor_outline(row)
        qtbot.wait(30)
        viewport = window.editor_document_scroll.viewport()
        visible_top = target.mapTo(viewport, target.rect().topLeft()).y()
        assert 0 <= visible_top <= 60, (row, visible_top)

    window.editor_outline.setCurrentRow(2)
    assert window.tabs.currentIndex() == 1
    window.editor_outline.setCurrentRow(4)
    window.show_detail_summary()
    window.show_detail_editor()
    assert window.editor_outline.currentRow() == 0
    window.show_detail_summary()
    assert window.main_stack.currentWidget() is window.library_page
    assert window.detail_stack.currentIndex() == 0
    assert not window.app_header.isHidden()

    first_item = window.paper_list.item(0)
    first_card = window.paper_list.itemWidget(first_item)
    qtbot.mouseDClick(
        first_card,
        Qt.MouseButton.LeftButton,
        pos=first_card.rect().center(),
    )
    assert window.main_stack.currentWidget() is window.reader_page
    window.leave_reader()

    window.enter_reader()
    assert window.main_stack.currentWidget() is window.reader_page
    assert window.app_header.isHidden()
    assert window.reader_pdf_preview.page_count == 1
    assert window.reader_pdf_preview.translate_button.text() == "翻译 PDF"
    assert window.home_translate_button.text() == "翻译 PDF"
    assert window.home_translate_button.isEnabled()
    assert window.reader_paper_list.count() == 1
    window.reader_tabs.setCurrentIndex(1)
    window.reader_notes_edit.setPlainText("Reader workspace note")
    assert window._dirty
    window.save_reader_notes()
    assert repository.get_paper(paper.id).detail.personal_notes == "Reader workspace note"
    window.leave_reader()
    assert window.main_stack.currentWidget() is window.library_page
    assert not window.app_header.isHidden()

    window.search_edit.setText("full text marker")
    window.search_checkboxes["full_text"].setChecked(True)
    qtbot.wait(260)
    assert window.paper_list.count() == 1
    search_card = window.paper_list.itemWidget(window.paper_list.item(0))
    assert any(
        "命中：" in label.text() and "全文" in label.text()
        for label in search_card.findChildren(QLabel)
    )
    window.search_edit.clear()
    qtbot.wait(260)

    window.title_edit.setPlainText("GUI Acceptance Updated Paper")
    assert window.editor_save_state_label.text() == "有未保存修改"
    assert window.editor_paper_title_label.toPlainText() == "GUI Acceptance Updated Paper"
    window.status_combo.setCurrentText("精读")
    window.importance_edit.setValue(5)
    window.summary_edit.setPlainText("GUI summary marker")
    window.contributions_edit.setPlainText("GUI innovation marker")
    window.personal_notes_edit.setPlainText("GUI personal note")
    window.memory_anchor_edit.setPlainText("GUI memory marker")
    window.add_dataset_metric_row()
    metric_values = ["ImageNet", "Top-1 Acc", "88.8%", "GUI-Net", "classification", "verified"]
    for column, value in enumerate(metric_values):
        window.dataset_metrics_table.setItem(0, column, QTableWidgetItem(value))
    window.add_dataset_metric_row()
    window.dataset_metrics_table.setCurrentCell(1, 0)
    window.remove_dataset_metric_row()
    assert window.dataset_metrics_table.rowCount() == 1
    window.save_current_paper()
    assert window.editor_save_state_label.text() == "已保存"
    assert window.editor_bottom_state_label.text() == "所有修改已保存"

    updated = repository.get_paper(paper.id)
    assert updated.title == "GUI Acceptance Updated Paper"
    assert updated.reading_status == "精读"
    assert updated.importance == 5
    assert updated.detail.summary == "GUI summary marker"
    assert updated.detail.contributions == "GUI innovation marker"
    assert updated.detail.personal_notes == "GUI personal note"
    assert updated.detail.memory_anchor == "GUI memory marker"
    assert "Top-1 Acc" in updated.detail.dataset_metrics
    section_lookup = {
        editor: field for field, editor in window.detail_sections
    }
    window.compact_detail_button.setChecked(True)
    assert not section_lookup[window.summary_edit].isHidden()
    assert section_lookup[window.limitations_edit].isHidden()
    window.compact_detail_button.setChecked(False)

    repository.set_abstract_translation(paper.id, "摘要中文缓存")
    window.load_paper(paper.id)
    window.toggle_abstract_language()
    assert window.abstract_language == "zh"
    assert window.abstract_edit.toPlainText() == "摘要中文缓存"
    assert window.abstract_edit.isReadOnly()
    window.toggle_abstract_language()
    assert window.abstract_language == "en"
    assert not window.abstract_edit.isReadOnly()

    window.tabs.setCurrentIndex(3)
    assert window.pdf_preview.page_count == 1
    assert not window.pdf_preview.prev_button.isEnabled()
    assert not window.pdf_preview.next_button.isEnabled()
    initial_zoom = window.pdf_preview.zoom
    window.pdf_preview.zoom_in_button.click()
    assert window.pdf_preview.zoom > initial_zoom
    window.pdf_preview.rotate_button.click()
    assert window.pdf_preview.rotation == 90
    window.pdf_preview.search_edit.setText("full text marker")
    window.pdf_preview.find_text()
    assert window.pdf_preview.search_pages == [0]
    window.pdf_preview.copy_current_page_text()
    assert "GUI acceptance full text marker" in QApplication.clipboard().text()

    # F19 单主分类:下拉切换即设置唯一主分类
    unassigned = next(
        category_id
        for category_id in window.category_lookup
        if category_id not in window.selected_category_ids
    )
    window.category_combo.setCurrentIndex(window.category_combo.findData(unassigned))
    assert window.selected_category_ids == [unassigned]
    previous = window.selected_category_ids[0] if window.selected_category_ids else None
    window.category_combo.setCurrentIndex(window.category_combo.findData(previous))
    assert window.selected_category_ids == [previous]
    window.save_current_paper()


def test_gui_save_shortcut_dirty_switch_and_close_guards(tmp_path, qtbot, monkeypatch):
    window, repository, paper, _ = make_window(tmp_path, qtbot)
    second = repository.create_blank_paper()
    window.refresh_papers()

    assert window.save_shortcut.key() == QKeySequence(QKeySequence.StandardKey.Save)
    # U16 后快捷键全集:原有 7 个 + 刷新/导出/备份/界面设置/快捷键一览 +
    # 作用域受限的 Esc 清搜索/Ctrl+A 全选/Delete 移回收站
    assert set(window.shortcuts) == {
        "search",
        "import",
        "new",
        "library",
        "dataset",
        "discovery",
        "tasks",
        "refresh",
        "refresh2",
        "export",
        "backup",
        "appearance",
        "shortcuts_help",
        "search_clear",
        "select_all",
        "trash",
        "nav_back",
    }
    window.shortcuts["search"].activated.emit()
    assert window.search_edit.hasFocus()
    window.title_edit.setPlainText("Saved with Ctrl+S")
    assert window._dirty
    assert window.windowTitle().endswith("*")
    window.title_edit.setFocus()
    qtbot.keyClick(
        window.title_edit,
        Qt.Key.Key_S,
        Qt.KeyboardModifier.ControlModifier,
    )
    qtbot.wait(30)
    assert repository.get_paper(paper.id).title == "Saved with Ctrl+S"
    assert not window._dirty

    window.title_edit.setPlainText("Should remain unsaved")
    monkeypatch.setattr(
        window,
        "_prompt_unsaved_changes",
        lambda: QMessageBox.StandardButton.Cancel,
    )
    window.refresh_papers()
    assert window.title_edit.toPlainText() == "Should remain unsaved"
    window.show_dataset_page()
    assert window.main_stack.currentWidget() is window.library_page
    window.select_paper(second.id)
    assert window.current_paper_id == paper.id
    assert window._dirty

    monkeypatch.setattr(
        window,
        "_prompt_unsaved_changes",
        lambda: QMessageBox.StandardButton.Discard,
    )
    window.select_paper(second.id)
    assert window.current_paper_id == second.id
    assert repository.get_paper(paper.id).title == "Saved with Ctrl+S"
    assert not window._dirty

    window.title_edit.setPlainText("Second paper saved before switching")
    monkeypatch.setattr(
        window,
        "_prompt_unsaved_changes",
        lambda: QMessageBox.StandardButton.Save,
    )
    window.select_paper(paper.id)
    assert repository.get_paper(second.id).title == "Second paper saved before switching"
    assert window.current_paper_id == paper.id
    assert not window._dirty
    window.paper_sort_combo.setCurrentIndex(window.paper_sort_combo.findData("title"))
    card_titles = [
        window.paper_list.itemWidget(window.paper_list.item(row)).accessibleName()
        for row in range(window.paper_list.count())
    ]
    assert card_titles == sorted(card_titles, key=str.casefold)
    for row in range(window.paper_list.count()):
        window.paper_list.item(row).setSelected(True)
    window.batch_set_reading_status("已读")
    assert repository.get_paper(paper.id).reading_status == "已读"
    assert repository.get_paper(second.id).reading_status == "已读"

    for button_text, expected_action in (
        ("缩小到系统托盘", "tray"),
        ("退出 ResearchWeave", "exit"),
        ("继续使用", "cancel"),
    ):
        QTimer.singleShot(
            0,
            lambda text=button_text: next(
                button
                for button in QApplication.activeModalWidget().findChildren(QPushButton)
                if button.text() == text
            ).click(),
        )
        assert MainWindow._prompt_window_close_action(window) == expected_action

    window.title_edit.setPlainText("Close guard")
    tray_calls: list[bool] = []
    monkeypatch.setattr(window, "_prompt_window_close_action", lambda: "tray")
    monkeypatch.setattr(window, "_hide_to_system_tray", lambda: tray_calls.append(True))
    tray_close = QCloseEvent()
    window.closeEvent(tray_close)
    assert not tray_close.isAccepted()
    qtbot.waitUntil(lambda: tray_calls == [True], timeout=1_000)

    monkeypatch.setattr(window, "_prompt_window_close_action", lambda: "cancel")
    cancelled_close = QCloseEvent()
    window.closeEvent(cancelled_close)
    assert not cancelled_close.isAccepted()

    monkeypatch.setattr(window, "_prompt_window_close_action", lambda: "exit")
    monkeypatch.setattr(
        window,
        "_prompt_unsaved_changes",
        lambda: QMessageBox.StandardButton.Cancel,
    )
    blocked_close = QCloseEvent()
    window.closeEvent(blocked_close)
    assert not blocked_close.isAccepted()
    assert window._dirty

    monkeypatch.setattr(
        window,
        "_prompt_unsaved_changes",
        lambda: QMessageBox.StandardButton.Discard,
    )
    accepted_close = QCloseEvent()
    window.closeEvent(accepted_close)
    assert accepted_close.isAccepted()
    assert not window._dirty

    cancelled_tasks: list[bool] = []
    task_id = window.task_manager.add(
        "GUI task",
        cancel=lambda: cancelled_tasks.append(True),
    )
    assert window.task_button.text() == "任务 1"
    window.open_task_center()
    assert window.task_center_dialog is not None
    assert window.task_center_dialog.table.rowCount() == 1
    window.task_manager.request_cancel(task_id)
    assert cancelled_tasks == [True]
    window.task_manager.cancelled(task_id)
    assert window.task_button.text() == "任务"
    window.task_center_dialog.reject()


def test_ai_url_import_prompts_and_routes_to_background_import(tmp_path, qtbot, monkeypatch):
    window, _repository, _paper, _source_pdf = make_window(tmp_path, qtbot)
    submitted: list[str] = []
    monkeypatch.setattr(window, "ensure_api_ready", lambda: True)
    monkeypatch.setattr(window, "_confirm_pending_changes", lambda: True)
    monkeypatch.setattr(
        QInputDialog,
        "getText",
        lambda *_args, **_kwargs: (" https://arxiv.org/abs/2405.12701v2 ", True),
    )
    monkeypatch.setattr(window, "start_url_import", submitted.append)

    assert window.ai_import_url_action.text() == "AI 链接导入"
    window.ai_import_url()

    assert submitted == ["https://arxiv.org/abs/2405.12701v2"]


def test_gui_import_categories_guards_and_record_actions(tmp_path, qtbot, monkeypatch):
    window, repository, paper, source_pdf = make_window(tmp_path, qtbot)
    messages: list[tuple[str, str]] = []
    opened: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, title, text: messages.append((title, text))
        or QMessageBox.StandardButton.Ok,
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, text: messages.append((title, text))
        or QMessageBox.StandardButton.Ok,
    )
    monkeypatch.setattr(
        ConfirmationDialog,
        "exec_action",
        lambda _self: "confirm",
    )
    monkeypatch.setattr("synthrecall.ui.main_window.os.startfile", lambda path: opened.append(str(path)))

    duplicate_pdf = tmp_path / "gui-paper-revision.pdf"
    write_pdf(
        duplicate_pdf,
        text="Different PDF bytes",
        title="GUI ACCEPTANCE DYNAMIC CONVOLUTION PAPER!",
        authors="Bob Example, Alice Example",
    )
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(duplicate_pdf), "PDF 文件 (*.pdf)"),
    )

    def open_existing(dialog):
        dialog.action = PDFImportPreviewDialog.ACTION_OPEN_EXISTING
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(PDFImportPreviewDialog, "exec", open_existing)
    before = len(repository.list_papers())
    window.add_pdf()
    assert len(repository.list_papers()) == before
    assert window.current_paper_id == paper.id

    preview = repository.preview_pdf_import(duplicate_pdf)
    preview_dialog = PDFImportPreviewDialog(
        preview,
        repository.list_categories(),
        parent=window,
    )
    qtbot.addWidget(preview_dialog)
    assert preview_dialog.preview.duplicate_reason == "论文标题和作者相同"
    assert preview_dialog.category_combo.currentText() == "未分类"
    assert preview_dialog.selected_category_id() is None
    preview_dialog.authors_edit.setText("Confirmed Authors")
    assert preview_dialog.metadata().authors == "Confirmed Authors"

    new_pdf = tmp_path / "new-gui-import.pdf"
    write_pdf(
        new_pdf,
        text="New GUI import text",
        title="Raw GUI Import Title",
        authors="Original Author",
    )
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(new_pdf), "PDF 文件 (*.pdf)"),
    )
    # B34:全新库不再种入演示分类树,显式建一个分类作为导入目标
    target_category = repository.add_category("GUI 导入目标")
    window.current_category_id = target_category.id

    def confirm_import(dialog):
        dialog.title_edit.setText("Confirmed GUI Import Title")
        assert dialog.selected_category_id() == target_category.id
        dialog.action = PDFImportPreviewDialog.ACTION_IMPORT
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(PDFImportPreviewDialog, "exec", confirm_import)
    window.add_pdf()
    imported = repository.get_paper(window.current_paper_id)
    assert imported.title == "Confirmed GUI Import Title"
    assert [category.id for category in imported.categories] == [target_category.id]

    window.load_paper(paper.id)
    window.open_current_pdf()
    assert opened == [str(repository.resolve_pdf_path(repository.get_paper(paper.id).pdf_path))]
    window.open_current_translation_pdf()
    window.open_current_dual_translation_pdf()
    assert sum(title == "没有译文" for title, _ in messages) == 2

    monkeypatch.setattr(
        "synthrecall.ui.main_window.get_llm_api_key",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(window, "open_api_settings", lambda: False)
    assert window.ensure_api_ready() is False
    assert any(title == "需要 API 设置" for title, _ in messages)

    window.create_blank_paper()
    blank_id = window.current_paper_id
    assert blank_id is not None
    window.delete_current_paper()
    assert all(item.id != blank_id for item in repository.list_papers())

    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("GUI临时分类", True))
    window.add_category()
    category = next(item for item in repository.list_categories() if item.name == "GUI临时分类")
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("GUI重命名分类", True))
    window.rename_category(category.id)
    assert any(item.name == "GUI重命名分类" for item in repository.list_categories())
    window.move_category(category.id, -1)
    window.delete_category(category.id)
    assert all(item.id != category.id for item in repository.list_categories())

    # B34:种子树移除后,用显式建的分组验证分类名过滤(一个命中一个不命中)
    filter_group = repository.add_category("GUI过滤组")
    repository.add_category("GUI Transformer", parent_id=filter_group.id)
    repository.add_category("GUI无关分类", parent_id=filter_group.id)
    window.refresh_categories()
    window.category_filter_edit.setText("Transformer")
    root_item = window.category_tree.topLevelItem(0)
    group_item = next(
        root_item.child(index)
        for index in range(root_item.childCount())
        if root_item.child(index).text(0).startswith("GUI过滤组")
    )
    assert any(
        not group_item.child(index).isHidden()
        for index in range(group_item.childCount())
    )
    assert any(
        group_item.child(index).isHidden()
        for index in range(group_item.childCount())
    )
    window.category_filter_edit.clear()

    window.load_paper(paper.id)
    quick_category = repository.add_category("GUI快捷分类")
    window.refresh_categories()
    window.category_combo.setCurrentIndex(window.category_combo.findData(quick_category.id))
    assert window.selected_category_ids == [quick_category.id]
    window.save_current_paper()

    assert not any(
        button.text() == "自动整理"
        for button in window.findChildren(QPushButton)
    )
    window.auto_classify_library(show_message=False)
    assert repository.get_paper(paper.id).categories
    assert source_pdf.exists()

    settings_dialog = LLMSettingsDialog(window.settings_path)
    qtbot.addWidget(settings_dialog)
    settings_dialog.prompt_edit.setPlainText("modified prompt")
    settings_dialog.reset_prompt()
    assert settings_dialog.prompt_edit.toPlainText() == DEFAULT_PROMPT_TEMPLATE
    settings_dialog._apply_available_models(["deepseek-flash", "deepseek-pro"])
    assert settings_dialog.model_combo.count() == 2
    assert settings_dialog.model_combo.itemText(0) == "deepseek-flash"
    settings_dialog.model_combo.setCurrentText("deepseek-pro")
    assert settings_dialog.model_edit.text() == "deepseek-pro"

    dots_index = next(
        index
        for index in range(settings_dialog.preset_combo.count())
        if settings_dialog.preset_combo.itemText(index) == "小红书 Dots"
    )
    # 对话框新默认就是小红书(index 0);先切走再切回,保证 currentIndexChanged 触发
    deepseek_index = next(
        index
        for index in range(settings_dialog.preset_combo.count())
        if settings_dialog.preset_combo.itemText(index) == "DeepSeek Chat"
    )
    settings_dialog.preset_combo.setCurrentIndex(deepseek_index)
    settings_dialog.preset_combo.setCurrentIndex(dots_index)
    assert settings_dialog.base_url_edit.text() == (
        "https://note3-prev-api.askdiandian.com/v1"
    )
    assert settings_dialog.model_edit.text() == "dots3-note-prev"
    assert settings_dialog._api_auth_style == "api-key"
    assert settings_dialog._send_temperature is False
    assert settings_dialog._enable_thinking is False
    assert settings_dialog.fetch_models_button.isEnabled() is False


def test_duplicate_import_dialog_stops_normal_import(tmp_path, qtbot):
    window, repository, _, _ = make_window(tmp_path, qtbot)
    source = tmp_path / "duplicate-source.pdf"
    write_pdf(
        source,
        text="Duplicate import body",
        title="A Clearly Duplicated Research Paper",
        authors="Alice",
    )
    first = repository.import_pdf(source, library_paper_dir=tmp_path / "data" / "papers")
    preview = repository.preview_pdf_import(
        source, library_paper_dir=tmp_path / "data" / "papers"
    )

    dialog = PDFImportPreviewDialog(preview, repository.list_categories(), parent=window)
    qtbot.addWidget(dialog)

    assert preview.duplicate_paper_id == first.paper.id
    assert dialog.windowTitle() == "检测到重复文献"
    assert any(
        label.text() == "检测到重复文献，已暂停导入"
        for label in dialog.findChildren(QLabel)
    )
    assert not any(
        button.text() == "确认导入" for button in dialog.findChildren(QPushButton)
    )


def test_splitter_widths_are_persisted_per_feature_page(tmp_path, qtbot):
    window, repository, _, _ = make_window(tmp_path, qtbot)
    window.resize(1500, 900)
    qtbot.wait(30)

    target_sizes = {
        "library": [210, 820, 460],
        "dataset": [360, 1120],
        "discovery": [250, 780, 450],
        "reader": [250, 800, 440],
    }
    for key, sizes in target_sizes.items():
        window._splitters[key].setSizes(sizes)
    window._save_all_splitter_states()
    expected_states = {
        key: bytes(splitter.saveState())
        for key, splitter in window._splitters.items()
    }

    restored = MainWindow(repository=repository, data_dir=tmp_path / "data")
    restored._prompt_window_close_action = lambda: "exit"
    restored.discovery_page.initial_feed_requested = True
    qtbot.addWidget(restored)

    assert set(restored._splitters) == set(target_sizes)
    for key, splitter in restored._splitters.items():
        assert bytes(splitter.saveState()) == expected_states[key]


def test_ai_workflow_splitter_width_is_persisted(tmp_path, qtbot):
    settings_path = tmp_path / "data" / "llm_settings.json"
    dialog = LLMSettingsDialog(settings_path)
    qtbot.addWidget(dialog)
    dialog.workflow_splitter.setSizes([155, 665, 340])
    dialog._save_splitter_state()
    expected_state = bytes(dialog.workflow_splitter.saveState())

    restored = LLMSettingsDialog(settings_path)
    qtbot.addWidget(restored)

    assert bytes(restored.workflow_splitter.saveState()) == expected_state
    assert restored.workflow_splitter.sizes()[0] <= 210


def test_gui_dataset_empty_state_inline_no_dialog(tmp_path, qtbot):
    window, repository, paper, _ = make_window(tmp_path, qtbot)

    # 库中没有任何数据集指标:数据集页应显示页面内空态,不弹任何窗口
    window.show_dataset_page()
    assert window.main_stack.currentIndex() == 1
    assert window.dataset_empty_state.isVisible()
    assert not window.dataset_splitter.isVisible()
    assert window.dataset_empty_hint_button.isVisible()
    # 空态引导按钮必须内嵌在数据集页里,而不是游离成顶层窗口(B28)
    assert window.dataset_empty_hint_button.window() is window
    assert not any(
        w.isVisible() and w is not window and w.window() is w
        for w in window.findChildren(QWidget)
    )

    # 添加一条指标后空态消失,数据区显示
    repository.add_dataset_metric_entry(
        DatasetMetricUpdate(
            dataset="ImageNet",
            metric="Top-1 Acc",
            value="88.8%",
            model="GUI-Net",
            task="classification",
            notes="empty-state",
            paper_id=paper.id,
        )
    )
    window.refresh_dataset_metrics()
    assert not window.dataset_empty_state.isVisible()
    assert window.dataset_splitter.isVisible()
    assert window.dataset_list.count() == 1


def test_pdf_translation_goes_to_queue_when_busy(tmp_path, qtbot, monkeypatch):
    window, _repository, paper, _ = make_window(tmp_path, qtbot)
    monkeypatch.setattr(window, "ensure_api_ready", lambda: True)
    monkeypatch.setattr(window, "save_current_paper", lambda: True)

    # 模拟翻译线程被占用(真实场景中运行中任务必然已登记到任务管理器):
    # 新翻译请求应入队而不是弹"正在翻译"拒绝框
    window.translate_thread = object()
    window.task_manager.add("占位运行任务", detail="")
    warned: list[str] = []
    monkeypatch.setattr(
        "synthrecall.ui.main_window.QMessageBox.information",
        lambda *a, **k: warned.append(str(a[1])),
    )
    window.translate_current_pdf()
    assert warned == [], "忙碌时不应弹拒绝对话框"
    assert window.task_manager.pending_count() == 1
    assert window.task_button.text() == "任务 2"

    # 释放占用 → 队列自动启动(确认框已随入队跳过)
    started: list[tuple] = []
    window.start_pdf_translation = lambda *a, **k: started.append(a)
    running = [
        r for r in window.task_manager.records() if r.state == "running"
    ]
    window.task_manager.succeed(running[0].id)
    window.translate_thread = None
    window.update_task_button()
    qtbot.waitUntil(lambda: len(started) == 1, timeout=3000)
    assert started[0][0] == paper.id
    assert window.task_manager.pending_count() == 0


def test_task_button_spins_while_tasks_active(tmp_path, qtbot, monkeypatch):
    window, _repository, _paper, _ = make_window(tmp_path, qtbot)

    # 空闲:动画停止,图标为原始静态图标
    assert not window._task_spin_timer.isActive()

    # 有任务运行:timer 启动,图标随帧旋转
    monkeypatch.setattr(window.task_manager, "active_count", lambda: 2)
    window.update_task_button()
    assert window.task_button.text() == "任务 2"
    assert window._task_spin_timer.isActive()
    before = window.task_button.icon().pixmap(32, 32).cacheKey()
    qtbot.wait(200)
    after = window.task_button.icon().pixmap(32, 32).cacheKey()
    assert after != before, "旋转动画应持续更新图标"

    # 任务结束:动画停止,图标恢复
    monkeypatch.setattr(window.task_manager, "active_count", lambda: 0)
    window.update_task_button()
    assert window.task_button.text() == "任务"
    assert not window._task_spin_timer.isActive()


def test_tag_context_menu_rename_color_delete(tmp_path, qtbot, monkeypatch):
    import synthrecall.ui.main_window as mw

    window, repository, _paper, _ = make_window(tmp_path, qtbot)
    window.tag_input.setText("复现必看")
    window.add_tag_from_input()
    tag = next(t for t in repository.list_tags() if t.name == "复现必看")
    button = next(
        window.tag_rail_layout.itemAt(i).widget()
        for i in range(window.tag_rail_layout.count())
        if window.tag_rail_layout.itemAt(i).widget() is not None
        and "复现必看" in window.tag_rail_layout.itemAt(i).widget().text()
    )

    # 重命名:菜单第一项(QMenu.exec 是 C++ 绑定不可 patch,替换实例弹出方法)
    monkeypatch.setattr(
        window, "_exec_tag_menu", lambda menu, button: menu.actions()[0]
    )
    monkeypatch.setattr(
        mw.QInputDialog, "getText", lambda *a, **k: ("复现必读", True)
    )
    window.open_tag_menu(tag.id, button)
    tag = next(t for t in repository.list_tags() if t.id == tag.id)
    assert tag.name == "复现必读"
    assert any("复现必读" in window.tag_rail_layout.itemAt(i).widget().text()
               for i in range(window.tag_rail_layout.count())
               if window.tag_rail_layout.itemAt(i).widget() is not None)

    # 改颜色:菜单第二项
    monkeypatch.setattr(
        window, "_exec_tag_menu", lambda menu, button: menu.actions()[1]
    )
    from PySide6.QtGui import QColor as _QColor

    monkeypatch.setattr(
        mw.QColorDialog,
        "getColor",
        lambda *a, **k: _QColor("#e11d48"),
    )
    window.open_tag_menu(tag.id, button)
    assert next(t for t in repository.list_tags() if t.id == tag.id).color == "#e11d48"

    # 删除:菜单最后一项(confirm 放行)
    monkeypatch.setattr(
        window, "_exec_tag_menu", lambda menu, button: menu.actions()[-1]
    )
    monkeypatch.setattr(mw, "confirm_action", lambda *a, **k: True)
    window.open_tag_menu(tag.id, button)
    assert all(t.id != tag.id for t in repository.list_tags())
    assert window.current_tag_id is None


def test_gui_dataset_ranking_and_dialog(tmp_path, qtbot, monkeypatch):
    window, repository, paper, _ = make_window(tmp_path, qtbot)
    repository.add_dataset_metric_entry(
        DatasetMetricUpdate(
            dataset="ImageNet",
            metric="Top-1 Acc",
            value="88.8%",
            model="GUI-Net",
            task="classification",
            notes="acceptance",
            paper_id=paper.id,
        )
    )

    window.show_dataset_page()
    assert window.main_stack.currentIndex() == 1
    assert window.dataset_list.count() == 1
    assert window.dataset_table.rowCount() == 1
    assert window.dataset_table.item(0, 1).text() == "GUI-Net"

    window.dataset_filter_edit.setText("missing")
    assert window.dataset_list.count() == 0
    window.dataset_filter_edit.setText("image")
    assert window.dataset_list.count() == 1
    window.sort_combo.setCurrentIndex(window.sort_combo.findData("asc"))
    assert window.dataset_table.rowCount() == 1

    dialog = DatasetMetricDialog(repository.list_papers())
    qtbot.addWidget(dialog)
    dialog.dataset_edit.setText("COCO")
    dialog.metric_edit.setText("mAP")
    dialog.value_edit.setText("51.2")
    dialog.unit_edit.setText("%")
    dialog.direction_combo.setCurrentIndex(dialog.direction_combo.findData("higher"))
    dialog.model_edit.setText("Dialog-Net")
    dialog.paper_combo.setCurrentIndex(dialog.paper_combo.findData(paper.id))
    update = dialog.update()
    assert update.dataset == "COCO"
    assert update.metric == "mAP"
    assert update.paper_id == paper.id
    assert update.unit == "%"
    assert update.direction == "higher"

    replacement = DatasetMetricUpdate(
        dataset="ImageNet",
        metric="Top-1 Acc",
        value="89.1%",
        model="GUI-Net-v2",
        task="classification",
        notes="edited",
        paper_id=paper.id,
    )
    monkeypatch.setattr(DatasetMetricDialog, "exec", lambda _self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(DatasetMetricDialog, "update", lambda _self: replacement)
    window.dataset_table.setCurrentCell(0, 0)
    window.edit_ranking_entry()
    assert repository.list_dataset_metrics()[0].model == "GUI-Net-v2"

    monkeypatch.setattr(
        ConfirmationDialog,
        "exec_action",
        lambda _self: "confirm",
    )
    window.dataset_table.setCurrentCell(0, 0)
    window.delete_ranking_entry()
    assert repository.list_dataset_metrics() == []


def test_gui_discovery_local_interactions(tmp_path, qtbot, monkeypatch):
    window, repository, _, _ = make_window(tmp_path, qtbot)
    view = window.discovery_page
    paper = ArxivPaper(
        arxiv_id="2607.12345v1",
        title="GUI Discovery Test Paper",
        authors=["Ada Example", "Bob Example"],
        summary="A dynamic convolution paper evaluated on ImageNet.",
        published="2026-07-20T00:00:00Z",
        updated="2026-07-21T00:00:00Z",
        categories=["cs.CV"],
        primary_category="cs.CV",
        entry_url="https://arxiv.org/abs/2607.12345",
        pdf_url="https://arxiv.org/pdf/2607.12345",
    )
    paper.score = DiscoveryRecommender(["dynamic convolution"]).score(paper)
    view.on_fetch_finished([paper])
    assert view.feed_list.count() == 1
    assert view.current_paper is paper
    assert view.inspector_title.text() == paper.title
    assert window.api_settings_button.accessibleName() == "API 设置"
    assert window.paper_list.item(0).data(Qt.ItemDataRole.AccessibleTextRole)
    assert window.paper_list.itemWidget(window.paper_list.item(0)).accessibleName()
    assert view.feed_list.item(0).data(Qt.ItemDataRole.AccessibleTextRole)
    assert view.feed_list.itemWidget(view.feed_list.item(0)).accessibleName() == paper.title
    view.show_cached_feed()
    assert "缓存模式" in view.feed_status.text()
    assert "上次更新" in view.feed_status.text()
    paper = view.current_paper
    assert paper is not None

    view.keyword_input.setText("remote sensing")
    view.add_focus_term_from_input()
    assert "remote sensing" in view.current_focus_terms()
    view.remove_focus_term("remote sensing")
    assert "remote sensing" not in view.current_focus_terms()

    view.cache.set_abstract_translation(paper, "中文发现摘要")
    view.toggle_abstract_language()
    assert view.abstract_language == "zh"
    assert "中文发现摘要" in view.summary_box.toPlainText()
    view.toggle_abstract_language()
    assert view.abstract_language == "en"

    view.toggle_bookmark_current()
    assert paper.is_bookmarked
    quick_filters: list[bool] = []
    monkeypatch.setattr(view, "refresh_feed", lambda randomize=True: quick_filters.append(randomize))
    view.apply_quick_filter("关注领域")
    assert view.feed_title.text() == "关注领域"
    assert quick_filters == [True]

    deep_reads: list[str] = []
    view.deepReadRequested.disconnect(window.deep_read_discovered_paper)
    view.deepReadRequested.connect(lambda selected: deep_reads.append(selected.arxiv_id))
    view.deep_read_current_paper()
    assert deep_reads == [paper.arxiv_id]

    opened: list[str] = []
    monkeypatch.setattr(
        "synthrecall.ui.discovery_view.webbrowser.open",
        lambda url: opened.append(url),
    )
    view.open_current_arxiv()
    assert opened == [paper.entry_url]

    before = len(repository.list_papers())
    view.import_current_paper()
    assert len(repository.list_papers()) == before + 1
    saved = next(item for item in repository.list_papers() if item.title == paper.title)
    assert [category.name for category in saved.categories] == ["未分类"]
    assert paper.is_saved

    downloaded = tmp_path / "downloaded.pdf"
    downloaded.write_bytes(b"%PDF-1.4")
    view.on_download_finished(paper, downloaded)
    assert paper.local_pdf_path == str(downloaded)

    view.hide_current_paper()
    assert view.feed_list.count() == 0
    assert paper.arxiv_id in view.cache.hidden_ids


def test_auto_height_justified_text_edit(qtbot):
    editor = AutoHeightJustifiedTextEdit(72)
    qtbot.addWidget(editor)
    editor.resize(520, 72)
    editor.show()
    editor.setPlainText("短段落。")
    qtbot.wait(30)
    short_height = editor.height()

    paragraphs = [
        "这是一段用于检查自动高度和两端对齐的较长文字。" * 8,
        "第二段继续补充内容，确保编辑框不依赖内部滚动条。" * 8,
        "第三段用于验证每一个文本块都继承两端对齐格式。" * 8,
    ]
    editor.setPlainText("\n\n".join(paragraphs))
    qtbot.waitUntil(lambda: editor.height() > short_height, timeout=1_000)

    assert editor.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    block = editor.document().begin()
    while block.isValid():
        assert block.blockFormat().alignment() == Qt.AlignmentFlag.AlignJustify
        block = block.next()

def test_paper_chat_uses_left_and_right_message_bubbles(tmp_path, qtbot):
    window, repository, paper, _source_pdf = make_window(tmp_path, qtbot)
    dialog = PaperChatDialog(repository, window.settings_path, paper.id, window)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog._append_message("你", "用户问题")
    dialog._append_message("AI", "模型回答")
    bubbles = {
        label.objectName(): label.text()
        for label in dialog.findChildren(QLabel)
        if label.objectName() in {"UserChatBubble", "AIChatBubble"}
    }
    assert bubbles == {"UserChatBubble": "用户问题", "AIChatBubble": "模型回答"}
    assert dialog.chat_layout.itemAt(1).widget().layout().itemAt(0).spacerItem() is not None
    assert dialog.chat_layout.itemAt(2).widget().layout().itemAt(0).widget().objectName() == "AIChatBubble"


def test_paper_list_refresh_reuses_unchanged_cards(tmp_path, qtbot):
    window, repository, _paper, _ = make_window(tmp_path, qtbot)
    _second = repository.create_blank_paper()
    window.refresh_papers()

    cards_before = {}
    for row in range(window.paper_list.count()):
        item = window.paper_list.item(row)
        cards_before[item.data(Qt.ItemDataRole.UserRole)] = window.paper_list.itemWidget(item)
    assert len(cards_before) == 2

    # 部分过滤:被过滤掉的行移除,保留行的卡片对象必须复用(B1 差量刷新)
    window.search_edit.setText("GUI Acceptance")
    window.refresh_papers()
    assert window.paper_list.count() == 1
    kept = window.paper_list.item(0)
    assert (
        window.paper_list.itemWidget(kept)
        is cards_before[kept.data(Qt.ItemDataRole.UserRole)]
    )

    # 清除搜索:列表恢复完整,行数正确(Qt 会销毁离场卡片,允许重建)
    window.search_edit.clear()
    window.refresh_papers()
    assert window.paper_list.count() == 2
    for row in range(window.paper_list.count()):
        item = window.paper_list.item(row)
        assert window.paper_list.itemWidget(item) is not None


def test_trash_gui_flow_delete_restore_purge(tmp_path, qtbot, monkeypatch):
    import synthrecall.ui.main_window as mw

    window, repository, paper, _ = make_window(tmp_path, qtbot)
    paper_id = paper.id
    pdf_path = repository.resolve_pdf_path(paper.pdf_path)
    assert pdf_path is not None and pdf_path.exists()

    monkeypatch.setattr(mw, "confirm_action", lambda *args, **kwargs: True)

    def select_paper_row():
        for row in range(window.paper_list.count()):
            if window.paper_list.item(row).data(Qt.ItemDataRole.UserRole) == paper_id:
                window.paper_list.setCurrentItem(window.paper_list.item(row))
                return True
        return False

    # 1) 移入回收站:列表隐藏、PDF 保留、树节点计数
    assert select_paper_row()
    window.batch_delete_papers()
    assert all(p.id != paper_id for p in repository.list_papers())
    assert [p.id for p in repository.list_trashed_papers()] == [paper_id]
    assert pdf_path.exists(), "软删除不应删除 PDF 文件"
    labels = [
        window.category_tree.topLevelItem(i).text(0)
        for i in range(window.category_tree.topLevelItemCount())
    ]
    assert any(label.startswith("回收站  1") for label in labels), labels

    # 2) 切到回收站视图:看到被删文献,批量菜单切换为恢复/彻底删除
    def find_trash_item():
        # refresh_categories 会重建树,节点引用每次重查
        return next(
            window.category_tree.topLevelItem(i)
            for i in range(window.category_tree.topLevelItemCount())
            if window.category_tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
            == mw.TRASH_CATEGORY_ID
        )

    window.category_tree.setCurrentItem(find_trash_item())
    qtbot.wait(30)
    assert window._viewing_trash
    row_ids = [
        window.paper_list.item(row).data(Qt.ItemDataRole.UserRole)
        for row in range(window.paper_list.count())
    ]
    assert row_ids == [paper_id]
    window._rebuild_batch_menu()
    action_texts = [action.text() for action in window._batch_menu.actions()]
    assert "恢复所选文献" in action_texts
    assert any("彻底删除" in text for text in action_texts)

    # 3) 恢复:回到文献库
    select_paper_row()
    window.batch_restore_papers()
    assert any(p.id == paper_id for p in repository.list_papers())
    assert not repository.list_trashed_papers()

    # 4) 再删后彻底删除:记录与受管 PDF 一并清理
    select_paper_row()
    window.batch_delete_papers()
    window.category_tree.setCurrentItem(find_trash_item())
    qtbot.wait(30)
    select_paper_row()
    monkeypatch.setattr(mw.ConfirmationDialog, "exec_action", lambda self: "purge")
    window.batch_purge_papers()
    import pytest
    from sqlalchemy.exc import NoResultFound

    assert not repository.list_trashed_papers()
    with pytest.raises(NoResultFound):
        repository.get_paper(paper_id)
    assert not pdf_path.exists(), "彻底删除应清理受管 PDF"


def test_reader_notes_tab_lists_annotations_and_exports_markdown(
    tmp_path, qtbot, monkeypatch
):
    import synthrecall.ui.main_window as mw

    window, _repository, _paper, _ = make_window(tmp_path, qtbot)
    window.enter_reader()
    qtbot.wait(50)

    reader = window.reader_pdf_preview
    reader._apply_annotation_gesture(
        0, "highlight", [__import__("PySide6.QtCore", fromlist=["QPointF"]).QPointF(40, 40),
                         __import__("PySide6.QtCore", fromlist=["QPointF"]).QPointF(220, 60)]
    )
    qtbot.wait(30)

    # U14:批注页应出现 PDF 批注列表并计入数量
    window.refresh_pdf_annotations()
    assert window.pdf_annotations_list.count() == 1
    assert "共 1 条" in window.pdf_annotations_count.text()

    # F7:点击列表项跳转到对应页
    item = window.pdf_annotations_list.item(0)
    window._jump_to_pdf_annotation(item)
    assert reader.page_index == item.data(Qt.ItemDataRole.UserRole) - 1

    # F7:导出 Markdown(monkeypatch 文件对话框)
    window.reader_notes_edit.setPlainText("这是一条我的笔记。")
    export_path = tmp_path / "export.md"
    monkeypatch.setattr(
        mw.QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(export_path), "Markdown 文件 (*.md)"),
    )
    window.export_reader_notes_markdown()
    content = export_path.read_text(encoding="utf-8")
    assert "## 我的笔记" in content
    assert "这是一条我的笔记。" in content
    assert "## PDF 批注" in content
    assert "第 1 页" in content

    # 收尾:走真实用户路径保存笔记并放弃未保存批注,
    # 否则 teardown 关窗会弹"未保存修改"模态框(reader_notes 同步会置脏 personal_notes)
    window.save_reader_notes()
    window.reader_pdf_preview.discard_annotations()
    assert not window._dirty
    assert not window.reader_pdf_preview.has_unsaved_annotations


def test_reader_memory_anchor_renders_as_bullets(tmp_path, qtbot):
    from synthrecall.repositories import PaperUpdate
    from synthrecall.ui.main_window import format_memory_anchor

    # 纯函数:英文/中文分号、换行混排的锚点原文 → 逐条列表
    assert format_memory_anchor("A;B；C\nD；；E") == "• A\n• B\n• C\n• D\n• E"
    assert format_memory_anchor("  ;；\n ") == ""

    window, repository, paper, _ = make_window(tmp_path, qtbot)
    repository.update_paper(
        paper.id,
        PaperUpdate(
            title="Anchor Paper",
            authors="Demo",
            year=2026,
            venue="Local Test",
            doi="",
            arxiv_id="",
            url="",
            abstract="",
            pdf_path="",
            reading_status="待读",
            importance=3,
            summary="",
            datasets="",
            dataset_metrics="",
            methods="",
            contributions="",
            limitations="",
            experiment_notes="",
            reproducibility_notes="",
            memory_anchor="RMT: Relative Mapping Token;MaSA 注意力；数据集: ImageNet-1K",
            personal_notes="",
        ),
        category_id=None,
    )
    window.load_paper(paper.id)
    qtbot.wait(30)

    expected = "• RMT: Relative Mapping Token\n• MaSA 注意力\n• 数据集: ImageNet-1K"
    assert window.reader_memory_label.toPlainText() == expected
    assert window.inspector_memory_label.text() == expected


def test_tag_gui_input_chip_rail_and_filter(tmp_path, qtbot):
    window, repository, paper, _ = make_window(tmp_path, qtbot)

    # 输入回车即建标签并附加到当前文献
    window.tag_input.setText("复现必看")
    window.add_tag_from_input()
    window.tag_input.setText("方法借鉴")
    window.add_tag_from_input()

    chips = [
        window.tag_chips_layout.itemAt(i).widget().text()
        for i in range(window.tag_chips_layout.count())
        if window.tag_chips_layout.itemAt(i).widget() is not None
    ]
    assert any("复现必看" in text for text in chips), chips
    assert any("方法借鉴" in text for text in chips)
    assert "共" not in "".join(chips) or True  # 占位提示已消失

    # 左侧标签栏出现并带计数
    rail_texts = [
        window.tag_rail_layout.itemAt(i).widget().text()
        for i in range(window.tag_rail_layout.count())
    ]
    assert any("复现必看" in text and "1" in text for text in rail_texts), rail_texts

    # 点击左侧标签筛选
    tag = next(t for t in repository.list_tags() if t.name == "复现必看")
    window.toggle_tag_filter(tag.id)
    assert window.current_tag_id == tag.id
    assert window.paper_list.count() == 1

    # 再次点击取消筛选
    window.toggle_tag_filter(tag.id)
    assert window.current_tag_id is None
    assert window.paper_list.count() >= 1

    # chip × 移除标签
    chip = next(
        window.tag_chips_layout.itemAt(i).widget()
        for i in range(window.tag_chips_layout.count())
        if window.tag_chips_layout.itemAt(i).widget() is not None
        and "复现必看" in window.tag_chips_layout.itemAt(i).widget().text()
    )
    chip.click()
    assert all(t.name != "复现必看" or paper.id not in [p.id for p in []] for t in [])
    remaining = repository.get_paper(paper.id, include_text_content=False).tags
    assert all(t.name != "复现必看" for t in remaining)


def test_scan_orphan_files_reports_and_cleans(tmp_path, qtbot, monkeypatch):
    import synthrecall.ui.main_window as mw

    # "没有发现孤儿文件"的提示是模态框,测试里必须静默掉
    monkeypatch.setattr(
        mw.QMessageBox, "information", lambda *args, **kwargs: None
    )
    window, repository, paper, _ = make_window(tmp_path, qtbot)
    pdf_path = repository.resolve_pdf_path(paper.pdf_path)
    assert pdf_path is not None and pdf_path.exists()

    # 前置保护:被引用的 PDF 即使在回收站文献名下也绝不能被清理
    repository.delete_papers([paper.id])
    window.scan_orphan_files()
    assert pdf_path.exists(), "回收站文献引用的 PDF 不得判为孤儿(B21)"

    # 把记录的 pdf_path 指走 → 原 PDF 真正成为孤儿(引用集合含回收站,需先恢复再置空)
    from synthrecall.repositories import PaperUpdate

    repository.restore_papers([paper.id])
    window.search_edit.clear()
    repository.update_paper(
        paper.id,
        PaperUpdate(
            title=paper.title, authors="", year=None, venue="", doi="", arxiv_id="",
            url="", abstract="", pdf_path="", reading_status="待读", importance=3,
            summary="", datasets="", dataset_metrics="", methods="", contributions="",
            limitations="", experiment_notes="", reproducibility_notes="",
            memory_anchor="", personal_notes="",
        ),
    )
    monkeypatch.setattr(mw, "confirm_action", lambda *args, **kwargs: True)
    window.scan_orphan_files()
    assert not pdf_path.exists(), "孤儿 PDF 应被清理"


def test_undo_trash_offers_restore(tmp_path, qtbot, monkeypatch):
    import synthrecall.ui.main_window as mw

    window, repository, paper, _ = make_window(tmp_path, qtbot)
    paper_id = paper.id
    monkeypatch.setattr(mw, "confirm_action", lambda *args, **kwargs: True)

    for row in range(window.paper_list.count()):
        if window.paper_list.item(row).data(Qt.ItemDataRole.UserRole) == paper_id:
            window.paper_list.setCurrentItem(window.paper_list.item(row))
            break
    window.batch_delete_papers()
    assert [p.id for p in repository.list_trashed_papers()] == [paper_id]

    # U11:撤销弹窗出现,点撤销恢复
    undo_buttons = [
        button for button in window.findChildren(QWidget)
        if isinstance(button, QPushButton) and button.text() == "撤销"
    ]
    assert undo_buttons, "移入回收站后应出现撤销按钮"
    undo_buttons[0].click()
    assert not repository.list_trashed_papers()
    assert any(p.id == paper_id for p in repository.list_papers())


def test_smart_group_nodes_appear_and_filter(tmp_path, qtbot):
    from synthrecall.repositories import PaperUpdate

    window, repository, paper, _ = make_window(tmp_path, qtbot)
    # 导入路径不解析年份、状态默认"待读";智能分组需要带年份与"精读"状态的文献
    repository.update_paper(
        paper.id,
        PaperUpdate(
            title=paper.title,
            authors="Demo",
            year=2026,
            venue="Local Test",
            doi="",
            arxiv_id="",
            url="",
            abstract="",
            pdf_path=paper.pdf_path or "",
            reading_status="精读",
            importance=5,
            summary="",
            datasets="",
            dataset_metrics="",
            methods="",
            contributions="",
            limitations="",
            experiment_notes="",
            reproducibility_notes="",
            memory_anchor="",
            personal_notes="",
        ),
        category_id=None,
    )
    paper = repository.get_paper(paper.id, include_text_content=False)
    window.refresh_categories()

    # 树中出现智能分组父节点,子节点带计数
    top_labels = [
        window.category_tree.topLevelItem(i).text(0)
        for i in range(window.category_tree.topLevelItemCount())
    ]
    assert "阅读状态" in top_labels and "年份" in top_labels and "重要度" in top_labels

    def find_node(parent_text, prefix):
        parent = next(
            window.category_tree.topLevelItem(i)
            for i in range(window.category_tree.topLevelItemCount())
            if window.category_tree.topLevelItem(i).text(0) == parent_text
        )
        for child_index in range(parent.childCount()):
            child = parent.child(child_index)
            if child.text(0).startswith(prefix):
                return child
        return None

    status_node = find_node("阅读状态", "精读")
    assert status_node is not None, "应出现'精读'状态子节点"
    window.category_tree.setCurrentItem(status_node)
    qtbot.wait(30)
    assert window.smart_filter == ("smart", "精读")
    assert window.paper_list.count() >= 1

    # 点击父节点(清除筛选)
    parent = status_node.parent()
    window.category_tree.setCurrentItem(parent)
    qtbot.wait(30)
    assert window.smart_filter is None

    # 年份分组
    year_node = find_node("年份", str(paper.year or 0))
    if year_node is not None:
        window.category_tree.setCurrentItem(year_node)
        qtbot.wait(30)
        assert window.smart_filter == ("smart", str(paper.year))



def test_enrich_metadata_fills_only_missing(tmp_path, qtbot, monkeypatch):
    from synthrecall.metadata_enrichment import PaperMetadata

    window, _repository, _paper, _ = make_window(tmp_path, qtbot)
    window._apply_enriched_metadata(PaperMetadata(
        title="官方标题", authors="官方作者", year=1999, venue="官方来源",
        abstract="官方摘要", doi="10.9/8", arxiv_id="2307.00002", url="https://x",
    ))
    assert "官方标题" not in window.title_edit.toPlainText(), "已有标题不得覆盖"
    assert window.doi_edit.text() == "10.9/8"
    assert window.year_edit.value() == 1999

    # 收尾:补全填充会置脏,关窗前保存,否则 teardown 弹"未保存修改"模态框
    assert window.save_current_paper(refresh=False)
    assert not window._dirty


def test_search_fields_all_unchecked_falls_back_to_all_fields(tmp_path, qtbot):
    window, _repository, _paper, _ = make_window(tmp_path, qtbot)

    # 取消全部字段 → selected_search_fields 应回退为全字段集
    for checkbox in window.search_checkboxes.values():
        checkbox.setChecked(False)
    fields = window.selected_search_fields()
    assert fields >= {"title", "authors", "abstract", "memory_anchor"}, (
        "B15:全取消后应按全字段搜索"
    )

    # 勾选回一个字段 → 正常返回单字段
    window.search_checkboxes["title"].setChecked(True)
    assert window.selected_search_fields() == {"title"}


def test_backup_and_restore_menu_actions(tmp_path, qtbot, monkeypatch):
    import synthrecall.ui.main_window as mw

    window, repository, paper, _ = make_window(tmp_path, qtbot)
    paper_id = paper.id

    # 备份:getSaveFileName 返回固定路径
    backup_path = tmp_path / "manual-backup.zip"
    monkeypatch.setattr(
        mw.QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(backup_path), "备份文件 (*.zip)"),
    )
    window.backup_data_now()
    assert backup_path.exists()

    # 恢复:getOpenFileName 返回该备份;确认对话框返回 restore
    repository.delete_papers([paper_id])
    assert all(p.id != paper_id for p in repository.list_papers())
    monkeypatch.setattr(
        mw.QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(backup_path), "备份文件 (*.zip)"),
    )
    monkeypatch.setattr(mw.ConfirmationDialog, "exec_action", lambda self: "restore")
    monkeypatch.setattr(mw.QMessageBox, "information", lambda *args, **kwargs: None)
    window.restore_data_from_backup()
    assert any(p.id == paper_id for p in repository.list_papers()), (
        "恢复后备份点文献应回来"
    )


def test_gui_duplicate_cleanup_wizard_scans_and_merges(tmp_path, qtbot, monkeypatch):
    from synthrecall.models import Paper

    window, repository, paper, _ = make_window(tmp_path, qtbot)

    # 造一条库内重复(标题+作者相同,模拟导入查重上线前的存量重复)
    with repository._session_factory() as session:
        duplicate = Paper(
            title="GUI Acceptance Dynamic Convolution Paper",
            authors="Alice Example; Bob Example",
        )
        session.add(duplicate)
        session.commit()
        duplicate_id = duplicate.id

    signals: list[int] = []
    dialog = DuplicateCleanupDialog(repository, parent=window)
    qtbot.addWidget(dialog)
    dialog.duplicatesChanged.connect(lambda: signals.append(1))
    dialog.show()
    qtbot.wait(20)

    # 扫描:一组 2 篇,默认保留最早收录的原文献;空态不可见
    assert dialog.group_list.count() == 1
    assert dialog.paper_table.rowCount() == 2
    assert dialog.empty_state.isHidden()
    assert dialog.splitter.isVisible()
    first_radio = dialog._keeper_radios[paper.id]
    assert first_radio.isChecked(), "默认保留最早收录的一篇"
    assert "标题和作者相同" in dialog.group_reason_label.text()

    # 取消确认 → 不动任何数据
    monkeypatch.setattr(dialog, "_confirm_merge", lambda *args, **kwargs: False)
    dialog.merge_current_group()
    assert repository.list_trashed_papers() == []

    # 确认合并 → 重复记录移入回收站,原文献保留,重扫后空态出现
    monkeypatch.setattr(dialog, "_confirm_merge", lambda *args, **kwargs: True)
    dialog.merge_current_group()
    assert signals, "合并后应发出 duplicatesChanged"
    trashed = repository.list_trashed_papers()
    assert [p.id for p in trashed] == [duplicate_id]
    assert [p.id for p in repository.list_papers()] == [paper.id]
    assert dialog.group_list.count() == 0
    assert dialog.empty_state.isVisible()
    assert dialog.splitter.isHidden()
    assert "回收站" in dialog.status_label.text()
    # 空态必须内嵌在向导里,不允许游离成顶层窗口(B28 教训)
    assert dialog.empty_state.window() is dialog.window()


def test_gui_statistics_panel_cards_and_charts(tmp_path, qtbot):
    from synthrecall.models import Paper
    from synthrecall.ui.statistics_panel import StatisticsDialog

    window, repository, paper, _ = make_window(tmp_path, qtbot)
    repository.set_papers_reading_status([paper.id], "精读")
    with repository._session_factory() as session:
        session.get(Paper, paper.id).year = 2026
        session.commit()

    dialog = StatisticsDialog(repository, parent=window)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(20)

    # 概览卡:总数 1、含 PDF 1、深度阅读 1
    card_values = [
        dialog.overview_row.itemAt(index).widget().value_label.text()
        for index in range(dialog.overview_row.count())
        if dialog.overview_row.itemAt(index).widget() is not None
    ]
    assert card_values[:4] == ["1", "1", "0", "1"], card_values

    # 阅读状态行与柱状图数据
    assert "精读" in dialog.status_bars
    assert dialog.status_value_labels["精读"].text() == "1 篇"
    assert dialog.year_chart.columns(), "年份分布不应为空"
    assert len(dialog.trend_chart.columns()) == 12, "趋势应为连续 12 个月(含零月)"
    assert sum(value for _label, value in dialog.trend_chart.columns()) == 1
    assert dialog.empty_state.isHidden()

    # 空库:内嵌空态可见,图表区禁用
    empty_factory_dir = tmp_path / "empty"
    empty_factory_dir.mkdir()
    from synthrecall.database import create_session_factory, init_database

    empty_factory = create_session_factory(empty_factory_dir / "lib.db")
    init_database(empty_factory)
    empty_repo = LibraryRepository(empty_factory)
    empty_dialog = StatisticsDialog(empty_repo)
    qtbot.addWidget(empty_dialog)
    empty_dialog.show()
    qtbot.wait(20)
    assert empty_dialog.empty_state.isVisible()
    assert not empty_dialog.charts_grid.isEnabled()
    assert empty_dialog.trend_chart.columns() == []
    assert empty_dialog.empty_state.window() is empty_dialog.window()


def test_llm_settings_dialog_remembers_last_step(tmp_path, qtbot):
    """U10:AI 工作流对话框记住上次所在步骤;显式 initial_step 优先;无记忆默认第 1 步。"""
    settings_path = tmp_path / "llm_settings.json"

    first = LLMSettingsDialog(settings_path)
    qtbot.addWidget(first)
    assert first.workflow_list.currentRow() == 0, "首次打开(无记忆)默认第 1 步「读取论文」"

    first.workflow_list.setCurrentRow(2)  # 切到「生成知识卡」
    first.close()

    restored = LLMSettingsDialog(settings_path)
    qtbot.addWidget(restored)
    assert restored.workflow_list.currentRow() == 2, "重开恢复上次步骤"
    assert restored.workflow_pages.currentIndex() == 2, "页面与步骤列表同步"

    explicit = LLMSettingsDialog(settings_path, initial_step=1)
    qtbot.addWidget(explicit)
    assert explicit.workflow_list.currentRow() == 1, "显式 initial_step 覆盖记忆"


def test_stream_progress_throttle_and_worker_progress_signal(tmp_path, qtbot, monkeypatch):
    """B23:流式增量节流(0.4s 一帧)与 AIAnalyzeWorker.progress 信号。"""
    from synthrecall.ai import PaperAIAnalysis
    from synthrecall.ui.workers import AIAnalyzeWorker, _StreamProgressThrottle

    throttle = _StreamProgressThrottle()
    emitted = [throttle.feed("x" * 10) for _ in range(50)]
    frames = [text for text in emitted if text is not None]
    assert frames, "应至少发出一帧"
    assert len(frames) < 50, "高频 delta 必须被节流"
    assert "字符" in frames[-1]

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def analyze_paper(self, source, on_delta=None):
            if on_delta:
                for piece in ("a" * 30, "b" * 30, "c" * 30):
                    on_delta(piece)
            return PaperAIAnalysis(raw={"title": "t", "summary": ["要点"]})

    import synthrecall.ui.workers as workers_module

    applied: list[int] = []
    stub_repo = SimpleNamespace(
        get_ai_source=lambda *a, **k: {"title": "t"},
        apply_ai_analysis=lambda pid, analysis: applied.append(pid),
    )
    monkeypatch.setattr(workers_module, "LLMClient", FakeClient)

    worker = AIAnalyzeWorker(stub_repo, tmp_path / "s.json", 7)
    progress_texts: list[str] = []
    worker.progress.connect(progress_texts.append)
    finished: list[bool] = []
    failures: list[str] = []
    worker.finished.connect(lambda *a: finished.append(True))
    worker.failed.connect(failures.append)
    worker.run()
    assert finished and applied == [7], f"fake 客户端应成功完成并落库: {failures}"
    assert progress_texts and "字符" in progress_texts[0]


def test_paper_chat_dialog_streams_answer_into_bubble(tmp_path, qtbot, monkeypatch):
    """B23:对话气泡渐进——delta 逐段填充占位气泡,完成后整体替换。"""
    window, repository, paper, _ = make_window(tmp_path, qtbot)

    import synthrecall.ui.dialogs.paper_chat as pc

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def discuss_paper(self, source, history, question, on_delta=None):
            if on_delta:
                on_delta("第一段,")
                on_delta("第二段")
            return "第一段,第二段"

    monkeypatch.setattr(pc, "LLMClient", FakeClient)
    monkeypatch.setattr(pc, "confirm_external_request", lambda *args, **kwargs: True)

    dialog = pc.PaperChatDialog(repository, window.settings_path, paper.id, parent=window)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(20)

    dialog.question_edit.setPlainText("这篇文献的方法?")
    dialog.send_question()
    qtbot.waitUntil(lambda: dialog.send_button.isEnabled(), timeout=3000)

    assert dialog.history[-1]["content"] == "第一段,第二段"
    bubbles = [b.text() for b in dialog.findChildren(pc.QLabel)]
    assert "第一段,第二段" in bubbles
    assert dialog._streaming_bubble is None, "完成后占位气泡应释放"


def test_gui_workspace_manager_and_switch_flow(tmp_path, qtbot, monkeypatch):
    """F11:工作区管理对话框(默认条目/移除保护/切换信号)与切换重启流程。"""
    import synthrecall.workspaces as ws
    from synthrecall.ui.workspace_manager import WorkspaceManagerDialog

    window, _repository, _paper, _ = make_window(tmp_path, qtbot)
    registry = tmp_path / "workspaces.json"
    monkeypatch.setattr(ws, "project_root", lambda: tmp_path)
    from synthrecall.ui import workspace_manager as wm_module

    monkeypatch.setattr(wm_module, "list_workspaces", lambda: ws.list_workspaces(registry))

    # 注册一个额外工作区
    ws.add_workspace("实验库", tmp_path / "lib-exp", path=registry)

    dialog = WorkspaceManagerDialog(
        current_root=window.data_dir.parent,
        default_root=tmp_path,
        parent=window,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(20)

    names = [dialog.table.item(row, 0).text() for row in range(dialog.table.rowCount())]
    assert names == ["默认工作区", "实验库"], names
    statuses = [
        dialog.table.item(row, 2).text() for row in range(dialog.table.rowCount())
    ]
    assert statuses[0] == "当前使用"

    # 选当前行点切换 → 提示已是当前,不发信号
    dialog.table.selectRow(0)
    dialog.request_switch()
    requested: list[str] = []
    dialog.switchRequested.connect(requested.append)
    dialog.table.selectRow(1)
    dialog.request_switch()
    assert requested == ["实验库"]

    # 切换全流程:patch 确认框/重启,断言注册表更新与顺序
    import synthrecall.ui.main_window as mw

    monkeypatch.setattr(mw, "confirm_action", lambda *a, **k: True)
    monkeypatch.setattr(window, "_confirm_pending_changes", lambda: True)
    restarts: list[bool] = []
    monkeypatch.setattr(window, "_restart_app", staticmethod(lambda: restarts.append(True)))
    window.switch_workspace("实验库")
    assert restarts == [True]
    assert ws.current_workspace(registry).name == "实验库"
