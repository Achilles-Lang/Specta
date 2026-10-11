"""主窗口：服务 Tab、全局操作、状态栏与托盘。

只把用户意图转换为 ServiceManager 调用；不直接操作 QProcess 或日志存储。
日志刷新由主窗口单一 50ms 定时器驱动，且只刷新当前可见 Tab（隐藏 Tab 仍在收集）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFontMetrics, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from ..application.service_manager import ServiceManager
from ..domain.errors import ErrorCode, SpectaError
from ..domain.models import (
    ACTION_RESTART,
    ACTION_START,
    ACTION_STOP,
    ServiceSpec,
)
from ..infrastructure.settings import AppSettings, save_settings
from ..logs import export as log_export
from ..logs import query as log_query
from . import theme
from .service_editor import ServiceEditorDialog
from .service_header import ServiceHeader
from .service_log_view import ServiceLogView
from .settings_dialog import SettingsDialog, build_mcp_config

REFRESH_MS = 50
RESIZE_DEBOUNCE_MS = 150

FILTER_ALL = "全部"
FILTER_ERROR = "仅错误"


# ---------------------------------------------------------------------------
# Tab
# ---------------------------------------------------------------------------
class ServiceTab(QPushButton):
    context_menu_requested = Signal(str)

    def __init__(self, service_id: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("ServiceTab")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self._service_id = service_id
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)
        self._dot = QLabel()
        self._dot.setFixedSize(9, 9)
        self._dot.setStyleSheet(theme.dot_style(theme.SUBTEXT, 9))
        layout.addWidget(self._dot)
        self._name = QLabel(service_id)
        layout.addWidget(self._name)
        self._badge = QLabel("")
        self._badge.setObjectName("TabBadge")
        self._badge.hide()
        layout.addWidget(self._badge)

    @property
    def service_id(self) -> str:
        return self._service_id

    def update_state(self, name: str, lifecycle: str, exit_code, error_count: int, unviewed: bool) -> None:
        self._name.setText(name)
        _label, color = theme.lifecycle_display(lifecycle, exit_code)
        self._dot.setStyleSheet(theme.dot_style(color, 9))
        if unviewed and error_count > 0:
            self._badge.setText(f"错误 {error_count}")
            self._badge.show()
        else:
            self._badge.hide()
        self.setToolTip(f"{name}（{_label}）")
        self.adjustSize()


class TabStrip(QWidget):
    selected = Signal(str)
    context_menu_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("ServiceTabBar")
        self.setFixedHeight(40)
        root = QHBoxLayout(self)
        root.setContentsMargins(4, 0, 4, 0)
        root.setSpacing(2)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._holder = QWidget()
        self._holder.setStyleSheet("background: transparent;")
        self._row = QHBoxLayout(self._holder)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(2)
        self._row.addStretch(1)
        self._scroll.setWidget(self._holder)
        root.addWidget(self._scroll, 1)

        self._left = QPushButton("‹")
        self._left.setObjectName("TabScroll")
        self._left.setFixedWidth(28)
        self._left.clicked.connect(lambda: self._scroll_by(-160))
        root.addWidget(self._left)
        self._right = QPushButton("›")
        self._right.setObjectName("TabScroll")
        self._right.setFixedWidth(28)
        self._right.clicked.connect(lambda: self._scroll_by(160))
        root.addWidget(self._right)

        self._tabs: Dict[str, ServiceTab] = {}
        self._order: List[str] = []
        self._current: Optional[str] = None

    def add_tab(self, service_id: str, name: str) -> ServiceTab:
        tab = ServiceTab(service_id)
        tab.update_state(name, "stopped", None, 0, False)
        tab.clicked.connect(lambda: self.selected.emit(service_id))
        tab.setContextMenuPolicy(Qt.CustomContextMenu)
        tab.customContextMenuRequested.connect(
            lambda pos, sid=service_id, t=tab: self._show_menu(t, pos, sid)
        )
        self._row.insertWidget(self._row.count() - 1, tab)
        self._tabs[service_id] = tab
        self._order.append(service_id)
        return tab

    def remove_tab(self, service_id: str) -> None:
        tab = self._tabs.pop(service_id, None)
        if tab is not None:
            self._row.removeWidget(tab)
            tab.deleteLater()
        if service_id in self._order:
            self._order.remove(service_id)
        if self._current == service_id:
            self._current = None

    def set_current(self, service_id: str) -> None:
        self._current = service_id
        for sid, tab in self._tabs.items():
            tab.setChecked(sid == service_id)

    def tab(self, service_id: str) -> Optional[ServiceTab]:
        return self._tabs.get(service_id)

    def order(self) -> List[str]:
        return list(self._order)

    def _scroll_by(self, dx: int) -> None:
        bar = self._scroll.horizontalScrollBar()
        bar.setValue(bar.value() + dx)

    def _show_menu(self, tab: ServiceTab, pos, service_id: str) -> None:
        self.context_menu_requested.emit(service_id)


# ---------------------------------------------------------------------------
# 每服务工作区
# ---------------------------------------------------------------------------
class ServicePage(QWidget):
    def __init__(self, service_id: str, manager: ServiceManager, settings: AppSettings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._service_id = service_id
        self._manager = manager
        self._settings = settings

        self._logs_dirty = True
        self._facts_dirty = True
        self._forced_full = True
        self._filter_error = False
        self._keyword = ""
        self._only_matches = False
        self._current_match = -1
        self._auto_scroll = True
        self._last_snapshot: Optional[Dict[str, Any]] = None
        self._view_records: List[Any] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.header = ServiceHeader(service_id)
        self.header.start_requested.connect(self._on_start)
        self.header.stop_requested.connect(self._on_stop)
        self.header.restart_requested.connect(self._on_restart)
        root.addWidget(self.header)

        # -- 日志工具栏 ------------------------------------------------------
        toolbar = QWidget()
        toolbar.setObjectName("LogToolbar")
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(10, 6, 10, 6)
        bar.setSpacing(8)

        self._filter_combo = QComboBox()
        self._filter_combo.addItems([FILTER_ALL, FILTER_ERROR])
        self._filter_combo.currentTextChanged.connect(self._on_filter_changed)
        bar.addWidget(self._filter_combo)

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("搜索日志（大小写不敏感的字面文本）")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._on_search_changed)
        self._search_edit.returnPressed.connect(self._goto_next)
        bar.addWidget(self._search_edit, 1)

        self._match_label = QLabel("0/0")
        self._match_label.setObjectName("LogMeta")
        bar.addWidget(self._match_label)

        self._prev_btn = QPushButton("↑")
        self._prev_btn.setObjectName("FlatBtn")
        self._prev_btn.setToolTip("上一个匹配 (Shift+F3)")
        self._prev_btn.clicked.connect(self._goto_prev)
        bar.addWidget(self._prev_btn)
        self._next_btn = QPushButton("↓")
        self._next_btn.setObjectName("FlatBtn")
        self._next_btn.setToolTip("下一个匹配 (F3)")
        self._next_btn.clicked.connect(self._goto_next)
        bar.addWidget(self._next_btn)

        self._only_matches_check = QCheckBox("只看匹配")
        self._only_matches_check.setEnabled(False)
        self._only_matches_check.toggled.connect(self._on_only_matches)
        bar.addWidget(self._only_matches_check)

        self._auto_scroll_btn = QPushButton("自动滚动：开")
        self._auto_scroll_btn.setObjectName("FlatBtn")
        self._auto_scroll_btn.setCheckable(True)
        self._auto_scroll_btn.setChecked(True)
        self._auto_scroll_btn.toggled.connect(self._on_auto_scroll)
        bar.addWidget(self._auto_scroll_btn)

        self._clear_btn = QPushButton("清屏")
        self._clear_btn.setToolTip("清除当前保留日志，Agent 后续也无法读取这些日志")
        self._clear_btn.clicked.connect(self._on_clear)
        bar.addWidget(self._clear_btn)

        self._export_btn = QPushButton("导出")
        self._export_btn.setToolTip("导出当前视图为 UTF-8 .log (Ctrl+Shift+E)")
        self._export_btn.clicked.connect(self._on_export)
        bar.addWidget(self._export_btn)

        root.addWidget(toolbar)

        self.log_view = ServiceLogView()
        self.log_view.at_bottom_changed.connect(lambda _v: None)
        root.addWidget(self.log_view, 1)

        hint = QLabel("")
        hint.setObjectName("Hint")
        hint.hide()
        self._replay_hint = hint
        root.addWidget(hint)

        self.keyword_jump = QShortcut(QKeySequence("F3"), self)
        self.keyword_jump.activated.connect(self._goto_next)
        self.keyword_jump_back = QShortcut(QKeySequence("Shift+F3"), self)
        self.keyword_jump_back.activated.connect(self._goto_prev)

    # ------------------------------------------------------------------
    @property
    def service_id(self) -> str:
        return self._service_id

    def mark_logs_dirty(self) -> None:
        self._logs_dirty = True

    def mark_facts_dirty(self) -> None:
        self._facts_dirty = True

    def force_full_rebuild(self) -> None:
        self._logs_dirty = True
        self._forced_full = True

    def apply_settings(self, settings: AppSettings) -> None:
        self._settings = settings
        self._logs_dirty = True
        self._forced_full = True
        self._update_search_controls()

    def current_error_count(self) -> int:
        service = self._service_or_none()
        if service is None:
            return 0
        return service.pipeline.error_count

    # ------------------------------------------------------------------
    # 刷新（由主窗口定时器只对可见 Tab 调用）
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        if self._facts_dirty:
            self._apply_facts()
            self._facts_dirty = False
        if self._logs_dirty:
            self._apply_logs()
            self._logs_dirty = False

    def _service_or_none(self):
        try:
            return self._manager.get(self._service_id)
        except SpectaError:
            return None

    def _apply_facts(self) -> None:
        service = self._service_or_none()
        if service is None:
            return
        snap = self._manager.get_snapshot(self._service_id)
        snap["launch"] = service.spec.launch.to_dict()
        snap["ready_when"] = service.spec.ready_when.to_dict() if service.spec.ready_when else None
        self._last_snapshot = snap
        self.header.apply_snapshot(snap)

    def _apply_logs(self) -> None:
        service = self._service_or_none()
        if service is None:
            return
        pipeline_snap = service.pipeline.snapshot()
        context_lines = self._settings.context_lines
        view = log_query.select_view(
            pipeline_snap.records,
            filter_error=self._filter_error,
            keyword=self._keyword,
            only_matches=self._only_matches and bool(self._keyword),
            context_lines=context_lines,
        )
        self._view_records = list(view.records)
        matches = list(view.match_indices or [])
        if matches:
            if self._current_match < 0 or self._current_match >= len(matches):
                self._current_match = len(matches) - 1
        else:
            self._current_match = -1

        full = self._forced_full
        self._forced_full = False
        self.log_view.apply(
            records=view.records,
            matches=matches,
            current_match=self._current_match,
            generation=pipeline_snap.generation,
            evicted_total=pipeline_snap.evicted_total,
            epoch=pipeline_snap.epoch,
            full_rebuild=full,
            auto_scroll=self._auto_scroll,
        )
        self._update_match_label(matches)
        self._update_replay_hint(pipeline_snap)

    def _update_replay_hint(self, snap) -> None:
        if snap.replay_complete:
            self._replay_hint.hide()
            return
        self._replay_hint.setText("部分旧终端布局按原宽保留（原始输出前缀已淘汰，未能无损重建）")
        self._replay_hint.show()

    def _update_match_label(self, matches) -> None:
        total = len(matches)
        if total == 0:
            self._match_label.setText("0/0")
        else:
            self._match_label.setText(f"{self._current_match + 1}/{total}")

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def focus_search(self) -> None:
        self._search_edit.setFocus()
        self._search_edit.selectAll()

    def _on_filter_changed(self, text: str) -> None:
        self._filter_error = text == FILTER_ERROR
        self._forced_full = True
        self._logs_dirty = True
        self._manager_page_refresh()

    def _on_search_changed(self, text: str) -> None:
        self._keyword = text.strip()
        self._current_match = -1
        self._forced_full = True
        self._logs_dirty = True
        self._update_search_controls()
        self._manager_page_refresh()

    def _update_search_controls(self) -> None:
        has_keyword = bool(self._keyword)
        self._only_matches_check.setEnabled(has_keyword)
        if not has_keyword and self._only_matches_check.isChecked():
            self._only_matches_check.setChecked(False)

    def _on_only_matches(self, checked: bool) -> None:
        self._only_matches = checked
        self._forced_full = True
        self._logs_dirty = True
        self._manager_page_refresh()

    def _on_auto_scroll(self, checked: bool) -> None:
        self._auto_scroll = checked
        self._auto_scroll_btn.setText("自动滚动：开" if checked else "自动滚动：关")
        self._logs_dirty = True
        self._manager_page_refresh()

    def _goto_next(self) -> None:
        self._navigate(1)

    def _goto_prev(self) -> None:
        self._navigate(-1)

    def _navigate(self, delta: int) -> None:
        if not self._view_records:
            return
        view = log_query.select_view(
            self._view_records,
            filter_error=False,
            keyword=self._keyword,
            only_matches=False,
            context_lines=self._settings.context_lines,
        )
        matches = list(view.match_indices or [])
        if not matches:
            return
        if self._current_match < 0:
            self._current_match = 0 if delta > 0 else len(matches) - 1
        else:
            self._current_match = (self._current_match + delta) % len(matches)
        self.log_view._matches = matches
        self.log_view._current_match = self._current_match
        self.log_view._records = list(view.records)
        self.log_view._apply_highlights()
        self.log_view.scroll_to_match(matches[self._current_match])
        self._update_match_label(matches)

    def _manager_page_refresh(self) -> None:
        """请求主窗口立即刷新当前页（避免等下一个 50ms tick）。"""
        window = self.window()
        if hasattr(window, "refresh_now"):
            window.refresh_now(self._service_id)

    def _on_start(self) -> None:
        self._submit(ACTION_START)

    def _on_stop(self) -> None:
        self._submit(ACTION_STOP)

    def _on_restart(self) -> None:
        self._submit(ACTION_RESTART)

    def _submit(self, action: str) -> None:
        try:
            if action == ACTION_START:
                self._manager.submit_start(self._service_id, None, source="ui")
            elif action == ACTION_STOP:
                self._manager.submit_stop(self._service_id, source="ui")
            else:
                self._manager.submit_restart(self._service_id, source="ui")
            self.header.clear_error()
        except SpectaError as exc:
            self.header.show_error(exc.to_dict())
        self._facts_dirty = True
        self._manager_page_refresh()

    def _on_clear(self) -> None:
        self._manager.clear_logs(self._service_id)
        self.force_full_rebuild()
        self._manager_page_refresh()

    def _on_export(self) -> None:
        service = self._service_or_none()
        if service is None:
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = log_export.default_export_filename(self._service_id, stamp)
        path, _ = QFileDialog.getSaveFileName(self, "导出日志", default_name, "日志文件 (*.log);;所有文件 (*)")
        if not path:
            return
        snapshot = self._last_snapshot or {}
        run_id = ((snapshot.get("facts") or {}).get("run_id")) or ""
        filtered = self._filter_error or bool(self._keyword)
        text = log_export.build_export_text(
            service_id=self._service_id,
            name=service.spec.name,
            cwd=service.spec.cwd,
            run_id=run_id,
            exported_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            filtered=filtered,
            records=self._view_records,
        )
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

    def copy_all(self) -> None:
        QApplication.clipboard().setText(self.log_view.copy_all_body())


# ---------------------------------------------------------------------------
# 空状态
# ---------------------------------------------------------------------------
class EmptyState(QWidget):
    add_requested = Signal()
    mcp_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setAlignment(Qt.AlignCenter)
        root.setSpacing(10)
        title = QLabel("还没有服务")
        title.setObjectName("EmptyTitle")
        title.setAlignment(Qt.AlignCenter)
        root.addWidget(title)
        body = QLabel("添加启动命令，或让 Agent 通过 MCP 注册服务")
        body.setObjectName("EmptyBody")
        body.setAlignment(Qt.AlignCenter)
        root.addWidget(body)
        row = QHBoxLayout()
        row.setAlignment(Qt.AlignCenter)
        add_btn = QPushButton("新增服务")
        add_btn.setObjectName("PrimaryBtn")
        add_btn.clicked.connect(self.add_requested.emit)
        row.addWidget(add_btn)
        mcp_btn = QPushButton("查看 MCP 配置")
        mcp_btn.clicked.connect(self.mcp_requested.emit)
        row.addWidget(mcp_btn)
        root.addLayout(row)


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    exit_requested = Signal()

    def __init__(
        self,
        manager: ServiceManager,
        settings: AppSettings,
        *,
        bridge_count_provider: Optional[Callable[[], int]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._manager = manager
        self._settings = settings
        self._bridge_count_provider = bridge_count_provider or (lambda: 0)
        self._pages: Dict[str, ServicePage] = {}
        self._unviewed: Dict[str, int] = {}
        self._last_seen_errors: Dict[str, int] = {}
        self._exiting = False
        self._tray = None

        self.setWindowTitle("Specta")
        self.resize(1280, 820)
        self.setMinimumSize(960, 640)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_top_bar())
        self.tab_strip = TabStrip()
        self.tab_strip.selected.connect(self._select_service)
        self.tab_strip.context_menu_requested.connect(self.show_tab_menu)
        root.addWidget(self.tab_strip)

        self.stack = QStackedWidget()
        self.empty_state = EmptyState()
        self.empty_state.add_requested.connect(self._on_new_service)
        self.empty_state.mcp_requested.connect(self._show_mcp_config)
        self.stack.addWidget(self.empty_state)
        root.addWidget(self.stack, 1)

        root.addWidget(self._build_status_bar())

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(RESIZE_DEBOUNCE_MS)
        self._resize_timer.timeout.connect(self._recompute_columns)

        self._install_shortcuts()
        self._setup_tray()
        self._connect_manager()
        self._restore_geometry()

    # ------------------------------------------------------------------
    # 顶栏
    # ------------------------------------------------------------------
    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(48)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(10)
        brand = QLabel("Specta")
        brand.setObjectName("BrandTitle")
        layout.addWidget(brand)
        sub = QLabel("本地开发运行台")
        sub.setObjectName("BrandSub")
        layout.addWidget(sub)
        layout.addStretch(1)
        self._bridge_badge = QLabel("MCP 桥 0")
        self._bridge_badge.setObjectName("BridgeBadge")
        self._bridge_badge.setToolTip("已连接的桥数量；连接本身不代表 Agent 正在工作")
        layout.addWidget(self._bridge_badge)
        new_btn = QPushButton("＋ 新增")
        new_btn.setObjectName("PrimaryBtn")
        new_btn.setToolTip("新增服务 (Ctrl+N)")
        new_btn.clicked.connect(self._on_new_service)
        layout.addWidget(new_btn)
        settings_btn = QPushButton("设置")
        settings_btn.clicked.connect(self._on_settings)
        layout.addWidget(settings_btn)
        return bar

    def _build_status_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(26)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(10)
        self._status_retention = QLabel("保留 0 / 3000 行")
        self._status_run = QLabel("未选择服务")
        self._status_last_op = QLabel("上次操作：—")
        self._status_daemon = QLabel("Daemon 在线")
        for label in (self._status_retention, self._status_run, self._status_last_op):
            layout.addWidget(label)
            sep = QLabel("|")
            sep.setObjectName("StatusSep")
            layout.addWidget(sep)
        layout.addStretch(1)
        layout.addWidget(self._status_daemon)
        return bar

    # ------------------------------------------------------------------
    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self._on_new_service)
        QShortcut(QKeySequence("Ctrl+Shift+E"), self, activated=self._on_export_current)
        QShortcut(QKeySequence("Ctrl+Tab"), self, activated=lambda: self._cycle_tab(1))
        QShortcut(QKeySequence("Ctrl+Shift+Tab"), self, activated=lambda: self._cycle_tab(-1))
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_search)
        QShortcut(QKeySequence("Esc"), self, activated=self._on_escape)

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(self)
        self._tray.setToolTip("Specta")
        menu = QMenu()
        show_action = QAction("显示窗口", self)
        show_action.triggered.connect(self._restore_window)
        menu.addAction(show_action)
        stop_all = QAction("停止全部服务", self)
        stop_all.triggered.connect(self._stop_all_services)
        menu.addAction(stop_all)
        menu.addSeparator()
        quit_action = QAction("退出 Specta", self)
        quit_action.triggered.connect(self._perform_exit)
        menu.addAction(quit_action)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    # ------------------------------------------------------------------
    def _connect_manager(self) -> None:
        self._manager.service_added.connect(self._on_service_added)
        self._manager.service_removed.connect(self._on_service_removed)
        self._manager.service_changed.connect(self._on_service_changed)
        self._manager.logs_changed.connect(self._on_logs_changed)
        self._manager.operation_changed.connect(self._on_operation_changed)
        self._manager.assessment_changed.connect(self._on_service_changed)
        self._manager.error_occurred.connect(self._on_error_occurred)
        self._manager.recovery_needed.connect(self._on_recovery_needed)

    def bind_existing_services(self) -> None:
        for service_id in self._manager.service_ids():
            self._on_service_added(service_id)
        if self._manager.service_ids():
            preferred = self._settings.current_service_id
            ids = self._manager.service_ids()
            self._select_service(preferred if preferred in ids else ids[0])
        else:
            self.stack.setCurrentWidget(self.empty_state)

    # ------------------------------------------------------------------
    # 服务增删改
    # ------------------------------------------------------------------
    def _on_service_added(self, service_id: str) -> None:
        if service_id in self._pages:
            return
        service = self._manager.get(service_id)
        page = ServicePage(service_id, self._manager, self._settings)
        self._pages[service_id] = page
        self.stack.addWidget(page)
        self.tab_strip.add_tab(service_id, service.spec.name)
        self._last_seen_errors[service_id] = service.pipeline.error_count
        self._unviewed[service_id] = 0
        if len(self._pages) == 1:
            self._select_service(service_id)
        self._refresh_tab(service_id)

    def _on_service_removed(self, service_id: str) -> None:
        page = self._pages.pop(service_id, None)
        if page is not None:
            self.stack.removeWidget(page)
            page.deleteLater()
        self.tab_strip.remove_tab(service_id)
        self._unviewed.pop(service_id, None)
        self._last_seen_errors.pop(service_id, None)
        remaining = self.tab_strip.order()
        if remaining:
            self._select_service(remaining[0])
        else:
            self.stack.setCurrentWidget(self.empty_state)
            self._status_run.setText("未选择服务")

    def _on_service_changed(self, service_id: str) -> None:
        page = self._pages.get(service_id)
        if page is not None:
            page.mark_facts_dirty()
            if service_id == self._current_service_id():
                page.refresh()
        self._refresh_tab(service_id)

    def _on_logs_changed(self, service_id: str, epoch: int, revision: int) -> None:
        page = self._pages.get(service_id)
        if page is None:
            return
        page.mark_logs_dirty()
        if service_id != self._current_service_id():
            service = self._manager.get(service_id)
            current = service.pipeline.error_count
            if current > self._last_seen_errors.get(service_id, 0):
                self._unviewed[service_id] = current
            self._refresh_tab(service_id)

    def _on_operation_changed(self, operation_id: str) -> None:
        try:
            operation = self._manager.get_operation(operation_id)
        except SpectaError:
            return
        service_id = operation.get("service_id")
        page = self._pages.get(service_id)
        if page is not None:
            page.mark_facts_dirty()
        self._refresh_tab(service_id)
        if service_id == self._current_service_id() and page is not None:
            page.refresh()

    def _on_error_occurred(self, service_id: str, error: dict) -> None:
        page = self._pages.get(service_id)
        if page is not None:
            page.header.show_error(error)

    def _on_recovery_needed(self, info: object) -> None:
        data = info if isinstance(info, dict) else {}
        messages = [issue.get("message", "") for issue in data.get("issues", [])]
        text = "\n".join(m for m in messages if m)
        if not text:
            return
        QMessageBox.warning(
            self,
            "配置恢复提示",
            "services.json 存在问题，已保留原文件并仅加载可用条目：\n\n" + text,
        )

    def _refresh_tab(self, service_id: str) -> None:
        tab = self.tab_strip.tab(service_id)
        if tab is None:
            return
        service = self._manager.get(service_id)
        errors = service.pipeline.error_count
        unviewed = service_id != self._current_service_id() and errors > self._last_seen_errors.get(service_id, 0)
        tab.update_state(service.spec.name, service.facts.lifecycle, service.facts.exit_code, errors, unviewed)

    # ------------------------------------------------------------------
    # 选择与刷新
    # ------------------------------------------------------------------
    def _current_service_id(self) -> Optional[str]:
        widget = self.stack.currentWidget()
        if isinstance(widget, ServicePage):
            return widget.service_id
        return None

    def _select_service(self, service_id: str) -> None:
        page = self._pages.get(service_id)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        self.tab_strip.set_current(service_id)
        self._settings.current_service_id = service_id
        service = self._manager.get(service_id)
        self._last_seen_errors[service_id] = service.pipeline.error_count
        self._unviewed[service_id] = 0
        page.force_full_rebuild()
        page.refresh()
        self._refresh_tab(service_id)
        self._update_status_bar()
        self._resize_timer.start()

    def _cycle_tab(self, delta: int) -> None:
        order = self.tab_strip.order()
        if len(order) < 2:
            return
        current = self._current_service_id()
        index = order.index(current) if current in order else 0
        self._select_service(order[(index + delta) % len(order)])

    def _on_tick(self) -> None:
        current = self._current_service_id()
        if current is not None:
            page = self._pages.get(current)
            if page is not None:
                page.refresh()
        self._update_status_bar()
        self._bridge_badge.setText(f"MCP 桥 {self._bridge_count_provider()}")

    def refresh_now(self, service_id: Optional[str] = None) -> None:
        if service_id is None or service_id == self._current_service_id():
            current = self._current_service_id()
            if current is not None:
                page = self._pages.get(current)
                if page is not None:
                    page.refresh()
                    self._refresh_tab(current)
                    self._update_status_bar()

    def _update_status_bar(self) -> None:
        current = self._current_service_id()
        if current is None:
            self._status_retention.setText("保留 0 / 3000 行")
            self._status_run.setText("未选择服务")
            self._status_last_op.setText("上次操作：—")
            return
        service = self._manager.get(current)
        pipeline_snap = service.pipeline.snapshot()
        suffix = "（已淘汰最早的日志）" if pipeline_snap.history_evicted else ""
        self._status_retention.setText(f"保留 {pipeline_snap.retained_rows} / 3000 行{suffix}")
        label, _color = theme.lifecycle_display(service.facts.lifecycle, service.facts.exit_code)
        if pipeline_snap.current_run_id:
            self._status_run.setText(f"当前运行：{label} · {pipeline_snap.current_run_id}")
        else:
            self._status_run.setText(f"当前运行：{label}")
        # 上一次操作的来源属于"当前页的快照"。注意 MainWindow 自身不持有该快照：
        # 早期版本写成 self._last_snapshot，导致带已保存服务启动时
        # （bind_existing_services -> _select_service -> 这里）直接 AttributeError 崩溃。
        page = self._pages.get(current)
        snap = (page._last_snapshot if page is not None else None) or {}
        last_op = snap.get("last_operation")
        if last_op:
            source = "界面" if last_op.get("source") == "ui" else "Agent"
            self._status_last_op.setText(
                f"上次操作：{source} {_action_label(last_op.get('action'))} · {last_op.get('status')}"
            )
        else:
            self._status_last_op.setText("上次操作：—")

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def _on_new_service(self) -> None:
        if len(self._pages) >= 50:
            QMessageBox.warning(self, "已达上限", "最多注册 50 个服务")
            return
        dialog = ServiceEditorDialog(parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        spec = dialog.result_spec()
        if spec is None:
            return
        try:
            self._manager.create_or_replace_user_service(spec)
        except SpectaError as exc:
            QMessageBox.warning(self, "保存失败", f"{exc.code}：{exc.message}")
            return
        self._select_service(spec.service_id)

    def _edit_service(self, service_id: str) -> None:
        try:
            service = self._manager.get(service_id)
        except SpectaError:
            return
        if service.running or self._manager._registry.has_work(service_id):
            QMessageBox.information(self, "无法编辑", "服务正在运行或有操作排队，请先停止服务。")
            return
        dialog = ServiceEditorDialog(existing=service.spec, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        spec = dialog.result_spec()
        if spec is None:
            return
        try:
            self._manager.create_or_replace_user_service(spec)
        except SpectaError as exc:
            QMessageBox.warning(self, "保存失败", f"{exc.code}：{exc.message}")

    def _delete_service(self, service_id: str) -> None:
        try:
            service = self._manager.get(service_id)
        except SpectaError:
            return
        if service.running or self._manager._registry.has_work(service_id):
            QMessageBox.information(self, "无法删除", "请先停止服务再删除。")
            return
        confirm = QMessageBox.question(
            self,
            "删除服务",
            f"确定删除服务 “{service.spec.name}”（{service_id}）的配置？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self._manager.delete_service(service_id, source="ui")
        except SpectaError as exc:
            QMessageBox.warning(self, "删除失败", f"{exc.code}：{exc.message}")

    def _copy_service_id(self, service_id: str) -> None:
        QApplication.clipboard().setText(service_id)

    def show_tab_menu(self, service_id: str) -> None:
        service = self._manager.get(service_id)
        menu = QMenu(self)
        start = QAction("启动", self)
        start.setEnabled(not service.running)
        start.triggered.connect(lambda: self._manager.submit_start(service_id, None, source="ui"))
        menu.addAction(start)
        restart = QAction("重启", self)
        restart.triggered.connect(lambda: self._manager.submit_restart(service_id, source="ui"))
        menu.addAction(restart)
        stop = QAction("停止", self)
        stop.setEnabled(service.running)
        stop.triggered.connect(lambda: self._manager.submit_stop(service_id, source="ui"))
        menu.addAction(stop)
        menu.addSeparator()
        edit = QAction("编辑配置…", self)
        edit.triggered.connect(lambda: self._edit_service(service_id))
        menu.addAction(edit)
        copy_id = QAction("复制 ID", self)
        copy_id.triggered.connect(lambda: self._copy_service_id(service_id))
        menu.addAction(copy_id)
        delete = QAction("删除服务…", self)
        delete.setEnabled(not service.running)
        delete.triggered.connect(lambda: self._delete_service(service_id))
        menu.addAction(delete)
        menu.exec(self.cursor().pos())

    def _on_settings(self) -> None:
        dialog = SettingsDialog(self._settings, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        self._settings = dialog.result_settings()
        save_settings(self._settings)
        self._apply_fonts()
        for page in self._pages.values():
            page.apply_settings(self._settings)

    def _apply_fonts(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.build_stylesheet(self._settings.font_size))
        for page in self._pages.values():
            page.log_view.setFont(theme.mono_font(self._settings.font_size))

    def _show_mcp_config(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("MCP 配置")
        dialog.setMinimumWidth(560)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit(build_mcp_config())
        text.setReadOnly(True)
        text.setFont(theme.mono_font(12))
        layout.addWidget(text)
        hint = QLabel("把上面的片段合并进 Agent 客户端的 MCP 配置；命令使用当前 Specta 的 Python 解释器。")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        copy = QPushButton("复制")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(text.toPlainText()))
        buttons.addButton(copy, QDialogButtonBox.ActionRole)
        layout.addWidget(buttons)
        dialog.exec()

    def _focus_search(self) -> None:
        current = self._current_service_id()
        if current:
            self._pages[current].focus_search()

    def _on_export_current(self) -> None:
        current = self._current_service_id()
        if current:
            self._pages[current]._on_export()

    def _on_escape(self) -> None:
        current = self._current_service_id()
        if current:
            self._pages[current].header.close_details()

    def _stop_all_services(self) -> None:
        for service_id in self.tab_strip.order():
            service = self._manager.get(service_id)
            if service.running:
                try:
                    self._manager.submit_stop(service_id, source="ui")
                except SpectaError:
                    pass

    # ------------------------------------------------------------------
    # 列宽
    # ------------------------------------------------------------------
    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._resize_timer.start()

    def _recompute_columns(self) -> None:
        current = self._current_service_id()
        if current is None:
            return
        page = self._pages.get(current)
        if page is None:
            return
        metrics = QFontMetrics(theme.mono_font(self._settings.font_size))
        char_width = max(1, metrics.horizontalAdvance("0"))
        width = page.log_view.viewport().width() - 16
        columns = max(40, min(400, width // char_width))
        self._manager.resize_logs(int(columns))

    # ------------------------------------------------------------------
    # 关闭 / 退出
    # ------------------------------------------------------------------
    def closeEvent(self, event):  # noqa: N802
        if self._exiting:
            event.accept()
            return
        behavior = self._settings.close_behavior
        if behavior == "tray" and self._tray is not None:
            event.ignore()
            self.hide()
            if not self._settings.tray_hint_shown:
                self._settings.tray_hint_shown = True
                save_settings(self._settings)
                self._tray.showMessage("Specta", "Specta 已隐藏到托盘，服务继续在后台运行。", QSystemTrayIcon.Information, 4000)
            return
        event.ignore()
        self._perform_exit()

    def _perform_exit(self) -> None:
        running = [sid for sid in self.tab_strip.order() if self._manager.get(sid).running]
        if running:
            confirm = QMessageBox.question(
                self,
                "退出 Specta",
                f"退出将停止 {len(running)} 个服务，确定继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
        self._exiting = True
        try:
            self._settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        except Exception:  # noqa: BLE001
            pass
        save_settings(self._settings)
        self.exit_requested.emit()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self._restore_window()

    def _restore_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _restore_geometry(self) -> None:
        if not self._settings.window_geometry:
            return
        try:
            from PySide6.QtCore import QByteArray

            self.restoreGeometry(QByteArray.fromBase64(self._settings.window_geometry.encode("ascii")))
        except Exception:  # noqa: BLE001
            pass


def _action_label(action: Optional[str]) -> str:
    return {ACTION_START: "启动", ACTION_STOP: "停止", ACTION_RESTART: "重启"}.get(action or "", action or "—")
