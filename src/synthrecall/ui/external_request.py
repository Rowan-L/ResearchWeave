from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from synthrecall.settings import LLMSettings
from synthrecall.ui import theme
from synthrecall.ui.confirmation import ConfirmationDialog, DialogAction


class ExternalRequestDialog(ConfirmationDialog):
    def __init__(
        self,
        *,
        operation: str,
        service: str,
        model: str,
        content_summary: str,
        output_summary: str = "",
        parent: QWidget | None = None,
    ) -> None:
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(12)

        details = QFrame()
        details.setObjectName("ExternalRequestDetails")
        details_layout = QGridLayout(details)
        details_layout.setContentsMargins(14, 12, 14, 12)
        details_layout.setHorizontalSpacing(14)
        details_layout.setVerticalSpacing(9)

        rows = [
            ("服务", service or "未配置"),
            ("模型", model or "未配置"),
            ("发送内容", content_summary),
        ]
        if output_summary:
            rows.append(("输出", output_summary))
        for row, (label, value) in enumerate(rows):
            key_label = QLabel(label)
            key_label.setObjectName("ExternalRequestKey")
            key_label.setAlignment(
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
            )
            value_label = QLabel(value)
            value_label.setObjectName("ExternalRequestValue")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            details_layout.addWidget(key_label, row, 0)
            details_layout.addWidget(value_label, row, 1)
        details_layout.setColumnStretch(1, 1)

        self.remember_checkbox = QCheckBox("以后不再提示（可在“模型设置”中重新开启）")
        self.remember_checkbox.setChecked(False)
        self.remember_checkbox.setAccessibleDescription(
            "启用后，后续外部模型调用将不再显示发送内容和费用提醒"
        )

        content_layout.addWidget(details)
        content_layout.addWidget(self.remember_checkbox)

        super().__init__(
            window_title="确认外部调用",
            title=operation,
            message="论文内容将发送到第三方模型服务，并可能产生费用。",
            detail="请先核对服务、模型与发送范围；API Key 不会显示在此处。",
            eyebrow="外部模型调用",
            tone="warning",
            actions=(
                DialogAction("cancel", "取消"),
                DialogAction("confirm", "确认并开始", "primary", default=True),
            ),
            extra_widget=content,
            parent=parent,
        )
        self.setMinimumWidth(620)
        self.setMaximumWidth(720)
        self.setStyleSheet(
            theme.apply_to_qss(self.styleSheet())
            + """
                QFrame#ExternalRequestDetails {
                    background: #ffffff;
                    border: 1px solid #dfe8ee;
                    border-radius: 10px;
                }
                QLabel#ExternalRequestKey {
                    color: #7c8490;
                    font-family: "Microsoft YaHei UI", "Microsoft YaHei";
                    font-size: 9pt;
                    font-weight: 700;
                }
                QLabel#ExternalRequestValue {
                    color: #334155;
                    font-family: "Microsoft YaHei UI", "Microsoft YaHei";
                    font-size: 9.5pt;
                }
                QCheckBox {
                    color: #526070;
                    spacing: 8px;
                    font-family: "Microsoft YaHei UI", "Microsoft YaHei";
                    font-size: 9.5pt;
                }
            """
        )

    def skip_future_confirmations(self) -> bool:
        return self.remember_checkbox.isChecked()


def confirm_external_request(
    settings_path: Path,
    *,
    operation: str,
    content_summary: str,
    output_summary: str = "",
    parent: QWidget | None = None,
) -> bool:
    settings = LLMSettings.load(settings_path)
    if not settings.confirm_external_requests:
        return True
    dialog = ExternalRequestDialog(
        operation=operation,
        service=settings.base_url,
        model=settings.model,
        content_summary=content_summary,
        output_summary=output_summary,
        parent=parent,
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return False
    if dialog.skip_future_confirmations():
        settings.confirm_external_requests = False
        settings.save(settings_path)
    return True
