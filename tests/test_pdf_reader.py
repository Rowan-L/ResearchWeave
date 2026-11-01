import pymupdf as fitz
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QWheelEvent
from PySide6.QtWidgets import QApplication, QInputDialog

from synthrecall.ui.pdf_reader import ContinuousPDFReader


def write_pdf(path, *, pages=6, prefix="paper"):
    document = fitz.open()
    for index in range(pages):
        page = document.new_page(width=400, height=560)
        page.insert_text((48, 70), f"{prefix} page {index + 1}")
    document.save(path)
    document.close()


def test_continuous_reader_switches_versions_at_same_page_and_resets_zoom(tmp_path, qtbot):
    original = tmp_path / "original.pdf"
    mono = tmp_path / "mono.pdf"
    dual = tmp_path / "dual.pdf"
    write_pdf(original, prefix="original")
    write_pdf(mono, prefix="mono")
    write_pdf(dual, prefix="dual")

    reader = ContinuousPDFReader()
    qtbot.addWidget(reader)
    reader.resize(760, 700)
    reader.show()
    reader.set_documents(original=original, mono=mono, dual=dual)
    qtbot.wait(50)

    assert reader.page_count == 6
    assert (
        reader.scroll.verticalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAlwaysOn
    )

    scroll_bar = reader.scroll.verticalScrollBar()
    start_value = scroll_bar.value()
    wheel_event = QWheelEvent(
        QPointF(20, 20),
        QPointF(20, 20),
        QPoint(0, 0),
        QPoint(0, -720),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(reader.scroll.viewport(), wheel_event)
    qtbot.wait(20)
    assert scroll_bar.value() > start_value

    reader.search_edit.setText("page 5")
    reader.find_text()
    assert reader._search_pages == [4]
    assert reader.page_index == 4
    reader.copy_current_page_text()
    assert "page 5" in QApplication.clipboard().text().lower()

    reader.jump_to_page(4)
    assert reader.page_index == 4

    reader.mode_buttons["mono"].click()
    qtbot.wait(50)
    assert reader.pdf_path == mono
    assert reader.page_index == 4

    ctrl_wheel = QWheelEvent(
        QPointF(20, 20),
        QPointF(20, 20),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(reader.scroll.viewport(), ctrl_wheel)
    assert reader.zoom == 1.15
    reader.change_zoom(0.35)
    assert reader.zoom == 1.5
    reader.reset_zoom()
    assert reader.zoom == 1.0


def test_continuous_reader_evicts_distant_page_pixmaps(tmp_path, qtbot):
    original = tmp_path / "long.pdf"
    write_pdf(original, pages=12)
    reader = ContinuousPDFReader()
    qtbot.addWidget(reader)
    reader.resize(760, 700)
    reader.show()
    reader.set_documents(original=original)
    qtbot.wait(50)

    for index in range(reader.page_count):
        reader._render_page(index)
    assert all(not label.pixmap().isNull() for label in reader._page_labels)

    reader.scroll.verticalScrollBar().setValue(0)
    reader._render_visible_pages()
    assert reader._page_labels[0].pixmap().isNull() is False
    assert reader._page_labels[-1].pixmap().isNull() is True


def test_continuous_reader_annotations_colors_ink_freetext_and_save(
    tmp_path, qtbot, monkeypatch
):
    original = tmp_path / "original.pdf"
    write_pdf(original, pages=1)

    reader = ContinuousPDFReader()
    qtbot.addWidget(reader)
    reader.resize(760, 700)
    reader.show()
    reader.set_documents(original=original)
    reader._render_page(0)

    reader.set_annotation_tool("highlight")
    page_label = reader._page_labels[0]
    qtbot.mousePress(page_label, Qt.MouseButton.LeftButton, pos=QPoint(40, 40))
    qtbot.mouseMove(page_label, pos=QPoint(220, 100))
    qtbot.mouseRelease(page_label, Qt.MouseButton.LeftButton, pos=QPoint(220, 100))

    reader.set_annotation_tool("pen")
    reader._apply_annotation_gesture(
        0,
        "pen",
        [QPointF(60, 130), QPointF(90, 150), QPointF(140, 135)],
    )

    reader.note_color = QColor("#7c3aed")
    monkeypatch.setattr(
        QInputDialog,
        "getMultiLineText",
        lambda *_args, **_kwargs: ("可见的浮动批注", True),
    )
    reader._apply_annotation_gesture(
        0,
        "note",
        [QPointF(80, 180), QPointF(280, 250)],
    )

    assert reader.annotation_buttons["delete"].text() == "橡皮擦"
    assert reader.has_unsaved_annotations
    assert reader.save_button.isEnabled()
    qtbot.mouseClick(reader.save_button, Qt.MouseButton.LeftButton)
    assert not reader.has_unsaved_annotations
    assert not (tmp_path / "backups").exists() or not list((tmp_path / "backups").glob("*.pdf")), (
        "按用户要求,保存批注不应再生成原版备份"
    )

    reader.close_documents()
    with fitz.open(original) as saved:
        page = saved[0]
        annotations = list(page.annots() or [])
        assert {annotation.type[1] for annotation in annotations} == {
            "Highlight",
            "Ink",
            "FreeText",
        }
        free_text = next(
            annotation for annotation in annotations if annotation.type[1] == "FreeText"
        )
        assert free_text.info["content"] == "可见的浮动批注"


def test_reader_tool_toggle_text_selection_context_actions_and_translation_signal(
    tmp_path, qtbot
):
    original = tmp_path / "selectable.pdf"
    write_pdf(original, pages=1, prefix="selectable words")
    reader = ContinuousPDFReader()
    qtbot.addWidget(reader)
    reader.resize(760, 700)
    reader.show()
    reader.set_documents(original=original)
    reader._render_page(0)

    reader.annotation_buttons["highlight"].click()
    assert reader._annotation_tool == "highlight"
    assert reader.annotation_buttons["highlight"].isChecked()
    reader.annotation_buttons["highlight"].click()
    assert reader._annotation_tool == "browse"
    assert reader.annotation_buttons["browse"].isChecked()
    assert not reader.annotation_buttons["highlight"].isChecked()

    reader._apply_text_selection(
        0,
        [QPointF(40, 48), QPointF(250, 82)],
    )
    assert "selectable words" in reader._selected_text
    reader.copy_selected_text()
    assert "selectable words" in QApplication.clipboard().text()
    reader.highlight_selected_text()
    assert reader.has_unsaved_annotations
    page = reader._document().load_page(0)
    annotation = next(page.annots())
    assert annotation.type[1] == "Highlight"
    assert reader._annotation_at(page, fitz.Point(55, 68)) is not None

    with qtbot.waitSignal(reader.translateRequested, timeout=500):
        reader.translate_button.click()


def test_find_matches_enable_navigation_buttons_immediately(tmp_path, qtbot):
    original = tmp_path / "find.pdf"
    write_pdf(original, pages=3, prefix="target word here")
    reader = ContinuousPDFReader()
    qtbot.addWidget(reader)
    reader.resize(760, 700)
    reader.show()
    reader.set_documents(original=original)
    reader._render_page(0)

    reader.search_edit.setText("target")
    reader.find_text()
    assert reader._search_pages, "应找到匹配页"
    # B16 回归:查找成功后,上一个/下一个按钮必须立即可用
    assert reader.next_match_button.isEnabled()
    assert reader.previous_match_button.isEnabled()


def test_save_fallback_without_backup_when_incremental_write_refused(
    tmp_path, qtbot, monkeypatch
):
    original = tmp_path / "fallback.pdf"
    write_pdf(original, pages=1)

    reader = ContinuousPDFReader()
    qtbot.addWidget(reader)
    reader.resize(760, 700)
    reader.show()
    reader.set_documents(original=original)
    reader._render_page(0)

    def refuse_incr(self):
        raise RuntimeError("code=4: Can't do incremental writes on a repaired file")

    monkeypatch.setattr(fitz.Document, "saveIncr", refuse_incr)

    def add_highlight(y):
        reader._apply_annotation_gesture(
            0, "highlight", [QPointF(40, y), QPointF(220, y + 20)]
        )

    add_highlight(40)
    assert reader.save_annotations() is True
    with fitz.open(original) as saved:
        assert sum(len(list(page.annots() or [])) for page in saved) == 1
    assert not (tmp_path / "backups").exists(), (
        "按用户要求,保存批注不应再生成原版备份目录"
    )

    add_highlight(120)
    assert reader.save_annotations() is True
    with fitz.open(original) as saved:
        assert sum(len(list(page.annots() or [])) for page in saved) == 2, (
            "带痕迹的版本应随每次保存累积"
        )
    assert not list(tmp_path.glob("*.saving.tmp")), "临时文件应被清理"


def test_reader_page_spin_jumps_to_page(tmp_path, qtbot):
    original = tmp_path / "jump.pdf"
    write_pdf(original, pages=6, prefix="page marker")
    reader = ContinuousPDFReader()
    qtbot.addWidget(reader)
    reader.resize(760, 700)
    reader.show()
    reader.set_documents(original=original)
    qtbot.wait(50)

    assert reader.page_spin.maximum() == 6
    assert reader.page_spin.value() == 1
    reader.page_spin.setValue(5)
    reader._go_to_spin_page()
    assert reader.page_index == 4
    assert reader.page_spin.value() == 5
    # 跳转后回读的页码不应被滚动回调改成相邻页
    assert reader.page_spin.value() == reader.page_index + 1

    # 箭头/滚轮(stepBy)调页应立即跳转,且按阅读方向:▲/滚轮上 = 往文档开头
    reader.page_spin.stepBy(1)
    assert reader.page_index == 3
    assert reader.page_spin.value() == 4
    reader.page_spin.stepBy(-1)
    assert reader.page_index == 4
    assert reader.page_spin.value() == 5

    # 宽度自适应:不得小于含"第 / 页"前后缀与箭头的最窄需求
    assert reader.page_spin.width() >= reader.page_spin.sizeHint().width()


def test_reader_annotations_summary_and_changed_signal(tmp_path, qtbot, monkeypatch):
    original = tmp_path / "summary.pdf"
    write_pdf(original, pages=2, prefix="annotation target text")
    reader = ContinuousPDFReader()
    qtbot.addWidget(reader)
    reader.resize(760, 700)
    reader.show()
    reader.set_documents(original=original)
    reader._render_page(0)
    qtbot.wait(30)

    assert reader.annotations_summary() == []

    reader.set_annotation_tool("highlight")
    reader._apply_annotation_gesture(
        0, "highlight", [QPointF(40, 40), QPointF(220, 60)]
    )
    monkeypatch.setattr(
        QInputDialog,
        "getMultiLineText",
        lambda *_args, **_kwargs: ("摘要批注内容", True),
    )
    reader._apply_annotation_gesture(
        1, "note", [QPointF(80, 120), QPointF(280, 160)]
    )

    summary = reader.annotations_summary()
    kinds = {(entry["page"], entry["kind"]) for entry in summary}
    assert (1, "Highlight") in kinds
    assert (2, "FreeText") in kinds
    note_entry = next(e for e in summary if e["kind"] == "FreeText")
    assert note_entry["text"] == "摘要批注内容"
    highlight_entry = next(e for e in summary if e["kind"] == "Highlight")
    assert highlight_entry["text"], "高亮条目应摘录覆盖的文字"

