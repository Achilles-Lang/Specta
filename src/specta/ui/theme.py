"""Specta 暗色主题：色板、字体、状态语义与全局样式表。

规格 3.2 定义的全部 Token 集中在此，UI 其他模块只引用常量，不硬编码颜色。
第一版只提供暗色主题。
"""

from __future__ import annotations

from typing import Tuple

from PySide6.QtGui import QFont, QFontDatabase

# ---------------------------------------------------------------------------
# 色板（规格 3.2）
# ---------------------------------------------------------------------------
BG = "#101419"          # 主窗口背景
PANEL = "#171D25"       # 服务头 / 工具栏 / 设置
LOG_BG = "#0D1117"      # 日志编辑器
HOVER = "#222C38"       # Tab 与按钮悬停
BORDER = "#2B3542"      # 1px 分隔线
TEXT = "#E6EDF3"        # 主文字
SUBTEXT = "#93A4B8"     # 次文字
ACCENT = "#67B7FF"      # 强调：选中 Tab、主操作、搜索焦点
OK = "#57C785"          # 正常：运行事实、Agent 就绪
WARN = "#E7B65A"        # 过渡/警告：启停中、未知
ERR = "#F27681"         # 错误/危险：非零退出、Agent 异常、Stop
DISABLED_TEXT = "#5A6675"

# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------
UI_FONT_STACK = [
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "Segoe UI",
    "PingFang SC",
    "Noto Sans CJK SC",
    "sans-serif",
]
MONO_FONT_STACK = [
    "Cascadia Mono",
    "Consolas",
    "JetBrains Mono",
    "Sarasa Mono SC",
    "Noto Sans Mono CJK SC",
    "Courier New",
    "monospace",
]

BASE_FONT_SIZE = 13
SERVICE_NAME_SIZE = 16
BRAND_SIZE = 18

_cached_ui: dict = {}
_cached_mono: dict = {}


def _families() -> set:
    try:
        return set(QFontDatabase.families())
    except Exception:  # noqa: BLE001 - 无 GUI 环境
        return set()


def pick_family(stack) -> str:
    available = _families()
    for name in stack:
        if name in available:
            return name
    return stack[-1]


def ui_font(point_size: int = BASE_FONT_SIZE, bold: bool = False) -> QFont:
    key = ("ui", point_size, bold)
    if key in _cached_ui:
        font = QFont(_cached_ui[key])
    else:
        family = pick_family(UI_FONT_STACK)
        _cached_ui[key] = family
        font = QFont(family, point_size)
    font.setBold(bold)
    font.setPointSize(point_size)
    return font


def mono_font(point_size: int = BASE_FONT_SIZE, bold: bool = False) -> QFont:
    key = ("mono", point_size, bold)
    if key in _cached_mono:
        font = QFont(_cached_mono[key])
    else:
        family = pick_family(MONO_FONT_STACK)
        _cached_mono[key] = family
        font = QFont(family, point_size)
    font.setBold(bold)
    font.setPointSize(point_size)
    font.setStyleHint(QFont.Monospace)
    return font


# ---------------------------------------------------------------------------
# 状态语义（规格 3.4）
# ---------------------------------------------------------------------------
STOPPED_LABEL = "已停止"


def lifecycle_display(lifecycle: str, exit_code) -> Tuple[str, str]:
    """返回 (文本, 颜色)。状态必须同时有文本，不能只靠颜色区分。"""
    if lifecycle == "starting":
        return "启动中", WARN
    if lifecycle == "running":
        return "运行中", OK
    if lifecycle == "stopping":
        return "停止中", WARN
    if lifecycle == "failed":
        return "启动失败", ERR
    if lifecycle == "exited":
        if exit_code == 0:
            return "已退出 · 0", SUBTEXT
        if exit_code is None:
            return "已退出", SUBTEXT
        return f"异常退出 · {exit_code}", ERR
    return STOPPED_LABEL, SUBTEXT


def lifecycle_actions(lifecycle: str, exit_code) -> Tuple[bool, bool, bool]:
    """返回 (可启动, 可重启, 可停止)。"""
    if lifecycle == "starting":
        return False, False, True
    if lifecycle == "running":
        return False, True, True
    if lifecycle == "stopping":
        return False, False, False
    # stopped / exited / failed
    return True, True, False


ASSESS_LABEL = {
    "ready": "就绪",
    "error": "异常",
    "unknown": "未知",
}


def assessment_display(state) -> Tuple[str, str]:
    if state is None:
        return "未评估", SUBTEXT
    if state == "ready":
        return "就绪", OK
    if state == "error":
        return "异常", ERR
    return "未知", WARN


def port_display(observation) -> Tuple[str, str]:
    """端口三态：已监听 / 未监听 / 检查失败 / 被其他进程占用。"""
    if observation is None:
        return "未设置端口", SUBTEXT
    if observation.listening:
        if observation.owned_by_service:
            return f"端口 {observation.port} 已监听", OK
        return f"端口 {observation.port} 被其他进程占用", ERR
    return f"端口 {observation.port} 未监听", SUBTEXT


# ---------------------------------------------------------------------------
# 样式表
# ---------------------------------------------------------------------------
def build_stylesheet(font_size: int = BASE_FONT_SIZE) -> str:
    fs = font_size
    return f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: "{pick_family(UI_FONT_STACK)}";
    font-size: {fs}px;
}}
QMainWindow, QDialog {{ background-color: {BG}; }}

#TopBar {{
    background-color: {PANEL};
    border-bottom: 1px solid {BORDER};
}}
#BrandTitle {{ font-size: {BRAND_SIZE}px; font-weight: 600; color: {TEXT}; }}
#BrandSub {{ color: {SUBTEXT}; font-size: {fs}px; }}
#BridgeBadge {{
    color: {SUBTEXT};
    background-color: {BG};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 2px 10px;
}}

#ServiceTabBar {{
    background-color: {BG};
    border-bottom: 1px solid {BORDER};
}}
QPushButton#ServiceTab {{
    background-color: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 6px 12px;
    color: {SUBTEXT};
    text-align: left;
}}
QPushButton#ServiceTab:hover {{ background-color: {HOVER}; color: {TEXT}; }}
QPushButton#ServiceTab:checked {{
    color: {TEXT};
    border-bottom: 2px solid {ACCENT};
    background-color: {PANEL};
}}
QLabel#TabBadge {{
    background-color: {ERR};
    color: #10141A;
    border-radius: 7px;
    padding: 0 5px;
    font-size: 11px;
    font-weight: 600;
}}
QPushButton#TabScroll {{
    background-color: transparent;
    border: none;
    color: {SUBTEXT};
    padding: 2px 8px;
}}
QPushButton#TabScroll:hover {{ color: {TEXT}; background-color: {HOVER}; }}

#ServiceHeader {{
    background-color: {PANEL};
    border-bottom: 1px solid {BORDER};
}}
#ServiceName {{ font-size: {SERVICE_NAME_SIZE}px; font-weight: 600; }}
#HeaderMeta {{ color: {SUBTEXT}; }}
#HeaderPath {{ color: {SUBTEXT}; font-family: "{pick_family(MONO_FONT_STACK)}"; font-size: {max(11, fs - 1)}px; }}

#LogToolbar {{
    background-color: {PANEL};
    border-bottom: 1px solid {BORDER};
}}
#LogMeta {{ color: {SUBTEXT}; }}
QPlainTextEdit#LogView {{
    background-color: {LOG_BG};
    border: none;
    color: {TEXT};
    font-family: "{pick_family(MONO_FONT_STACK)}";
    font-size: {fs}px;
    selection-background-color: #2B4A6F;
    selection-color: {TEXT};
}}
QLineEdit {{
    background-color: {BG};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 8px;
    color: {TEXT};
    selection-background-color: #2B4A6F;
}}
QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
QComboBox {{
    background-color: {BG};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 8px;
    color: {TEXT};
    min-height: 22px;
}}
QComboBox:focus {{ border: 1px solid {ACCENT}; }}
QComboBox QAbstractItemView {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    selection-background-color: {HOVER};
    color: {TEXT};
}}
QPushButton {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 12px;
    color: {TEXT};
    min-height: 22px;
}}
QPushButton:hover {{ background-color: {HOVER}; }}
QPushButton:pressed {{ background-color: #1B232D; }}
QPushButton:disabled {{ color: {DISABLED_TEXT}; border-color: #222A34; background-color: #141A21; }}
QPushButton:focus {{ border: 1px solid {ACCENT}; }}
QPushButton#PrimaryBtn {{
    background-color: #1D3A57;
    border: 1px solid #2F5B85;
    color: #CFE6FF;
}}
QPushButton#PrimaryBtn:hover {{ background-color: #24486B; }}
QPushButton#DangerBtn {{
    background-color: #3A2126;
    border: 1px solid #6B3239;
    color: #FFC7CD;
}}
QPushButton#DangerBtn:hover {{ background-color: #4A2A30; }}
QPushButton#FlatBtn {{ background-color: transparent; border: none; color: {SUBTEXT}; padding: 4px 8px; }}
QPushButton#FlatBtn:hover {{ color: {TEXT}; background-color: {HOVER}; }}
QPushButton#FlatBtn:checked {{ color: {ACCENT}; }}

#StatusBar {{
    background-color: {PANEL};
    border-top: 1px solid {BORDER};
    color: {SUBTEXT};
}}
QLabel#StatusSep {{ color: {BORDER}; }}

#InlineError {{
    background-color: #3A2126;
    border: 1px solid #6B3239;
    border-radius: 6px;
    color: #FFD9DC;
    padding: 6px 8px;
}}
#EmptyTitle {{ font-size: {SERVICE_NAME_SIZE + 2}px; font-weight: 600; }}
#EmptyBody {{ color: {SUBTEXT}; }}

#Card {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
QLabel#FieldLabel {{ color: {SUBTEXT}; }}
QLabel#Hint {{ color: {SUBTEXT}; font-size: {max(11, fs - 1)}px; }}
QLabel#RequiredMark {{ color: {ERR}; }}

QScrollBar:vertical {{ background: {BG}; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #333F4D; border-radius: 6px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: #435264; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: {BG}; height: 12px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #333F4D; border-radius: 6px; min-width: 28px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

QToolTip {{
    background-color: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px 6px;
}}
QMenu {{ background-color: {PANEL}; border: 1px solid {BORDER}; }}
QMenu::item {{ padding: 6px 20px; color: {TEXT}; }}
QMenu::item:selected {{ background-color: {HOVER}; }}
QMenu::item:disabled {{ color: {DISABLED_TEXT}; }}
QCheckBox {{ color: {TEXT}; spacing: 6px; }}
QSpinBox {{
    background-color: {BG};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 6px;
    color: {TEXT};
}}
QTableWidget {{ background-color: {BG}; gridline-color: {BORDER}; border: 1px solid {BORDER}; }}
QHeaderView::section {{ background-color: {PANEL}; color: {SUBTEXT}; border: none; padding: 4px 6px; }}
"""


def dot_style(color: str, size: int = 10) -> str:
    return (
        f"background-color: {color}; border-radius: {size // 2}px; "
        f"min-width: {size}px; max-width: {size}px; "
        f"min-height: {size}px; max-height: {size}px;"
    )
