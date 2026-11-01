"""主题与外观设置(F10):深色模式、字号、界面密度的统一入口。

方案:浅色 QSS 保持原样零改动;深色模式在应用样式前对 QSS 做「按属性角色
的颜色变换」——background→暗色表面、color→亮色文字、border→暗色边框,
辅以精选覆盖表处理歧义色(如白底卡片与按钮白字)。代码内取色一律走
color(name),图表/搜索高亮等跟随主题;密度决定文献卡片行高,字号决定
全局字体 pt。
"""

from __future__ import annotations

import colorsys
import re
from pathlib import Path

from PySide6.QtCore import QSettings

THEME_LIGHT = "light"
THEME_DARK = "dark"

FONT_SIZES: dict[str, float] = {"small": 9.5, "standard": 10.0, "large": 11.0}
DENSITY_HEIGHTS: dict[str, int] = {"compact": 104, "standard": 126, "relaxed": 148}

_font_keys = {
    "small": "小(9.5pt)",
    "standard": "标准(10pt)",
    "large": "大(11pt)",
}
_density_keys = {
    "compact": "紧凑",
    "standard": "标准",
    "relaxed": "宽松",
}

_state: dict[str, object] = {
    "theme": THEME_LIGHT,
    "font_size": "standard",
    "density": "standard",
    "settings": None,
}


def configure(data_dir: Path) -> None:
    """启动时绑定 ui_state.ini 并恢复上次的外观设置。"""
    settings = QSettings(str(Path(data_dir) / "ui_state.ini"), QSettings.Format.IniFormat)
    theme_value = str(settings.value("appearance/theme", THEME_LIGHT))
    font_value = str(settings.value("appearance/font_size", "standard"))
    density_value = str(settings.value("appearance/density", "standard"))
    _state["settings"] = settings
    _state["theme"] = theme_value if theme_value in (THEME_LIGHT, THEME_DARK) else THEME_LIGHT
    _state["font_size"] = font_value if font_value in FONT_SIZES else "standard"
    _state["density"] = density_value if density_value in DENSITY_HEIGHTS else "standard"


def _settings() -> QSettings | None:
    return _state.get("settings")  # type: ignore[return-value]


def theme_mode() -> str:
    return str(_state["theme"])


def font_size_key() -> str:
    return str(_state["font_size"])


def density_key() -> str:
    return str(_state["density"])


def is_dark() -> bool:
    return theme_mode() == THEME_DARK


def set_theme_mode(mode: str) -> None:
    _state["theme"] = mode if mode in (THEME_LIGHT, THEME_DARK) else THEME_LIGHT
    settings = _settings()
    if settings is not None:
        settings.setValue("appearance/theme", _state["theme"])
        settings.sync()


def set_font_size(key: str) -> None:
    _state["font_size"] = key if key in FONT_SIZES else "standard"
    settings = _settings()
    if settings is not None:
        settings.setValue("appearance/font_size", _state["font_size"])
        settings.sync()


def set_density(key: str) -> None:
    _state["density"] = key if key in DENSITY_HEIGHTS else "standard"
    settings = _settings()
    if settings is not None:
        settings.setValue("appearance/density", _state["density"])
        settings.sync()


def font_size_label(key: str | None = None) -> str:
    return _font_keys.get(key or font_size_key(), _font_keys["standard"])


def density_label(key: str | None = None) -> str:
    return _density_keys.get(key or density_key(), _density_keys["standard"])


def base_font_pt() -> float:
    return FONT_SIZES.get(font_size_key(), 10.0)


def card_row_height() -> int:
    return DENSITY_HEIGHTS.get(density_key(), 126)


def card_min_height() -> int:
    return max(72, round(card_row_height() * 0.72))


# ---------------------------------------------------------------------------
# 深色变换引擎
# ---------------------------------------------------------------------------

# (角色, 小写十六进制) -> 深色值。规则变换前优先命中;只列歧义色与主结构色。
CURATED_DARK: dict[tuple[str, str], str] = {
    ("bg", "#fff7ed"): "#12161d",  # 窗口底(暖白)
    ("bg", "#fffdf8"): "#1a212a",  # 顶栏/面板
    ("bg", "#f1ece3"): "#222932",  # 顶部导航槽
    ("bg", "#f8fafc"): "#161c24",  # 次级底/状态栏
    ("bg", "#ffffff"): "#1f2731",  # 卡片/输入框
    ("bg", "#fbfcfd"): "#1e252e",
    ("bg", "#f6f8fb"): "#1b222b",
    ("bg", "#f3f6f9"): "#1a212a",
    ("bg", "#0f9f91"): "#0d9488",  # 主按钮
    ("bg", "#0f766e"): "#0f766e",  # 深青主按钮
    ("bg", "#ccfbf1"): "#134e4a",  # 树选中底
    ("bg", "#ecfeff"): "#0e2f2b",
    ("bg", "#f0fdfa"): "#122a26",
    ("bg", "#e8f5f3"): "#15352f",
    ("bg", "#edf8f5"): "#142f2a",
    ("bg", "#fde68a"): "#6b5518",  # 搜索高亮底
    ("color", "#ffffff"): "#f2f6fa",  # 按钮白字柔化
    ("color", "#fff7ed"): "#e7ecf3",
    ("color", "#ecfeff"): "#c9f2ec",
    ("color", "#f0fdfa"): "#c9f2ec",
    ("color", "#ccfbf1"): "#99f6e4",
}

_DECL_RE = re.compile(
    r"(?P<prop>background(?:-color)?|color|border(?:-(?:top|bottom|left|right))?(?:-color)?"
    r"|selection-background-color|selection-color|alternate-background-color)"
    r"\s*:\s*(?P<body>[^;{}]+)"
)
_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b")
_STOP_RE = re.compile(r"(stop\s*:\s*)(#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b)")


def _expand(hex_color: str) -> str:
    value = hex_color[1:]
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return "#" + value.lower()


def _hls_of(hex_color: str) -> tuple[float, float, float]:
    value = _expand(hex_color)[1:]
    r, g, b = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return h, l, s


def _hex_of(h: float, l: float, s: float) -> str:
    s = min(max(s, 0.0), 1.0)
    l = min(max(l, 0.0), 1.0)
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def _surface(hex_color: str) -> str:
    h, l, s = _hls_of(hex_color)
    if l >= 0.93:
        # 近白中性底:按亮度落进暗色表面梯度(窗口最暗,悬浮面最亮)
        if l >= 0.995:
            return "#1f2731"
        if l >= 0.975:
            return "#1c232c"
        if l >= 0.955:
            return "#1a212a"
        return "#171d25"
    if l >= 0.80:
        return _hex_of(h, 0.185 + (0.93 - l) * 0.35, min(s * 0.45, 0.30))
    if l >= 0.55:
        return _hex_of(h, 0.24, min(s * 0.40, 0.26))
    if s >= 0.18 and l <= 0.55:
        # 饱和强调色背景(主按钮等):保持色相,轻微提亮保证可点击感
        return _hex_of(h, min(max(l, 0.30), 0.44), s)
    return _hex_of(h, max(l, 0.26), min(s, 0.20))


def _text(hex_color: str) -> str:
    h, l, s = _hls_of(hex_color)
    if l >= 0.85 and s <= 0.35:
        return "#f2f6fa"
    if s >= 0.35 and 0.22 <= l <= 0.55:
        # 饱和强调文字(青/蓝/红/琥珀):同色相提亮到暗底可读区间;
        # 极暗色(l<0.22,如藏青正文)不算强调色,按普通深色文字处理
        return _hex_of(h, 0.66, min(max(s * 0.85, 0.40), 0.80))
    if l <= 0.30:
        return "#e7ecf3"
    if l <= 0.45:
        return "#cfd7e1"
    if l <= 0.60:
        return "#a6b2c1"
    return "#f2f6fa"


def _border(hex_color: str) -> str:
    h, l, s = _hls_of(hex_color)
    if l >= 0.80:
        return _hex_of(h, 0.26, min(s * 0.35, 0.18))
    if l >= 0.55:
        return _hex_of(h, 0.30, min(s * 0.35, 0.20))
    if s >= 0.18:
        # 强调边框(聚焦青/危险红):轻微提亮
        return _hex_of(h, min(max(l, 0.34), 0.52), s)
    return _hex_of(h, max(l, 0.30), min(s, 0.16))


_ROLE_TRANSFORM = {
    "bg": _surface,
    "text": _text,
    "border": _border,
}


def _role_for(prop: str) -> str:
    if prop == "color" or prop == "selection-color":
        return "text"
    if prop.startswith("border"):
        return "border"
    return "bg"


def _transform_declaration(match: re.Match[str]) -> str:
    prop = match.group("prop")
    body = match.group("body")
    role = _role_for(prop)
    transform = _ROLE_TRANSFORM[role]

    def _replace_one(hex_match: re.Match[str]) -> str:
        original = _expand(hex_match.group(0))
        curated = CURATED_DARK.get((role, original))
        if curated is None:
            curated = CURATED_DARK.get(("any", original))
        return curated if curated is not None else transform(original)

    return f"{prop}: {_HEX_RE.sub(_replace_one, body)}"


def _transform_stops(match: re.Match[str]) -> str:
    original = _expand(match.group(2))
    curated = CURATED_DARK.get(("bg", original))
    return match.group(1) + (curated if curated is not None else _surface(original))


def apply_to_qss(qss: str) -> str:
    """深色模式:按属性角色变换 QSS 颜色;浅色模式原样返回。"""
    if not is_dark():
        return qss
    without_stops = _STOP_RE.sub(_transform_stops, qss)
    return _DECL_RE.sub(_transform_declaration, without_stops)


# ---------------------------------------------------------------------------
# 代码内取色(图表/高亮/自绘控件)
# ---------------------------------------------------------------------------

CODE_COLORS: dict[str, dict[str, str]] = {
    "light": {
        "chart_track": "#e8eef4",
        "chart_text": "#475569",
        "chart_muted": "#7c8da1",
        "chart_axis": "#dbe4ee",
        "card_highlight_bg": "#fde68a",
        "card_highlight_fg": "#713f12",
        "tag_default": "#6b7280",
        "panel_bg": "#ffffff",
        "panel_border": "#e2e8f0",
        "tooltip_bg": "#334155",
        "tooltip_fg": "#f8fafc",
    },
    "dark": {
        "chart_track": "#2a3441",
        "chart_text": "#b6c2d0",
        "chart_muted": "#8494a8",
        "chart_axis": "#37414f",
        "card_highlight_bg": "#6b5518",
        "card_highlight_fg": "#ffe9a8",
        "tag_default": "#98a4b3",
        "panel_bg": "#1f2731",
        "panel_border": "#323b48",
        "tooltip_bg": "#e2e8f0",
        "tooltip_fg": "#1a212a",
    },
}


def color(name: str) -> str:
    return CODE_COLORS[theme_mode()].get(name) or CODE_COLORS["light"][name]


# QPalette 骨架(深色);浅色走 Qt 标准调色板。
DARK_PALETTE_TOKENS: dict[str, str] = {
    "window": "#12161d",
    "window_text": "#e7ecf3",
    "base": "#1f2731",
    "text": "#e7ecf3",
    "button": "#232b35",
    "button_text": "#e7ecf3",
    "highlight": "#0f9f91",
    "highlighted_text": "#ffffff",
    "tooltip_base": "#e2e8f0",
    "tooltip_text": "#1a212a",
    "placeholder": "#6b7684",
    "link": "#7ea6f7",
}
