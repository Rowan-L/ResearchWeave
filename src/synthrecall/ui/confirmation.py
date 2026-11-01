from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from synthrecall.ui import theme

ActionAppearance = Literal["primary", "secondary", "danger", "danger-soft"]
DialogTone = Literal["question", "warning", "danger", "information"]


@dataclass(frozen=True, slots=True)
class DialogAction:
    key: str
    label: str
    appearance: ActionAppearance = "secondary"
    description: str = ""
    default: bool = False


class ConfirmationDialog(QDialog):
    """A consistent, accessible decision dialog for user-controlled actions."""

    def __init__(
        self,
        *,
        title: str,
        message: str,
        actions: tuple[DialogAction, ...],
        detail: str = "",
        eyebrow: str = "请确认",
        tone: DialogTone = "question",
        cancel_key: str = "cancel",
        window_title: str | None = None,
        extra_widget: QWidget | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not actions:
            raise ValueError("ConfirmationDialog requires at least one action")

        self._selected_action = cancel_key
        self._cancel_key = cancel_key
        self.setObjectName("ConfirmationDialog")
        self.setWindowTitle(window_title or title)
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setMaximumWidth(680)
        self.setSizeGripEnabled(False)
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)

        surface = QFrame()
        surface.setObjectName("ConfirmationSurface")
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(18)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(16)
        header_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        icon_frame = QFrame()
        icon_frame.setObjectName("ConfirmationIcon")
        icon_frame.setProperty("tone", tone)
        icon_frame.setFixedSize(52, 52)
        icon_layout = QVBoxLayout(icon_frame)
        icon_layout.setContentsMargins(0, 0, 0, 0)

        icon_label = QLabel()
        icon_label.setObjectName("ConfirmationIconGlyph")
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setPixmap(self._standard_icon(tone).pixmap(QSize(26, 26)))
        icon_label.setAccessibleName(self._tone_accessible_name(tone))
        icon_layout.addWidget(icon_label)

        copy_layout = QVBoxLayout()
        copy_layout.setContentsMargins(0, 0, 0, 0)
        copy_layout.setSpacing(5)

        eyebrow_label = QLabel(eyebrow)
        eyebrow_label.setObjectName("ConfirmationEyebrow")
        title_label = QLabel(title)
        title_label.setObjectName("ConfirmationTitle")
        title_label.setWordWrap(True)
        message_label = QLabel(message)
        message_label.setObjectName("ConfirmationMessage")
        message_label.setWordWrap(True)
        message_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        copy_layout.addWidget(eyebrow_label)
        copy_layout.addWidget(title_label)
        copy_layout.addWidget(message_label)
        header_layout.addWidget(icon_frame)
        header_layout.addLayout(copy_layout, 1)
        layout.addLayout(header_layout)

        if detail:
            detail_frame = QFrame()
            detail_frame.setObjectName("ConfirmationDetail")
            detail_layout = QVBoxLayout(detail_frame)
            detail_layout.setContentsMargins(14, 12, 14, 12)
            detail_label = QLabel(detail)
            detail_label.setObjectName("ConfirmationDetailText")
            detail_label.setWordWrap(True)
            detail_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            detail_layout.addWidget(detail_label)
            layout.addWidget(detail_frame)

        if extra_widget is not None:
            layout.addWidget(extra_widget)

        divider = QFrame()
        divider.setObjectName("ConfirmationDivider")
        divider.setFixedHeight(1)
        layout.addWidget(divider)

        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(10)
        button_layout.addStretch(1)
        default_button: QPushButton | None = None
        for action in actions:
            button = QPushButton(action.label)
            button.setProperty("appearance", action.appearance)
            button.setProperty("actionKey", action.key)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAccessibleName(action.label)
            if action.description:
                button.setAccessibleDescription(action.description)
                button.setToolTip(action.description)
            button.clicked.connect(
                lambda _checked=False, key=action.key: self._choose(key)
            )
            if action.default:
                button.setDefault(True)
                button.setAutoDefault(True)
                default_button = button
            else:
                button.setAutoDefault(False)
            button_layout.addWidget(button)
        layout.addLayout(button_layout)
        root_layout.addWidget(surface)

        self.setStyleSheet(theme.apply_to_qss(self._style_sheet()))
        self.setAccessibleName(title)
        self.setAccessibleDescription(message)
        self.adjustSize()
        if default_button is not None:
            default_button.setFocus(Qt.FocusReason.OtherFocusReason)


    def exec_action(self) -> str:
        self.exec()
        return self._selected_action

    def reject(self) -> None:
        self._selected_action = self._cancel_key
        super().reject()

    def _choose(self, key: str) -> None:
        self._selected_action = key
        if key == self._cancel_key:
            super().reject()
        else:
            super().accept()

    def _standard_icon(self, tone: DialogTone):
        pixmaps = {
            "question": QStyle.StandardPixmap.SP_MessageBoxQuestion,
            "warning": QStyle.StandardPixmap.SP_MessageBoxWarning,
            "danger": QStyle.StandardPixmap.SP_MessageBoxCritical,
            "information": QStyle.StandardPixmap.SP_MessageBoxInformation,
        }
        return self.style().standardIcon(pixmaps[tone])

    @staticmethod
    def _tone_accessible_name(tone: DialogTone) -> str:
        return {
            "question": "确认",
            "warning": "注意",
            "danger": "危险操作",
            "information": "提示",
        }[tone]

    @staticmethod
    def _style_sheet() -> str:
        return """
            QDialog#ConfirmationDialog {
                background: #fffdf8;
            }
            QFrame#ConfirmationSurface {
                background: #fffdf8;
                border: 1px solid #eaded5;
            }
            QFrame#ConfirmationIcon {
                background: #ecfdf5;
                border: 1px solid #a7f3d0;
                border-radius: 16px;
            }
            QFrame#ConfirmationIcon[tone="warning"] {
                background: #fffbeb;
                border-color: #fde68a;
            }
            QFrame#ConfirmationIcon[tone="danger"] {
                background: #fff1f2;
                border-color: #fecdd3;
            }
            QFrame#ConfirmationIcon[tone="information"] {
                background: #eff6ff;
                border-color: #bfdbfe;
            }
            QLabel#ConfirmationEyebrow {
                color: #0f766e;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei";
                font-size: 9pt;
                font-weight: 700;
            }
            QLabel#ConfirmationTitle {
                color: #1f2937;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei";
                font-size: 17pt;
                font-weight: 800;
            }
            QLabel#ConfirmationMessage {
                color: #5f6b7a;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei";
                font-size: 10pt;
                line-height: 1.45;
            }
            QFrame#ConfirmationDetail {
                background: #f8fafc;
                border: 1px solid #e2e8f0;
                border-radius: 10px;
            }
            QLabel#ConfirmationDetailText {
                color: #526070;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei";
                font-size: 9.5pt;
            }
            QFrame#ConfirmationDivider {
                background: #ece5df;
                border: none;
            }
            QDialog#ConfirmationDialog QPushButton {
                min-width: 88px;
                min-height: 36px;
                padding: 0 16px;
                border-radius: 10px;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei";
                font-size: 9.5pt;
                font-weight: 700;
            }
            QDialog#ConfirmationDialog QPushButton[appearance="secondary"] {
                background: #ffffff;
                color: #475569;
                border: 1px solid #d8e0e8;
            }
            QDialog#ConfirmationDialog QPushButton[appearance="secondary"]:hover {
                background: #f8fafc;
                border-color: #aab7c4;
            }
            QDialog#ConfirmationDialog QPushButton[appearance="primary"] {
                background: #0f9f91;
                color: #ffffff;
                border: 1px solid #0b8278;
            }
            QDialog#ConfirmationDialog QPushButton[appearance="primary"]:hover {
                background: #0f766e;
            }
            QDialog#ConfirmationDialog QPushButton[appearance="danger"] {
                background: #dc2626;
                color: #ffffff;
                border: 1px solid #b91c1c;
            }
            QDialog#ConfirmationDialog QPushButton[appearance="danger"]:hover {
                background: #b91c1c;
            }
            QDialog#ConfirmationDialog QPushButton[appearance="danger-soft"] {
                background: #fff7f7;
                color: #be123c;
                border: 1px solid #fecdd3;
            }
            QDialog#ConfirmationDialog QPushButton[appearance="danger-soft"]:hover {
                background: #ffe4e6;
                border-color: #fda4af;
            }
            QDialog#ConfirmationDialog QPushButton:focus {
                border: 2px solid #14b8a6;
            }
        """


def confirm_action(
    parent: QWidget,
    *,
    title: str,
    message: str,
    confirm_label: str,
    detail: str = "",
    destructive: bool = False,
) -> bool:
    dialog = ConfirmationDialog(
        title=title,
        message=message,
        detail=detail,
        eyebrow="不可撤销的操作" if destructive else "请确认",
        tone="danger" if destructive else "question",
        actions=(
            DialogAction("cancel", "取消", default=destructive),
            DialogAction(
                "confirm",
                confirm_label,
                "danger" if destructive else "primary",
                default=not destructive,
            ),
        ),
        parent=parent,
    )
    return dialog.exec_action() == "confirm"
