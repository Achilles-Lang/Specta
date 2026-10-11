"""Daemon 入口：python -m specta.daemon_main

组装顺序（规格 11.1 / 模块设计 6）：

  1. 单实例守卫（QLockFile）—— 只有持有者才发布 daemon.json 并启动完整应用；
  2. QApplication、Repository、ServiceManager（恢复配置，不自动启动服务）；
  3. IPC 服务监听 127.0.0.1:0，生成 token，原子写 daemon.json；
  4. MainWindow 与托盘；把 IPC 方法路由到 ServiceManager（不在 IPC 层重写业务）。

环境变量：
  SPECTA_NO_UI=1  不创建窗口（集成测试用，IPC 与进程管理仍可用）
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
import uuid
from typing import Any, Dict, Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from . import __version__
from .application import params as param_parse
from .application.service_manager import ServiceManager
from .domain.errors import ErrorCode, SpectaError
from .domain.models import to_rfc3339, utc_now
from .domain.protocol import PROTOCOL_VERSION
from .infrastructure import diagnostics
from .infrastructure.daemon_ipc import DaemonIpcServer
from .infrastructure.instance_guard import SingleInstanceGuard
from .infrastructure.repository import ServiceRepository
from .infrastructure.settings import (
    AppSettings,
    atomic_write_json,
    daemon_json_path,
    load_settings,
)

log = logging.getLogger("specta")


class DaemonRuntime:
    def __init__(self, app: QApplication, manager: ServiceManager, ipc: DaemonIpcServer, settings: AppSettings) -> None:
        self.app = app
        self.manager = manager
        self.ipc = ipc
        self.settings = settings
        self.window = None
        self.instance_id = ipc.instance_id
        self._stopping = False

    # ------------------------------------------------------------------
    def register_handlers(self) -> None:
        self.ipc.set_handler("list_services", self._h_list_services)
        self.ipc.set_handler("get_service_status", self._h_get_service_status)
        self.ipc.set_handler("start_service", self._h_start_service)
        self.ipc.set_handler("stop_service", self._h_stop_service)
        self.ipc.set_handler("restart_service", self._h_restart_service)
        self.ipc.set_handler("get_recent_logs", self._h_get_recent_logs)
        self.ipc.set_handler("report_service_state", self._h_report_service_state)
        self.ipc.set_handler("get_operation", self._h_get_operation)

    # -- 只读 ---------------------------------------------------------------
    def _h_list_services(self, params: Dict[str, Any], session_id: Optional[str]) -> Any:
        return {"services": self.manager.list_services()}

    def _h_get_service_status(self, params: Dict[str, Any], session_id: Optional[str]) -> Any:
        service_id = param_parse.service_id_param(params)
        return {"snapshot": self.manager.get_snapshot(service_id)}

    def _h_get_recent_logs(self, params: Dict[str, Any], session_id: Optional[str]) -> Any:
        parsed = param_parse.parse_logs(params)
        return self.manager.get_recent_logs(
            parsed["service_id"],
            lines=parsed["lines"],
            filter_error=parsed["filter_error"],
            run_id=parsed["run_id"],
            after_sequence=parsed["after_sequence"],
        )

    def _h_get_operation(self, params: Dict[str, Any], session_id: Optional[str]) -> Any:
        parsed = param_parse.parse_operation(params)
        return {"operation": self.manager.get_operation(parsed["operation_id"])}

    # -- 写操作 -------------------------------------------------------------
    def _h_start_service(self, params: Dict[str, Any], session_id: Optional[str]) -> Any:
        service_id, spec, update_existing = param_parse.parse_start(params)
        operation = self.manager.submit_start(
            service_id,
            spec,
            source="agent",
            session_id=session_id,
            request_key=param_parse.request_key_param(params),
            update_existing=update_existing,
        )
        return {"operation": operation.to_dict()}

    def _h_stop_service(self, params: Dict[str, Any], session_id: Optional[str]) -> Any:
        service_id = param_parse.service_id_param(params)
        operation = self.manager.submit_stop(
            service_id,
            source="agent",
            session_id=session_id,
            request_key=param_parse.request_key_param(params),
        )
        return {"operation": operation.to_dict()}

    def _h_restart_service(self, params: Dict[str, Any], session_id: Optional[str]) -> Any:
        service_id = param_parse.service_id_param(params)
        operation = self.manager.submit_restart(
            service_id,
            source="agent",
            session_id=session_id,
            request_key=param_parse.request_key_param(params),
        )
        return {"operation": operation.to_dict()}

    def _h_report_service_state(self, params: Dict[str, Any], session_id: Optional[str]) -> Any:
        parsed = param_parse.parse_report(params)
        snapshot = self.manager.report_service_state(
            parsed["service_id"],
            parsed["run_id"],
            parsed["state"],
            parsed["reason"],
            session_id=session_id,
        )
        return {"snapshot": snapshot}

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        log.info("Specta Daemon 正在退出")
        try:
            self.ipc.close()
        except Exception:  # noqa: BLE001
            log.exception("关闭 IPC 失败")
        # 清理全部 Job 与服务进程
        self.manager.shutdown()
        # 只删除属于本实例的 endpoint 文件
        try:
            path = daemon_json_path()
            if path.exists():
                import json

                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("instance_id") == self.instance_id:
                    path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            log.exception("清理 daemon.json 失败")


def build_runtime(app: QApplication, *, with_ui: bool = True) -> Optional[DaemonRuntime]:
    """组装 Daemon。返回 None 表示已有实例在运行（候选进程应直接退出）。"""
    guard = SingleInstanceGuard()
    if not guard.acquire():
        log.info("已有 Specta Daemon 在运行，候选进程退出")
        return None

    settings = load_settings()
    repository = ServiceRepository()
    manager = ServiceManager(
        repository,
        context_lines_provider=lambda: load_settings().context_lines,
        parent=app,
    )
    load_result = manager.load_from_disk()
    if load_result.needs_recovery:
        log.warning("配置恢复：%s", load_result.issues)
    manager.start_probing()

    token = secrets.token_urlsafe(32)
    instance_id = uuid.uuid4().hex
    ipc = DaemonIpcServer(token=token, instance_id=instance_id, parent=app)
    port = ipc.listen()

    atomic_write_json(
        daemon_json_path(),
        {
            "protocol_version": PROTOCOL_VERSION,
            "instance_id": instance_id,
            "pid": os.getpid(),
            "port": port,
            "token": token,
            "started_at": to_rfc3339(utc_now()),
        },
    )
    log.info("Specta Daemon 监听 127.0.0.1:%s（pid=%s）", port, os.getpid())

    runtime = DaemonRuntime(app, manager, ipc, settings)
    runtime.register_handlers()

    if with_ui:
        from .ui import theme
        from .ui.main_window import MainWindow

        app.setStyleSheet(theme.build_stylesheet(settings.font_size))
        window = MainWindow(
            manager,
            settings,
            bridge_count_provider=lambda: ipc.connection_count,
        )
        window.bind_existing_services()
        window.exit_requested.connect(runtime.shutdown)
        window.exit_requested.connect(app.quit)
        runtime.window = window
        window.show()

    app.aboutToQuit.connect(runtime.shutdown)
    # guard 必须在整个进程生命周期内持有互斥权
    runtime._guard = guard  # type: ignore[attr-defined]
    return runtime


def main(argv=None) -> int:
    diagnostics.setup_logging()
    # stdout 可能被 MCP 桥占用（本进程不会），但日志统一走 stderr 更安全
    logging.getLogger().setLevel(logging.INFO)

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Specta")
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)

    with_ui = os.environ.get("SPECTA_NO_UI", "").strip() not in ("1", "true", "yes")
    runtime = build_runtime(app, with_ui=with_ui)
    if runtime is None:
        return 0
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
