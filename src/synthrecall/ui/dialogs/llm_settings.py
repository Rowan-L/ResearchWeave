"""API 设置 / AI 工作流对话框(E1 从 main_window 拆出,行为不变)。"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from synthrecall.ai import DEFAULT_PROMPT_TEMPLATE, model_catalog_endpoint, parse_model_catalog
from synthrecall.branding import APP_NAME, app_icon_path
from synthrecall.settings import (
    COMMON_LLM_PRESETS,
    DEFAULT_EVIDENCE_SECTIONS,
    DEFAULT_EVIDENCE_TYPES,
    DEFAULT_PROVIDER,
    LLMProviderPreset,
    LLMSettings,
    get_llm_api_key,
    llm_api_key_header,
    normalize_llm_base_url,
    set_llm_api_key,
)
from synthrecall.ui import theme
from synthrecall.ui.widgets import NoWheelComboBox, NoWheelSpinBox, PasswordVisibilityButton


class LLMSettingsDialog(QDialog):
    def __init__(
        self,
        settings_path: Path,
        parent: QWidget | None = None,
        *,
        initial_step: int | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings_path = settings_path
        self._initial_step = initial_step
        settings = LLMSettings.load(settings_path)
        initial_key_scope = normalize_llm_base_url(settings.base_url)
        self._api_key_cache = {
            initial_key_scope: get_llm_api_key(settings.base_url)
        }
        self._active_api_key_scope = initial_key_scope
        self._api_auth_style = settings.api_auth_style
        self._send_temperature = settings.send_temperature
        self._enable_thinking = settings.enable_thinking
        self._ui_settings = QSettings(
            str(settings_path.parent / "ui_state.ini"),
            QSettings.Format.IniFormat,
        )
        self._splitter_save_timer = QTimer(self)
        self._splitter_save_timer.setSingleShot(True)
        self._splitter_save_timer.setInterval(250)
        self._model_network_manager = QNetworkAccessManager(self)
        self._model_reply: QNetworkReply | None = None

        self.setObjectName("AIWorkflowDialog")
        self.setWindowTitle(f"AI 工作流 · {APP_NAME}")
        self.resize(1160, 760)
        self.setMinimumSize(980, 650)
        icon = app_icon_path()
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("AIWorkflowHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(24, 18, 24, 16)
        header_layout.setSpacing(3)
        title = QLabel("AI 工作流")
        title.setObjectName("AIWorkflowTitle")
        subtitle = QLabel("配置 AI 从论文读取证据、区分推断并生成结构化知识卡的方式")
        subtitle.setObjectName("AIWorkflowSubtitle")
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)

        self.workflow_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workflow_splitter.setObjectName("AIWorkflowSplitter")
        self.workflow_splitter.setChildrenCollapsible(False)

        rail = QFrame()
        rail.setObjectName("AIWorkflowRail")
        rail.setMinimumWidth(145)
        rail.setMaximumWidth(210)
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(12, 16, 12, 16)
        rail_layout.setSpacing(10)
        rail_heading = QLabel("处理步骤")
        rail_heading.setObjectName("AISectionLabel")
        self.workflow_list = QListWidget()
        self.workflow_list.setObjectName("AIWorkflowSteps")
        self.workflow_list.setSpacing(4)
        self.workflow_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.workflow_list.addItems(
            ["1  读取论文", "2  提取证据", "3  生成知识卡"]
        )
        rail_layout.addWidget(rail_heading)
        rail_layout.addWidget(self.workflow_list, 1)

        self.workflow_pages = QStackedWidget()
        self.workflow_pages.setObjectName("AIWorkflowPages")
        self.workflow_pages.setMinimumWidth(440)
        self.workflow_pages.addWidget(self._build_connection_page(settings))
        self.workflow_pages.addWidget(self._build_extraction_page(settings))
        self.workflow_pages.addWidget(self._build_output_page())

        preview = self._build_output_preview()
        preview.setMinimumWidth(270)
        preview.setMaximumWidth(430)

        self.workflow_splitter.addWidget(rail)
        self.workflow_splitter.addWidget(self.workflow_pages)
        self.workflow_splitter.addWidget(preview)
        self.workflow_splitter.setStretchFactor(0, 0)
        self.workflow_splitter.setStretchFactor(1, 1)
        self.workflow_splitter.setStretchFactor(2, 0)
        self._restore_splitter_state()
        self.workflow_splitter.splitterMoved.connect(
            lambda *_args: self._splitter_save_timer.start()
        )
        self._splitter_save_timer.timeout.connect(self._save_splitter_state)

        self.workflow_list.currentRowChanged.connect(
            self.workflow_pages.setCurrentIndex
        )
        # U10:未显式指定步骤时恢复上次所在步骤(无记忆默认第 1 步「读取论文」,
        # 保持 U1「新用户直达 API 配置」的语义);切换即持久化。
        self.workflow_list.setCurrentRow(self._resolve_initial_row())
        self.workflow_list.currentRowChanged.connect(self._remember_last_step)

        footer = QFrame()
        footer.setObjectName("AIWorkflowFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(24, 12, 24, 14)
        local_note = QLabel("配置与 API Key 均保存在本机")
        local_note.setObjectName("AIWorkflowSubtitle")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        save_button.setText("保存工作流")
        save_button.setObjectName("PrimaryButton")
        cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_button.setText("取消")
        buttons.accepted.connect(self.save_and_accept)
        buttons.rejected.connect(self.reject)
        footer_layout.addWidget(local_note, 1)
        footer_layout.addWidget(buttons)

        layout.addWidget(header)
        layout.addWidget(self.workflow_splitter, 1)
        layout.addWidget(footer)
        self.setStyleSheet(theme.apply_to_qss(self._workflow_style()))

    def _build_connection_page(self, settings: LLMSettings) -> QWidget:
        content = QWidget()
        content.setObjectName("AIWorkflowContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(22, 16, 22, 16)
        layout.setSpacing(12)
        title = QLabel("读取论文")
        title.setObjectName("AIPageTitle")
        hint = QLabel("选择兼容服务并控制单次发送的论文正文长度。")
        hint.setObjectName("AIWorkflowSubtitle")

        form_host = QFrame()
        form_host.setObjectName("AISettingsGroup")
        form = QFormLayout(form_host)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(12)
        self.preset_combo = NoWheelComboBox()
        for preset in COMMON_LLM_PRESETS:
            self.preset_combo.addItem(preset.name, preset)
        self.preset_combo.addItem("自定义 API（OpenAI-compatible）", None)
        self.base_url_edit = QLineEdit(settings.base_url)
        self.model_combo = NoWheelComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.model_edit = self.model_combo.lineEdit()
        self.model_edit.setText(settings.model)
        self.model_edit.setPlaceholderText("输入模型名，或点击获取模型")
        self.fetch_models_button = QPushButton("获取模型")
        self.fetch_models_button.setObjectName("TinyButton")
        self.fetch_models_button.setToolTip(
            "调用当前服务的 /models 接口。该请求会向服务发送 API Key。"
        )
        self.fetch_models_button.clicked.connect(self.fetch_available_models)
        model_row = QHBoxLayout()
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.setSpacing(8)
        model_row.addWidget(self.model_combo, 1)
        model_row.addWidget(self.fetch_models_button)
        self.model_status_label = QLabel("可手动输入；也可从服务获取模型列表")
        self.model_status_label.setObjectName("AIWorkflowSubtitle")
        model_field = QVBoxLayout()
        model_field.setContentsMargins(0, 0, 0, 0)
        model_field.setSpacing(4)
        model_field.addLayout(model_row)
        model_field.addWidget(self.model_status_label)
        self.max_chars_edit = NoWheelSpinBox()
        self.max_chars_edit.setRange(5_000, 200_000)
        self.max_chars_edit.setSingleStep(5_000)
        self.max_chars_edit.setValue(settings.max_chars)
        self.api_key_edit = QLineEdit(
            self._api_key_cache[self._active_api_key_scope]
        )
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-...")
        self.api_key_visibility_button = PasswordVisibilityButton()
        self.api_key_visibility_button.toggled.connect(
            self._toggle_api_key_visibility
        )
        api_key_row = QHBoxLayout()
        api_key_row.setContentsMargins(0, 0, 0, 0)
        api_key_row.setSpacing(8)
        api_key_row.addWidget(self.api_key_edit, 1)
        api_key_row.addWidget(self.api_key_visibility_button)
        self.translation_output_dir_edit = QLineEdit(settings.translation_output_dir)
        self.translation_output_dir_edit.setPlaceholderText("留空则使用 data/translations")
        self.confirm_external_checkbox = QCheckBox("调用外部模型前显示发送内容和费用提醒")
        self.confirm_external_checkbox.setChecked(settings.confirm_external_requests)
        # F20-3:默认只生成译文单本(--no-dual),双语对照按需打开
        self.dual_output_checkbox = QCheckBox("同时生成双语对照 PDF(体积翻倍,默认关闭)")
        self.dual_output_checkbox.setChecked(settings.translation_dual_output)
        choose_translation_dir = QPushButton("选择")
        choose_translation_dir.setObjectName("TinyButton")
        choose_translation_dir.clicked.connect(self.choose_translation_output_dir)
        translation_dir_row = QHBoxLayout()
        translation_dir_row.setContentsMargins(0, 0, 0, 0)
        translation_dir_row.addWidget(self.translation_output_dir_edit, 1)
        translation_dir_row.addWidget(choose_translation_dir)

        self.preset_combo.setCurrentIndex(self._matching_preset_index(settings))
        self.preset_combo.currentIndexChanged.connect(self.apply_preset)
        self.base_url_edit.editingFinished.connect(self._sync_api_key_scope)
        self.apply_preset(self.preset_combo.currentIndex())

        form.addRow("API 服务", self.preset_combo)
        form.addRow("Base URL", self.base_url_edit)
        form.addRow("模型", model_field)
        form.addRow("最大文本字符", self.max_chars_edit)
        form.addRow("API Key", api_key_row)
        form.addRow("翻译输出目录", translation_dir_row)
        form.addRow("双语对照", self.dual_output_checkbox)
        form.addRow("隐私确认", self.confirm_external_checkbox)
        note = QLabel(
            "支持 OpenAI-compatible Chat Completions。不同 API 服务的 Key 会分别保存在系统凭据库中；点击眼睛可临时显示当前 Key。"
        )
        note.setObjectName("AIHintBox")
        note.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(form_host)
        layout.addWidget(note)
        layout.addStretch(1)
        return self._scroll_page(content)

    def _build_extraction_page(self, settings: LLMSettings) -> QWidget:
        content = QWidget()
        content.setObjectName("AIWorkflowContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(22, 16, 22, 16)
        layout.setSpacing(12)
        title = QLabel("提取证据")
        title.setObjectName("AIPageTitle")
        hint = QLabel("让模型围绕可定位证据提取事实，并显式区分推断与缺失信息。")
        hint.setObjectName("AIWorkflowSubtitle")

        rules = QFrame()
        rules.setObjectName("AISettingsGroup")
        rules_layout = QVBoxLayout(rules)
        rules_layout.setContentsMargins(14, 10, 14, 10)
        rules_layout.setSpacing(8)
        rules_title = QLabel("回答规则")
        rules_title.setObjectName("AISectionLabel")
        self.require_source_evidence_checkbox = QCheckBox("引用原文依据")
        self.require_source_evidence_checkbox.setChecked(
            settings.require_source_evidence
        )
        self.distinguish_inference_checkbox = QCheckBox("区分事实与推断")
        self.distinguish_inference_checkbox.setChecked(settings.distinguish_inference)
        self.leave_missing_blank_checkbox = QCheckBox("缺失信息留空")
        self.leave_missing_blank_checkbox.setChecked(settings.leave_missing_blank)
        rule_options = QHBoxLayout()
        rule_options.setSpacing(14)
        rule_options.addWidget(self.require_source_evidence_checkbox)
        rule_options.addWidget(self.distinguish_inference_checkbox)
        rule_options.addWidget(self.leave_missing_blank_checkbox)
        rule_options.addStretch(1)
        rules_layout.addWidget(rules_title)
        rules_layout.addLayout(rule_options)

        scope = QFrame()
        scope.setObjectName("AISettingsGroup")
        scope_layout = QVBoxLayout(scope)
        scope_layout.setContentsMargins(14, 10, 14, 10)
        scope_layout.setSpacing(7)
        scope_title = QLabel("证据范围")
        scope_title.setObjectName("AISectionLabel")
        scope_hint = QLabel("从论文的哪些部分抽取可定位依据")
        scope_hint.setObjectName("AIWorkflowSubtitle")
        scope_grid = QGridLayout()
        scope_grid.setHorizontalSpacing(16)
        scope_grid.setVerticalSpacing(8)
        self.evidence_section_checkboxes: dict[str, QCheckBox] = {}
        for index, name in enumerate(DEFAULT_EVIDENCE_SECTIONS):
            checkbox = QCheckBox(name)
            checkbox.setChecked(name in settings.evidence_sections)
            self.evidence_section_checkboxes[name] = checkbox
            scope_grid.addWidget(checkbox, index // 2, index % 2)
        scope_layout.addWidget(scope_title)
        scope_layout.addWidget(scope_hint)
        scope_layout.addLayout(scope_grid)

        evidence_types = QFrame()
        evidence_types.setObjectName("AISettingsGroup")
        type_layout = QVBoxLayout(evidence_types)
        type_layout.setContentsMargins(14, 10, 14, 10)
        type_layout.setSpacing(7)
        type_title = QLabel("证据类型")
        type_title.setObjectName("AISectionLabel")
        type_hint = QLabel("控制知识卡需要识别和输出的证据类型")
        type_hint.setObjectName("AIWorkflowSubtitle")
        type_grid = QGridLayout()
        type_grid.setHorizontalSpacing(16)
        type_grid.setVerticalSpacing(8)
        self.evidence_type_checkboxes: dict[str, QCheckBox] = {}
        for index, name in enumerate(DEFAULT_EVIDENCE_TYPES):
            checkbox = QCheckBox(name)
            checkbox.setChecked(name in settings.evidence_types)
            self.evidence_type_checkboxes[name] = checkbox
            type_grid.addWidget(checkbox, index // 3, index % 3)
        type_layout.addWidget(type_title)
        type_layout.addWidget(type_hint)
        type_layout.addLayout(type_grid)

        self.raw_prompt_toggle = QPushButton("查看原始提示词")
        self.raw_prompt_toggle.setObjectName("GhostButton")
        self.raw_prompt_toggle.setCheckable(True)
        self.raw_prompt_panel = QFrame()
        self.raw_prompt_panel.setObjectName("AISettingsGroup")
        raw_layout = QVBoxLayout(self.raw_prompt_panel)
        raw_layout.setContentsMargins(12, 12, 12, 12)
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setObjectName("FormulaText")
        self.prompt_edit.setAcceptRichText(False)
        self.prompt_edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.prompt_edit.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.prompt_edit.setMinimumHeight(260)
        self.prompt_edit.setPlainText(
            settings.prompt_template or DEFAULT_PROMPT_TEMPLATE
        )
        reset_prompt = QPushButton("恢复默认提示词")
        reset_prompt.setObjectName("TinyButton")
        reset_prompt.setToolTip(f"恢复 {APP_NAME} 的默认证据提取模板。")
        reset_prompt.clicked.connect(self.reset_prompt)
        raw_layout.addWidget(self.prompt_edit)
        raw_layout.addWidget(reset_prompt, 0, Qt.AlignmentFlag.AlignRight)
        self.raw_prompt_panel.hide()
        self.raw_prompt_toggle.toggled.connect(self._toggle_raw_prompt)

        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(rules)
        layout.addWidget(scope)
        layout.addWidget(evidence_types)
        layout.addWidget(self.raw_prompt_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.raw_prompt_panel)
        layout.addStretch(1)
        return self._scroll_page(content)

    def _build_output_page(self) -> QWidget:
        content = QWidget()
        content.setObjectName("AIWorkflowContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(26, 22, 26, 22)
        layout.setSpacing(16)
        title = QLabel("生成知识卡")
        title.setObjectName("AIPageTitle")
        hint = QLabel("模型输出会经过 JSON 校验，再写入当前文献的对应字段。")
        hint.setObjectName("AIWorkflowSubtitle")
        schema = QFrame()
        schema.setObjectName("AISettingsGroup")
        schema_layout = QVBoxLayout(schema)
        schema_layout.setContentsMargins(16, 14, 16, 14)
        schema_layout.setSpacing(8)
        for heading, description in (
            ("论文概览", "标题、作者、年份、来源与摘要"),
            ("方法与贡献", "研究问题、具体方法、结构作用与创新点"),
            ("实验与证据", "数据集、实验设置、对比、消融与关键结果"),
            ("局限与复现", "局限性、代码状态、训练配置和复现风险"),
            ("记忆锚点", "半年后仍能帮助找回论文的具体短语"),
        ):
            label = QLabel(f"{heading}\n{description}")
            label.setObjectName("AIOutputRow")
            label.setWordWrap(True)
            schema_layout.addWidget(label)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(schema)
        layout.addStretch(1)
        return self._scroll_page(content)

    def _build_output_preview(self) -> QFrame:
        preview = QFrame()
        preview.setObjectName("AIOutputPreview")
        layout = QVBoxLayout(preview)
        layout.setContentsMargins(18, 20, 18, 18)
        layout.setSpacing(12)
        title = QLabel("输出预览")
        title.setObjectName("AIPageTitle")
        subtitle = QLabel("当前规则生成的知识卡结构")
        subtitle.setObjectName("AIWorkflowSubtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        for heading, sample in (
            ("研究目标", "论文解决什么问题，以及该问题为何重要。"),
            ("方法", "具体模块、作用和与整体方法的关系。"),
            ("实验与证据", "数据集、对比实验、消融和可定位依据。"),
            ("局限性", "原文承认的限制与明确标记的推断。"),
            ("记忆锚点", "短、具体且可用于未来检索的线索。"),
        ):
            row = QLabel(f"{heading}\n{sample}")
            row.setObjectName("AIPreviewRow")
            row.setWordWrap(True)
            layout.addWidget(row)
        layout.addStretch(1)
        return preview

    @staticmethod
    def _scroll_page(content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName("AIWorkflowScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        return scroll

    def _toggle_raw_prompt(self, visible: bool) -> None:
        self.raw_prompt_panel.setVisible(visible)
        self.raw_prompt_toggle.setText(
            "收起原始提示词" if visible else "查看原始提示词"
        )

    def _resolve_initial_row(self) -> int:
        """U10:显式 initial_step 优先;否则恢复上次步骤,无记忆默认第 1 步。"""
        if self._initial_step is not None:
            return max(0, min(self._initial_step, 2))
        saved = self._ui_settings.value("workflow/last_step")
        try:
            row = int(saved) if saved is not None else 0
        except (TypeError, ValueError):
            row = 0
        return max(0, min(row, 2))

    def _remember_last_step(self, row: int) -> None:
        self._ui_settings.setValue("workflow/last_step", row)
        self._ui_settings.sync()

    def _restore_splitter_state(self) -> None:
        state = self._ui_settings.value("splitters/ai_workflow")
        restored = False
        if state is not None:
            try:
                restored = self.workflow_splitter.restoreState(state)
            except (TypeError, ValueError):
                restored = False
        if not restored:
            self.workflow_splitter.setSizes([170, 640, 320])

    def _save_splitter_state(self) -> None:
        self._ui_settings.setValue(
            "splitters/ai_workflow", self.workflow_splitter.saveState()
        )
        self._ui_settings.sync()

    def _matching_preset_index(self, settings: LLMSettings) -> int:
        for index, preset in enumerate(COMMON_LLM_PRESETS):
            if settings.base_url.rstrip("/") == preset.base_url.rstrip("/") and settings.model == preset.model:
                return index
        return len(COMMON_LLM_PRESETS)

    def apply_preset(self, _index: int) -> None:
        preset = self.preset_combo.currentData()
        if not isinstance(preset, LLMProviderPreset):
            self._api_auth_style = "bearer"
            self._send_temperature = True
            self._enable_thinking = None
            self.fetch_models_button.setEnabled(True)
            self.model_status_label.setText("可手动输入；也可从服务获取模型列表")
            return
        self._switch_api_key_scope(preset.base_url)
        self.base_url_edit.setText(preset.base_url)
        self.model_edit.setText(preset.model)
        self.api_key_edit.setPlaceholderText(preset.api_key_placeholder)
        self._api_auth_style = preset.api_auth_style
        self._send_temperature = preset.send_temperature
        self._enable_thinking = preset.enable_thinking
        self.fetch_models_button.setEnabled(preset.model_catalog_supported)
        if preset.model_catalog_supported:
            self.model_status_label.setText("可手动输入；也可从服务获取模型列表")
        else:
            self.model_status_label.setText(
                "官方暂未公开模型列表接口；已填入文档中的模型，可手动修改"
            )

    def _remember_current_api_key(self) -> None:
        self._api_key_cache[self._active_api_key_scope] = (
            self.api_key_edit.text().strip()
        )

    def _switch_api_key_scope(self, base_url: str) -> None:
        new_scope = normalize_llm_base_url(base_url)
        if new_scope == self._active_api_key_scope:
            return
        self._remember_current_api_key()
        if new_scope not in self._api_key_cache:
            self._api_key_cache[new_scope] = get_llm_api_key(
                base_url, allow_legacy=False
            )
        self._active_api_key_scope = new_scope
        self.api_key_edit.setText(self._api_key_cache[new_scope])

    def _sync_api_key_scope(self) -> None:
        self._switch_api_key_scope(self.base_url_edit.text())

    def _toggle_api_key_visibility(self, visible: bool) -> None:
        self.api_key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        label = "隐藏 API Key" if visible else "显示 API Key"
        self.api_key_visibility_button.setAccessibleName(label)
        self.api_key_visibility_button.setToolTip(label)
        self.api_key_visibility_button.update()

    def fetch_available_models(self) -> None:
        if self._model_reply is not None and self._model_reply.isRunning():
            return
        base_url = self.base_url_edit.text().strip()
        api_key = self.api_key_edit.text().strip()
        if not base_url:
            QMessageBox.information(self, "缺少 Base URL", "请先填写 API 服务地址。")
            return
        if not api_key:
            QMessageBox.information(self, "缺少 API Key", "请先填写 API Key。")
            return

        endpoint = model_catalog_endpoint(base_url)
        request = QNetworkRequest(QUrl(endpoint))
        request.setRawHeader(b"Accept", b"application/json")
        header_name, header_value = llm_api_key_header(
            self._api_auth_style, api_key
        )
        request.setRawHeader(
            header_name.encode("ascii"), header_value.encode("utf-8")
        )
        request.setTransferTimeout(15_000)
        reply = self._model_network_manager.get(request)
        self._model_reply = reply
        self.fetch_models_button.setEnabled(False)
        self.fetch_models_button.setText("获取中…")
        self.model_status_label.setText(f"正在读取 {endpoint}")
        reply.finished.connect(
            lambda current_reply=reply: self._finish_model_fetch(current_reply)
        )

    def _finish_model_fetch(self, reply: QNetworkReply) -> None:
        if self._model_reply is not reply:
            reply.deleteLater()
            return
        try:
            status_value = reply.attribute(
                QNetworkRequest.Attribute.HttpStatusCodeAttribute
            )
            status = int(status_value) if status_value is not None else 0
            if reply.error() != QNetworkReply.NetworkError.NoError:
                hints = {
                    401: "API Key 无效或没有读取模型列表的权限。",
                    403: "服务拒绝访问模型列表。",
                    404: "服务没有提供 /models 接口，请手动输入模型名。",
                    429: "请求过多或额度受限，请稍后重试。",
                }
                detail = hints.get(status, reply.errorString())
                raise RuntimeError(f"获取模型失败（HTTP {status or '未知'}）：{detail}")
            try:
                payload = json.loads(bytes(reply.readAll()).decode("utf-8"))
                models = parse_model_catalog(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise RuntimeError(f"模型列表响应无法识别：{exc}") from exc
            if not models:
                raise RuntimeError("服务返回了空模型列表，请手动输入模型名。")
            self._apply_available_models(models)
        except RuntimeError as exc:
            self.model_status_label.setText(str(exc))
            QMessageBox.warning(self, "无法获取模型", str(exc))
        finally:
            if self._model_reply is reply:
                self._model_reply = None
            self.fetch_models_button.setEnabled(True)
            self.fetch_models_button.setText("刷新模型")
            reply.deleteLater()

    def _apply_available_models(self, models: list[str]) -> None:
        current = self.model_edit.text().strip()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(models)
        if current:
            self.model_combo.setEditText(current)
        elif models:
            self.model_combo.setCurrentIndex(0)
        self.model_combo.blockSignals(False)
        self.model_status_label.setText(f"已获取 {len(models)} 个模型，可从下拉列表选择")

    def save_and_accept(self) -> None:
        settings = LLMSettings(
            base_url=self.base_url_edit.text().strip() or DEFAULT_PROVIDER.base_url,
            model=self.model_edit.text().strip() or DEFAULT_PROVIDER.model,
            api_auth_style=self._api_auth_style,
            send_temperature=self._send_temperature,
            enable_thinking=self._enable_thinking,
            max_chars=self.max_chars_edit.value(),
            prompt_template=self.prompt_edit.toPlainText().strip(),
            translation_output_dir=self.translation_output_dir_edit.text().strip(),
            translation_dual_output=self.dual_output_checkbox.isChecked(),
            confirm_external_requests=self.confirm_external_checkbox.isChecked(),
            require_source_evidence=self.require_source_evidence_checkbox.isChecked(),
            distinguish_inference=self.distinguish_inference_checkbox.isChecked(),
            leave_missing_blank=self.leave_missing_blank_checkbox.isChecked(),
            evidence_sections=[
                name
                for name, checkbox in self.evidence_section_checkboxes.items()
                if checkbox.isChecked()
            ],
            evidence_types=[
                name
                for name, checkbox in self.evidence_type_checkboxes.items()
                if checkbox.isChecked()
            ],
        )
        try:
            self._sync_api_key_scope()
            self._remember_current_api_key()
            settings.save(self.settings_path)
            for base_url, api_key in self._api_key_cache.items():
                if base_url:
                    set_llm_api_key(api_key, base_url)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.accept()

    def choose_translation_output_dir(self) -> None:
        start_dir = self.translation_output_dir_edit.text().strip()
        if not start_dir:
            start_dir = str(self.settings_path.parent / "translations")
        directory = QFileDialog.getExistingDirectory(
            self,
            "选择翻译输出目录",
            start_dir,
        )
        if directory:
            self.translation_output_dir_edit.setText(directory)

    def reset_prompt(self) -> None:
        self.prompt_edit.setPlainText(DEFAULT_PROMPT_TEMPLATE)

    def done(self, result: int) -> None:
        if self._model_reply is not None:
            self._model_reply.abort()
            self._model_reply.deleteLater()
            self._model_reply = None
        self._save_splitter_state()
        super().done(result)

    @staticmethod
    def _workflow_style() -> str:
        return """
            QDialog#AIWorkflowDialog { background: #fffdf8; color: #243447; }
            QFrame#AIWorkflowHeader, QFrame#AIWorkflowFooter {
                background: #fffdf8; border: none;
            }
            QFrame#AIWorkflowHeader { border-bottom: 1px solid #e4edf2; }
            QFrame#AIWorkflowFooter { border-top: 1px solid #e4edf2; }
            QLabel#AIWorkflowTitle { font-size: 20px; font-weight: 800; color: #243447; }
            QLabel#AIPageTitle { font-size: 17px; font-weight: 800; color: #243447; }
            QLabel#AIWorkflowSubtitle { color: #718096; font-size: 9.5pt; }
            QLabel#AIWorkflowBody { color: #405365; line-height: 1.45; }
            QLabel#AISectionLabel { color: #334155; font-weight: 800; }
            QFrame#AIWorkflowRail { background: #f8fbfb; border-right: 1px solid #e1eaee; }
            QListWidget#AIWorkflowSteps { background: transparent; border: none; outline: 0; }
            QListWidget#AIWorkflowSteps::item {
                min-height: 48px; padding: 8px 10px; margin: 2px 0;
                border-radius: 9px; color: #526070;
            }
            QListWidget#AIWorkflowSteps::item:hover { background: #eefaf8; }
            QListWidget#AIWorkflowSteps::item:selected {
                background: #dff5f1; color: #0f766e; font-weight: 800;
            }
            QScrollArea#AIWorkflowScroll, QWidget#AIWorkflowContent,
            QStackedWidget#AIWorkflowPages { background: #fffdf8; border: none; }
            QFrame#AISettingsGroup {
                background: #ffffff; border: 1px solid #dfe8ee; border-radius: 10px;
            }
            QLabel#AIHintBox {
                background: #f0fdfa; color: #3d625d; border: 1px solid #c9eee8;
                border-radius: 9px; padding: 11px 13px;
            }
            QFrame#AIOutputPreview {
                background: #fbfcfd; border-left: 1px solid #e1eaee;
            }
            QLabel#AIPreviewRow, QLabel#AIOutputRow {
                background: #ffffff; color: #405365; border: 1px solid #e1e8ed;
                border-radius: 8px; padding: 11px 12px; line-height: 1.4;
            }
            QSplitter#AIWorkflowSplitter::handle { background: #edf2f4; width: 5px; }
            QSplitter#AIWorkflowSplitter::handle:hover { background: #8bdad2; }
            QLineEdit, QTextEdit, QSpinBox, QComboBox {
                background: #fbfcff; color: #1f2937; border: 1px solid #dce6ec;
                border-radius: 8px; padding: 7px 9px;
            }
            QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {
                background: #ffffff; border: 1px solid #14b8a6;
            }
            QCheckBox { color: #405365; spacing: 8px; min-height: 26px; }
            QPushButton { min-height: 32px; border-radius: 9px; padding: 5px 12px; font-weight: 700; }
            QPushButton#PrimaryButton { background: #0f9f91; color: #ffffff; border: 1px solid #0b8278; }
            QPushButton#PrimaryButton:hover { background: #0f766e; }
            QPushButton#PrimaryButton:disabled { background: #e2e8f0; color: #94a3b8; border: 1px solid #dde5ee; }
            QPushButton#GhostButton, QPushButton#TinyButton {
                background: #ffffff; color: #0f766e; border: 1px solid #bfe4df;
            }
            QPushButton#GhostButton:hover, QPushButton#TinyButton:hover { background: #ecfeff; }
            QToolButton#PasswordVisibilityButton {
                background: #ffffff; color: #526070; border: 1px solid #bfe4df;
                border-radius: 8px;
            }
            QToolButton#PasswordVisibilityButton:hover { background: #ecfeff; color: #0f766e; }
            QToolButton#PasswordVisibilityButton:checked { background: #dff5f1; color: #0f766e; }
        """
