"""服务头部：名称、状态、Agent 判断、运行事实、启停按钮与详情展开。

只把用户意图转成信号，不直接调用 Manager；不直接操作 QProcess 或日志存储。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _uptime_text(started_at: Optional[str], lifecycle: str) -> str:
    if lifecycle not in ("running", "starting", "stopping"):
        return ""
    started = _parse_ts(started_at)
    if started is None:
        return ""
    seconds = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"已运行 {hours:02d}:{minutes:02d}:{secs:02d}"


class StatusDot(QLabel):
    def __init__(self, size: int = 10, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.set_color(theme.SUBTEXT, size)

    def set_color(self, color: str, size: int = 10) -> None:
        self.setStyleSheet(theme.dot_style(color, size))


class ServiceHeader(QFrame):
    start_requested = Signal(str)
    stop_requested = Signal(str)
    restart_requested = Signal(str)
    vertical_size_changed = Signal()

    def __init__(self, service_id: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("ServiceHeader")
        self._service_id = service_id
        self._details_open = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(4)

        # -- 第一行：名称 + 状态 + Agent 判断 + 按钮 -------------------------
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self._status_dot = StatusDot(10)
        row1.addWidget(self._status_dot)
        self._name_label = QLabel("—")
        self._name_label.setObjectName("ServiceName")
        row1.addWidget(self._name_label)
        self._lifecycle_label = QLabel("已停止")
        row1.addWidget(self._lifecycle_label)
        row1.addSpacing(10)
        self._assess_dot = StatusDot(8)
        row1.addWidget(self._assess_dot)
        self._assess_label = QLabel("Agent：未评估")
        self._assess_label.setObjectName("HeaderMeta")
        row1.addWidget(self._assess_label)
        row1.addStretch(1)

        self._start_btn = QPushButton("启动")
        self._start_btn.setObjectName("PrimaryBtn")
        self._start_btn.clicked.connect(lambda: self.start_requested.emit(self._service_id))
        self._restart_btn = QPushButton("重启")
        self._restart_btn.clicked.connect(lambda: self.restart_requested.emit(self._service_id))
        self._stop_btn = QPushButton("停止")
        self._stop_btn.setObjectName("DangerBtn")
        self._stop_btn.clicked.connect(lambda: self.stop_requested.emit(self._service_id))
        for btn in (self._start_btn, self._restart_btn, self._stop_btn):
            btn.setMinimumWidth(64)
            row1.addWidget(btn)
        root.addLayout(row1)

        # -- 第二行：PID / 端口 / 运行时长 -----------------------------------
        self._meta_label = QLabel("")
        self._meta_label.setObjectName("HeaderMeta")
        root.addWidget(self._meta_label)

        # -- 第三行：cwd + 详情展开 ------------------------------------------
        row3 = QHBoxLayout()
        row3.setSpacing(8)
        self._path_label = QLabel("")
        self._path_label.setObjectName("HeaderPath")
        self._path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row3.addWidget(self._path_label)
        row3.addStretch(1)
        self._details_btn = QPushButton("▸ 启动配置与 Agent 判断详情")
        self._details_btn.setObjectName("FlatBtn")
        self._details_btn.clicked.connect(self._toggle_details)
        row3.addWidget(self._details_btn)
        root.addLayout(row3)

        self._details_label = QLabel("")
        self._details_label.setObjectName("Hint")
        self._details_label.setWordWrap(True)
        self._details_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._details_label.hide()
        root.addWidget(self._details_label)

        # -- 行内错误 ---------------------------------------------------------
        self._error_row = QWidget()
        err_layout = QHBoxLayout(self._error_row)
        err_layout.setContentsMargins(0, 2, 0, 0)
        err_layout.setSpacing(6)
        self._error_label = QLabel("")
        self._error_label.setObjectName("InlineError")
        self._error_label.setWordWrap(True)
        self._error_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        err_layout.addWidget(self._error_label, 1)
        self._error_copy = QPushButton("复制诊断")
        self._error_copy.setObjectName("FlatBtn")
        self._error_copy.clicked.connect(self._copy_error)
        err_layout.addWidget(self._error_copy)
        self._error_close = QPushButton("✕")
        self._error_close.setObjectName("FlatBtn")
        self._error_close.setToolTip("关闭提示")
        self._error_close.clicked.connect(self.clear_error)
        err_layout.addWidget(self._error_close)
        self._error_row.hide()
        root.addWidget(self._error_row)

        self._last_error: Optional[Dict[str, Any]] = None
        self._snapshot: Optional[Dict[str, Any]] = None
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

    # ------------------------------------------------------------------
    def apply_snapshot(self, snap: Dict[str, Any]) -> None:
        self._snapshot = snap
        facts = snap.get("facts") or {}
        lifecycle = facts.get("lifecycle", "stopped")
        exit_code = facts.get("exit_code")

        self._name_label.setText(snap.get("name") or snap.get("service_id") or "")
        label, color = theme.lifecycle_display(lifecycle, exit_code)
        self._status_dot.set_color(color, 10)
        self._lifecycle_label.setText(label)
        self._lifecycle_label.setStyleSheet(f"color: {color};")

        assessment = snap.get("assessment")
        state = assessment.get("state") if assessment else None
        a_label, a_color = theme.assessment_display(state)
        self._assess_dot.set_color(a_color, 8)
        self._assess_label.setText(f"Agent：{a_label}")
        self._assess_label.setStyleSheet(f"color: {a_color};")

        can_start, can_restart, can_stop = theme.lifecycle_actions(lifecycle, exit_code)
        queued = snap.get("queued_operations") or 0
        busy = bool(snap.get("active_operation_id")) or bool(queued)
        self._start_btn.setEnabled(can_start and not busy)
        self._restart_btn.setEnabled(can_restart and not busy)
        self._stop_btn.setEnabled(can_stop)
        if busy:
            self._start_btn.setText("排队中…" if queued else "启动")
        else:
            self._start_btn.setText("启动")
        self._stop_btn.setToolTip("硬终止整组进程（taskkill /T /F + Job）")

        # 元信息
        parts = []
        pid = facts.get("pid")
        if pid:
            parts.append(f"PID {pid}")
        elif facts.get("launcher_pid") and lifecycle == "starting":
            parts.append(f"启动器 PID {facts.get('launcher_pid')}")
        for observation in facts.get("port_observations") or []:
            text, _ = theme.port_display(_Obs(observation))
            parts.append(text)
        for port in snap.get("ports") or []:
            if not any((o.get("port") == port) for o in (facts.get("port_observations") or [])):
                parts.append(f"端口 {port}")
        uptime = _uptime_text(facts.get("started_at"), lifecycle)
        if uptime:
            parts.append(uptime)
        if queued:
            parts.append(f"排队 {queued}")
        self._meta_label.setText(" · ".join(parts) if parts else "未运行")

        self._path_label.setText(snap.get("cwd") or "")
        self._details_label.setText(self._build_details(snap))

        if snap.get("active_operation_id") and not self._error_row.isVisible():
            pass

        self.vertical_size_changed.emit()

    # ------------------------------------------------------------------
    def _build_details(self, snap: Dict[str, Any]) -> str:
        lines = []
        launch = snap.get("launch") or {}
        if launch.get("mode") == "shell":
            lines.append(f"启动方式：命令行  {launch.get('command')}")
        elif launch.get("mode") == "exec":
            args = " ".join(launch.get("args") or [])
            lines.append(f"启动方式：程序与参数  {launch.get('program')} {args}".rstrip())
        lines.append(f"来源：{snap.get('origin')}    持久化：{'否（ephemeral）' if snap.get('ephemeral') else '是'}")
        ready = snap.get("ready_when")
        if ready:
            lines.append(f"就绪条件：{ready}")
        observation = (snap.get("facts") or {}).get("ready_observation")
        if observation:
            lines.append(f"就绪条件命中：{'是' if observation.get('matched') else '否'}    检查于 {observation.get('checked_at')}")
        assessment = snap.get("assessment")
        if assessment:
            lines.append(
                f"Agent 判断：{theme.ASSESS_LABEL.get(assessment.get('state'), assessment.get('state'))}"
                f"（{assessment.get('reported_at')}）  {assessment.get('reason') or ''}"
            )
        else:
            lines.append("Agent 判断：未评估")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    def _toggle_details(self) -> None:
        self._details_open = not self._details_open
        self._details_label.setVisible(self._details_open)
        self._details_btn.setText(
            "▾ 启动配置与 Agent 判断详情" if self._details_open else "▸ 启动配置与 Agent 判断详情"
        )
        self.vertical_size_changed.emit()

    def close_details(self) -> None:
        if self._details_open:
            self._toggle_details()

    def show_error(self, error: Dict[str, Any]) -> None:
        self._last_error = error
        if not error:
            self._error_row.hide()
            return
        text = error.get("message") or "操作失败"
        details = error.get("details") or {}
        extra = ""
        if details.get("owner_pids"):
            extra = f"  （占用 PID: {', '.join(str(p) for p in details['owner_pids'])}）"
        self._error_label.setText(f"{error.get('code', 'ERROR')}：{text}{extra}")
        self._error_row.show()
        self.vertical_size_changed.emit()

    def clear_error(self) -> None:
        self._last_error = None
        self._error_row.hide()
        self.vertical_size_changed.emit()

    def _copy_error(self) -> None:
        from PySide6.QtWidgets import QApplication

        if not self._last_error:
            return
        text = self._error_label.text()
        QApplication.clipboard().setText(text)


class _Obs:
    """把端口观察 dict 适配成 theme.port_display 需要的属性对象。"""

    def __init__(self, data: Dict[str, Any]) -> None:
        self.port = data.get("port")
        self.listening = bool(data.get("listening"))
        self.owned_by_service = bool(data.get("owned_by_service"))
        self.owner_pids = data.get("owner_pids") or []
