"""F8 库统计面板:概览卡片 + 阅读状态/分类/年份/导入趋势四组纯 Qt 图表。

不引入 matplotlib 等图表依赖:比例条与柱状图均为自绘 QWidget,
配色与全局扁平青绿风格一致;数据由仓库层 library_statistics() 聚合。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from synthrecall.repositories import LibraryRepository
from synthrecall.ui import theme

ACCENT = "#0f9f91"
TRACK = "#e8eef4"
TEXT = "#475569"
MUTED = "#7c8da1"
BORDER = "#e2e8f0"

# 阅读状态语义色:灰=未开始,蓝=浅接触,青=完成,琥珀=深读,紫=进行中,绿=交付
STATUS_COLORS = {
    "待读": "#94a3b8",
    "浏览": "#60a5fa",
    "已读": "#0f9f91",
    "精读": "#f59e0b",
    "复现中": "#8b5cf6",
    "已复现": "#22c55e",
}

CATEGORY_TOP_N = 8


class ProportionalBar(QWidget):
    """水平比例条:按 ratio(0~1)填充圆角矩形,零值只画浅灰轨道。"""

    def __init__(self, color: str = ACCENT, bar_height: int = 12) -> None:
        super().__init__()
        self._color = QColor(color)
        self._ratio = 0.0
        self.setFixedHeight(bar_height)

    def set_ratio(self, ratio: float) -> None:
        self._ratio = max(0.0, min(1.0, ratio))
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        height = self.height()
        painter.setBrush(QColor(theme.color("chart_track")))
        painter.drawRoundedRect(QRectF(0, 0, self.width(), height), height / 2, height / 2)
        if self._ratio > 0.0:
            width = max(height, self.width() * self._ratio)
            painter.setBrush(self._color)
            painter.drawRoundedRect(QRectF(0, 0, width, height), height / 2, height / 2)
        painter.end()


class ColumnChartView(QWidget):
    """柱状图:items 为 (标签, 数值);柱顶数值、底部自适应省略标签。"""

    def __init__(self, minimum_height: int = 190) -> None:
        super().__init__()
        self._columns: list[tuple[str, int, str]] = []
        self.setMinimumHeight(minimum_height)

    def set_columns(self, items: list[tuple[str, int]], color: str = ACCENT) -> None:
        self._columns = [(label, value, color) for label, value in items]
        self.update()

    def columns(self) -> list[tuple[str, int]]:
        return [(label, value) for label, value, _color in self._columns]

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self._columns:
            painter.end()
            return
        width, height = self.width(), self.height()
        label_height, value_height, baseline_gap = 22, 18, 8
        baseline = height - label_height - baseline_gap
        plot_height = baseline - value_height
        count = len(self._columns)
        gap = max(6.0, width / 80.0)
        # 柱宽设上限:少量数据时不至于撑成粗笨的方块(年份图常只有 2-3 根)
        bar_width = min(64.0, max(4.0, (width - gap * (count + 1)) / count))
        if count * (bar_width + gap) < width:
            offset = (width - count * (bar_width + gap) + gap) / 2
        else:
            offset = gap
        max_value = max(value for _label, value, _color in self._columns) or 1

        font = QFont(self.font())
        font.setPointSizeF(max(7.5, self.font().pointSizeF() - 1))
        painter.setFont(font)
        metrics = painter.fontMetrics()

        axis_pen = QPen(QColor(theme.color("chart_axis")))
        axis_pen.setWidth(1)
        painter.setPen(axis_pen)
        painter.drawLine(0, baseline, width, baseline)

        # 轴标签放不下时隔列显示(GLM 验收:900px 宽 12 个月标签全截断成 "25-…");
        # 展示的标签用两列宽度绘制,保证完整可读
        col_span = bar_width + gap
        any_overflow = any(
            metrics.horizontalAdvance(label) > int(col_span + gap - 6)
            for label, _value, _color in self._columns
        )
        skip_odd = any_overflow and count >= 6

        painter.setPen(Qt.PenStyle.NoPen)
        for index, (label, value, color) in enumerate(self._columns):
            x = offset + index * (bar_width + gap)
            bar_height = int(plot_height * value / max_value) if value else 0
            bar_height = max(bar_height, 3 if value else 0)
            radius = min(4.0, bar_width / 3)
            painter.setBrush(
                QColor(color if value else theme.color("chart_track"))
            )
            painter.drawRoundedRect(
                QRectF(x, baseline - bar_height, bar_width, bar_height), radius, radius
            )
            # 零值柱不画数值标签:趋势图一排灰 0 是噪点,轴标签已表达空月份
            if value:
                painter.setPen(QColor(theme.color("chart_text")))
                value_rect = QRectF(
                    x - gap / 2, baseline - bar_height - value_height, bar_width + gap, value_height
                )
                painter.drawText(
                    value_rect,
                    Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                    str(value),
                )
            painter.setPen(QColor(theme.color("chart_muted")))
            if skip_odd and index % 2 == 1:
                painter.setPen(Qt.PenStyle.NoPen)
                continue
            label_span = col_span * 2 if skip_odd else col_span + gap
            label_rect = QRectF(
                x - (label_span - bar_width) / 2,
                baseline + baseline_gap,
                label_span,
                label_height,
            )
            text = metrics.elidedText(label, Qt.TextElideMode.ElideRight, int(label_span))
            painter.drawText(
                label_rect,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                text,
            )
            painter.setPen(Qt.PenStyle.NoPen)
        painter.end()


def _section(title: str) -> tuple[QFrame, QVBoxLayout, QLabel]:
    frame = QFrame()
    frame.setStyleSheet(
        f"QFrame {{ background: {theme.color('panel_bg')}; "
        f"border: 1px solid {theme.color('panel_border')}; border-radius: 12px; }}"
    )
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(8)
    title_label = QLabel(title)
    title_label.setStyleSheet(
        f"color: {theme.color('chart_text')}; font-size: 13px; "
        "font-weight: 700; border: none; background: transparent;"
    )
    layout.addWidget(title_label)
    return frame, layout, title_label


def _card(title: str, value: int, accent: str, subtitle: str = "") -> QFrame:
    frame = QFrame()
    frame.setMinimumHeight(72)
    frame.setStyleSheet(
        f"QFrame {{ background: {theme.color('panel_bg')}; "
        f"border: 1px solid {theme.color('panel_border')}; border-radius: 12px; }}"
    )
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 10, 14, 10)
    layout.setSpacing(2)
    caption = QLabel(title)
    caption.setStyleSheet(
        f"color: {theme.color('chart_muted')}; font-size: 12px; "
        "border: none; background: transparent;"
    )
    number = QLabel(str(value))
    number.setStyleSheet(
        f"color: {accent}; font-size: 22px; font-weight: 800; border: none; background: transparent;"
    )
    layout.addWidget(caption)
    layout.addWidget(number)
    if subtitle:
        note = QLabel(subtitle)
        note.setStyleSheet(
            f"color: {theme.color('chart_muted')}; font-size: 11px; "
            "border: none; background: transparent;"
        )
        layout.addWidget(note)
    frame.value_label = number  # type: ignore[attr-defined]
    return frame


class StatisticsDialog(QDialog):
    """库统计:概览卡 + 阅读状态/分类分布(比例条) + 年份/导入趋势(柱状图)。"""

    def __init__(self, repository: LibraryRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.repository = repository
        self.setWindowTitle("库统计")
        self.resize(900, 660)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(self.scroll)

        self.content = QWidget()
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(16, 16, 16, 16)
        content_layout.setSpacing(14)
        self.scroll.setWidget(self.content)

        self.overview_row = QHBoxLayout()
        self.overview_row.setSpacing(12)
        content_layout.addLayout(self.overview_row)

        self.empty_state = QFrame()
        self.empty_state.setObjectName("PaperListEmptyState")
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_title = QLabel("库还是空的")
        empty_title.setObjectName("EmptyStateTitle")
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_hint = QLabel("导入第一批文献后,这里会展示阅读状态、分类、年份与导入趋势统计。")
        empty_hint.setObjectName("EmptyStateHint")
        empty_hint.setWordWrap(True)
        empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_title)
        empty_layout.addWidget(empty_hint)
        content_layout.addWidget(self.empty_state, 1)

        self.charts_grid = QGridLayout()
        self.charts_grid.setSpacing(12)

        status_frame, status_layout, _status_title = _section("阅读状态分布")
        self.status_rows_host = QWidget()
        self.status_rows_layout = QVBoxLayout(self.status_rows_host)
        self.status_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.status_rows_layout.setSpacing(6)
        status_layout.addWidget(self.status_rows_host, 1)
        self.charts_grid.addWidget(status_frame, 0, 0)

        category_frame, category_layout, self.category_title = _section("分类分布")
        self.category_rows_host = QWidget()
        self.category_rows_layout = QVBoxLayout(self.category_rows_host)
        self.category_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.category_rows_layout.setSpacing(6)
        category_layout.addWidget(self.category_rows_host, 1)
        self.charts_grid.addWidget(category_frame, 0, 1)

        year_frame, year_layout, _year_title = _section("年份分布")
        self.year_chart = ColumnChartView()
        year_layout.addWidget(self.year_chart)
        self.charts_grid.addWidget(year_frame, 1, 0)

        trend_frame, trend_layout, _trend_title = _section("近 12 个月导入趋势")
        self.trend_chart = ColumnChartView()
        trend_layout.addWidget(self.trend_chart)
        self.charts_grid.addWidget(trend_frame, 1, 1)

        self.charts_grid.setColumnStretch(0, 1)
        self.charts_grid.setColumnStretch(1, 1)
        content_layout.addLayout(self.charts_grid, 1)

        # F20:AI Token 用量(今日/近7天/近30天/累计)——独立于文献数,
        # 空库也有意义,放在图表区外
        token_frame, token_layout, _token_title = _section("AI Token 用量")
        self.token_summary_label = QLabel("")
        self.token_summary_label.setWordWrap(True)
        token_layout.addWidget(self.token_summary_label)
        content_layout.addWidget(token_frame)

        # F22:真实阅读时长统计 + 近 14 天柱状图
        reading_frame, reading_layout, _reading_title = _section("阅读时长")
        self.reading_summary_label = QLabel("")
        self.reading_summary_label.setWordWrap(True)
        reading_layout.addWidget(self.reading_summary_label)
        self.reading_chart = ColumnChartView()
        reading_layout.addWidget(self.reading_chart)
        content_layout.addWidget(reading_frame)

        self.status_bars: dict[str, ProportionalBar] = {}
        self.status_value_labels: dict[str, QLabel] = {}
        self.category_bars: list[ProportionalBar] = []
        self.refresh()

    def refresh(self) -> None:
        stats = self.repository.library_statistics()
        has_papers = stats.total_papers > 0
        self.empty_state.setVisible(not has_papers)
        self.charts_grid.setEnabled(has_papers)
        self._refresh_token_summary()
        self._refresh_reading_summary()
        for index in range(self.overview_row.count()):
            item = self.overview_row.itemAt(index)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self.overview_row.addStretch(0)

        deep_percent = (
            round(stats.deep_reading_count * 100 / stats.total_papers) if stats.total_papers else 0
        )
        pending = dict(stats.reading_status_counts).get("待读", 0)
        # 五张卡统一带副标题行,保证数字基线对齐(单卡多一行会压低数字)
        cards = (
            ("文献总数", stats.total_papers, theme.color("chart_text"), "正常库在册文献"),
            (
                "含 PDF",
                stats.pdf_count,
                ACCENT,
                f"占 {round(stats.pdf_count * 100 / stats.total_papers)}%" if stats.total_papers else "—",
            ),
            ("待读", pending, STATUS_COLORS["待读"], f"占 {round(pending * 100 / stats.total_papers)}%" if stats.total_papers else "—"),
            (
                "深度阅读",
                stats.deep_reading_count,
                STATUS_COLORS["精读"] if stats.deep_reading_count else theme.color("chart_muted"),
                f"精读+已复现 · 占 {deep_percent}%",
            ),
            ("回收站", stats.trashed_count, theme.color("chart_muted"), "可从左侧恢复"),
        )
        # stretch=1 使 5 张卡等宽,避免长标签把卡撑得参差
        for title, value, accent, subtitle in cards:
            self.overview_row.insertWidget(
                self.overview_row.count() - 1, _card(title, value, accent, subtitle), 1
            )

        # 阅读状态/分类行:整体重灌(数据量小,无差量刷新必要)
        while self.status_rows_layout.count():
            item = self.status_rows_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.status_bars.clear()
        self.status_value_labels.clear()
        if stats.reading_status_counts:
            total = stats.total_papers or 1
            for status, count in stats.reading_status_counts:
                row = QHBoxLayout()
                row.setSpacing(8)
                name = QLabel(status)
                name.setFixedWidth(64)
                name.setStyleSheet(f"color: {theme.color('chart_text')}; font-size: 12px;")
                bar = ProportionalBar(STATUS_COLORS.get(status, ACCENT))
                bar.set_ratio(count / total)
                value = QLabel(f"{count} 篇")
                value.setFixedWidth(52)
                value.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
                row.addWidget(name)
                row.addWidget(bar, 1)
                row.addWidget(value)
                host = QWidget()
                host.setLayout(row)
                self.status_rows_layout.addWidget(host)
                self.status_bars[status] = bar
                self.status_value_labels[status] = value
        else:
            hint = QLabel("暂无文献")
            hint.setStyleSheet(f"color: {theme.color('chart_muted')};")
            self.status_rows_layout.addWidget(hint)
        self.status_rows_layout.addStretch(1)

        while self.category_rows_layout.count():
            item = self.category_rows_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.category_bars = []
        total_categories = len(stats.category_counts)
        if total_categories > CATEGORY_TOP_N:
            self.category_title.setText(
                f"分类分布(前 {CATEGORY_TOP_N} 类 · 共 {total_categories} 类)"
            )
        else:
            self.category_title.setText(f"分类分布(共 {total_categories} 类)" if total_categories else "分类分布")
        top_categories = stats.category_counts[:CATEGORY_TOP_N]
        if top_categories:
            top_count = top_categories[0][1] or 1
            for name, count in top_categories:
                row = QHBoxLayout()
                row.setSpacing(8)
                label = QLabel()
                label.setFixedWidth(150)
                label.setToolTip(f"{name}:{count} 篇")
                label.setStyleSheet(f"color: {theme.color('chart_text')}; font-size: 12px;")
                label.setText(
                    label.fontMetrics().elidedText(
                        name, Qt.TextElideMode.ElideRight, 148
                    )
                )
                bar = ProportionalBar(ACCENT)
                bar.set_ratio(count / top_count)
                value = QLabel(f"{count}")
                value.setFixedWidth(36)
                value.setStyleSheet(f"color: {theme.color('chart_muted')}; font-size: 12px;")
                row.addWidget(label)
                row.addWidget(bar, 1)
                row.addWidget(value)
                host = QWidget()
                host.setLayout(row)
                self.category_rows_layout.addWidget(host)
                self.category_bars.append(bar)
        else:
            hint = QLabel("暂无分类文献")
            hint.setStyleSheet(f"color: {theme.color('chart_muted')};")
            self.category_rows_layout.addWidget(hint)
        self.category_rows_layout.addStretch(1)

        if has_papers:
            self.year_chart.set_columns(
                [(str(year), count) for year, count in stats.year_counts]
            )
            # 月份标签用短格式 "25-11":12 列下完整 "2025-11" 会被省略成 "202…"
            self.trend_chart.set_columns(
                [(month[2:], count) for month, count in stats.import_month_counts]
            )
        else:
            self.year_chart.set_columns([])
            self.trend_chart.set_columns([])

    def _refresh_token_summary(self) -> None:
        """F20:今日/近7天/近30天/累计四桶 + 按功能细分。"""
        summary = self.repository.token_usage_summary()
        total = summary["total"]["total"]
        if total <= 0:
            self.token_summary_label.setText(
                "还没有 AI 调用记录。AI 解析、摘要翻译、PDF 翻译的 token 用量会汇总在这里。"
            )
            self.token_summary_label.setStyleSheet(
                f"color: {theme.color('chart_muted')};"
            )
            return

        def _line(name: str, bucket: dict) -> str:
            note = f"(其中估算 {bucket['estimated']:,})" if bucket["estimated"] else ""
            return f"{name}  {bucket['total']:,} {note}".rstrip()

        text = "\n".join(
            [
                _line("今日", summary["today"]),
                _line("近 7 天", summary["week"]),
                _line("近 30 天", summary["month"]),
                _line("累计", summary["total"]),
            ]
        )
        features = summary.get("features") or {}
        if features:
            breakdown = " · ".join(
                f"{name} {count:,}" for name, count in list(features.items())[:6]
            )
            text += f"\n按功能:{breakdown}"
        prompt_share = (
            f"Prompt {summary['total']['prompt']:,} / Completion {summary['total']['completion']:,}"
        )
        text += f"\n{prompt_share}"
        self.token_summary_label.setText(text)
        self.token_summary_label.setStyleSheet(
            f"color: {theme.color('chart_text')};"
        )

    def _refresh_reading_summary(self) -> None:
        """F22:真实阅读时长——四桶汇总 + 近 14 天柱状图(分钟)。"""
        summary = self.repository.reading_time_summary()

        def _fmt(seconds: int) -> str:
            if seconds >= 3600:
                return f"{seconds / 3600:.1f} 小时"
            if seconds >= 60:
                return f"{seconds // 60} 分钟"
            return f"{seconds} 秒"

        total = summary["total"]
        if total <= 0:
            self.reading_summary_label.setText(
                "还没有阅读记录。进入阅读页的实际阅读时长会按天累计在这里。"
            )
            self.reading_summary_label.setStyleSheet(
                f"color: {theme.color('chart_muted')};"
            )
            self.reading_chart.set_columns([])
            return
        text = (
            f"今日 {_fmt(summary['today'])} · 近 7 天 {_fmt(summary['week'])}"
            f" · 近 30 天 {_fmt(summary['month'])} · 累计 {_fmt(total)}"
        )
        self.reading_summary_label.setText(text)
        self.reading_summary_label.setStyleSheet(
            f"color: {theme.color('chart_text')};"
        )
        self.reading_chart.set_columns(
            [(label, seconds // 60) for label, seconds in summary["series"]]
        )
