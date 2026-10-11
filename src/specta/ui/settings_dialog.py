"""设置对话框：日志字体大小、上下文行数、关闭行为、MCP 配置复制、诊断目录。

第一版只提供暗色主题，故不出现主题选项。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..infrastructure.settings import (
    AppSettings,
    CLOSE_BEHAVIOR_EXIT,
    CLOSE_BEHAVIOR_TRAY,
    MAX_CONTEXT_LINES,
    MAX_FONT_SIZE,
    MIN_CONTEXT_LINES,
    MIN_FONT_SIZE,
    diagnostics_dir,
)

MCP_SERVER_KEY = "specta"


def build_mcp_config() -> str:
    """生成通用 MCP 配置片段；用户替换实际路径，不预设某平台配置文件结构。"""
    payload = {
        "mcpServers": {
            MCP_SERVER_KEY: {
                "command": sys.executable,
                "args": ["-m", "specta.bridge_main"],
            }
        }
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(520)
        self.setModal(True)
        self._settings = settings

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self._font_spin = QSpinBox()
        self._font_spin.setRange(MIN_FONT_SIZE, MAX_FONT_SIZE)
        self._font_spin.setValue(settings.font_size)
        self._font_spin.setSuffix(" px")
        form.addRow("日志字体大小", self._font_spin)

        self._context_spin = QSpinBox()
        self._context_spin.setRange(MIN_CONTEXT_LINES, MAX_CONTEXT_LINES)
        self._context_spin.setValue(settings.context_lines)
        self._context_spin.setSuffix(" 行")
        form.addRow("错误上下文行数", self._context_spin)

        self._close_combo = QComboBox()
        self._close_combo.addItem("隐藏到托盘（不停止服务）", CLOSE_BEHAVIOR_TRAY)
        self._close_combo.addItem("退出 Specta（停止全部服务）", CLOSE_BEHAVIOR_EXIT)
        index = self._close_combo.findData(settings.close_behavior)
        self._close_combo.setCurrentIndex(max(0, index))
        form.addRow("关闭窗口行为", self._close_combo)

        root.addLayout(form)

        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("主题"))
        theme_label = QLabel("暗色（第一版固定）")
        theme_label.setObjectName("Hint")
        theme_row.addWidget(theme_label)
        theme_row.addStretch(1)
        root.addLayout(theme_row)

        hint = QLabel(
            "MCP 配置：把下面的片段合并进你的 Agent 客户端配置；"
            "命令使用当前 Specta 的 Python 解释器，无需依赖工作目录。"
        )
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        row = QHBoxLayout()
        copy_mcp = QPushButton("复制 MCP 配置")
        copy_mcp.clicked.connect(self._copy_mcp)
        row.addWidget(copy_mcp)
        open_diag = QPushButton("打开诊断目录")
        open_diag.clicked.connect(self._open_diagnostics)
        row.addWidget(open_diag)
        self._status = QLabel("")
        self._status.setObjectName("Hint")
        row.addWidget(self._status)
        row.addStretch(1)
        root.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------------
    def _copy_mcp(self) -> None:
        QApplication.clipboard().setText(build_mcp_config())
        self._status.setText("MCP 配置已复制到剪贴板")

    def _open_diagnostics(self) -> None:
        path = diagnostics_dir()
        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:  # pragma: no cover
                subprocess.Popen(["xdg-open", str(path)])
            self._status.setText(f"已打开 {path}")
        except OSError as exc:
            self._status.setText(f"打开失败：{exc}")

    # ------------------------------------------------------------------
    def result_settings(self) -> AppSettings:
        return AppSettings(
            font_size=self._font_spin.value(),
            context_lines=self._context_spin.value(),
            close_behavior=self._close_combo.currentData() or CLOSE_BEHAVIOR_TRAY,
            current_service_id=self._settings.current_service_id,
            window_geometry=self._settings.window_geometry,
            window_state=self._settings.window_state,
            tray_hint_shown=self._settings.tray_hint_shown,
        ).normalized()
