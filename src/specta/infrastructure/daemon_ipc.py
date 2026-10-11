"""Daemon 侧 IPC 服务：Qt TCP + JSON Lines + token 鉴权。

只绑 127.0.0.1，端口动态申请；端口与 token 由 daemon_main 在取得单实例互斥后发布
到 daemon.json。本类不执行自己的启停逻辑，全部转交 ServiceManager。
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any, Dict, Optional

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket

from ..domain.errors import ErrorCode, SpectaError
from ..domain.models import (
    ORIGIN_AGENT,
    ORIGIN_USER,
    ServiceSpec,
    slugify,
    spec_from_legacy,
)
from ..domain.protocol import (
    FrameDecoder,
    FrameError,
    IPC_AUTH_TIMEOUT_MS,
    IPC_MAX_CONNECTIONS,
    IPC_MAX_FRAME,
    M_GET_OPERATION,
    M_GET_RECENT_LOGS,
    M_GET_SERVICE_STATUS,
    M_HELLO,
    M_LIST_SERVICES,
    M_PING,
    M_REPORT_SERVICE_STATE,
    M_RESTART_SERVICE,
    M_START_SERVICE,
    M_STOP_SERVICE,
    PROTOCOL_VERSION,
    encode_frame,
    ipc_err,
    ipc_ok,
    validate_ipc_request,
)
from ..infrastructure.diagnostics import get_logger

log = get_logger()


class _Connection(QObject):
    def __init__(self, socket: QTcpSocket, server: "DaemonIpcServer") -> None:
        super().__init__(server)
        self.socket = socket
        self.server = server
        self.decoder = FrameDecoder(IPC_MAX_FRAME)
        self.authed = False
        self.session_id: Optional[str] = None
        self._buffer_out: bytearray = bytearray()
        self._auth_timer = QTimer(self)
        self._auth_timer.setSingleShot(True)
        self._auth_timer.timeout.connect(self._on_auth_timeout)
        self._auth_timer.start(IPC_AUTH_TIMEOUT_MS)
        socket.readyRead.connect(self._on_ready)
        socket.disconnected.connect(self._on_disconnected)
        socket.bytesWritten.connect(self._flush)

    # -- IO ------------------------------------------------------------
    def _on_ready(self) -> None:
        data = bytes(self.socket.readAll())
        if not data:
            return
        try:
            frames = self.decoder.feed(data)
        except FrameError as exc:
            if exc.fatal:
                self.close()
                return
            self._send(ipc_err("", ErrorCode.INVALID_ARGUMENT, exc.reason))
            return
        for frame in frames:
            self._handle(frame)

    def _handle(self, frame: Dict[str, Any]) -> None:
        try:
            req_id, method, params = validate_ipc_request(frame, self.authed)
        except FrameError as exc:
            if exc.reason == "ipc_not_authenticated":
                self.close()
                return
            self._send(ipc_err(str(frame.get("id") or ""), ErrorCode.INVALID_ARGUMENT, exc.reason))
            return

        if method == M_HELLO:
            self._handle_hello(req_id, params)
            return
        try:
            result = self.server.dispatch(method, params, self.session_id)
            self._send(ipc_ok(req_id, result))
        except SpectaError as exc:
            self._send(ipc_err(req_id, exc.code, exc.message, exc.details))
        except Exception as exc:  # noqa: BLE001
            diag = uuid.uuid4().hex[:8]
            log.exception("ipc internal error %s: %s", diag, exc)
            self._send(
                ipc_err(
                    req_id,
                    ErrorCode.INTERNAL_ERROR,
                    "内部错误",
                    {"diagnostic_id": diag},
                )
            )

    def _handle_hello(self, req_id: str, params: Dict[str, Any]) -> None:
        version = params.get("protocol_version")
        token = params.get("token")
        if version != PROTOCOL_VERSION:
            self._send(
                ipc_err(
                    req_id,
                    ErrorCode.PROTOCOL_MISMATCH,
                    f"协议版本不匹配：期望 {PROTOCOL_VERSION}，收到 {version}",
                    {"expected": PROTOCOL_VERSION, "got": version},
                )
            )
            self.close()
            return
        if not isinstance(token, str) or not secrets.compare_digest(token, self.server.token):
            self._send(ipc_err(req_id, ErrorCode.AUTH_FAILED, "token 无效"))
            self.close()
            return
        self.authed = True
        self._auth_timer.stop()
        session = params.get("session_id")
        self.session_id = session if isinstance(session, str) else None
        self._send(
            ipc_ok(
                req_id,
                {"instance_id": self.server.instance_id, "protocol_version": PROTOCOL_VERSION},
            )
        )

    def _on_auth_timeout(self) -> None:
        if not self.authed:
            self.close()

    def _send(self, payload: Dict[str, Any]) -> None:
        self._buffer_out.extend(encode_frame(payload))
        self._flush()

    def _flush(self) -> None:
        if not self._buffer_out:
            return
        if self.socket.state() != QTcpSocket.ConnectedState:
            return
        written = self.socket.write(bytes(self._buffer_out))
        if written > 0:
            del self._buffer_out[:written]

    def _on_disconnected(self) -> None:
        self.server.remove_connection(self)

    def close(self) -> None:
        """优雅关闭连接。

        当仍有未发出的响应时（典型为 AUTH_FAILED / PROTOCOL_MISMATCH），必须先把
        缓冲区数据写完再断开；直接 abort() 会丢弃这些帧，桥侧只会看到「连接被关闭」
        而读不到错误码。真正的移除交给 disconnected 信号，并设置兜底强杀定时器，
        防止对端不读取导致连接悬挂。
        """
        state = self.socket.state()
        try:
            if state in (QTcpSocket.ConnectedState, QTcpSocket.ClosingState):
                self.socket.disconnectFromHost()
            else:
                self.socket.abort()
        except Exception:  # noqa: BLE001
            pass
        if self.socket.state() == QTcpSocket.UnconnectedState:
            self.server.remove_connection(self)
        else:  # 兜底：2s 内仍未能优雅断开则强制中止
            QTimer.singleShot(2000, self._force_abort)

    def _force_abort(self) -> None:
        try:
            self.socket.abort()
        except Exception:  # noqa: BLE001
            pass
        self.server.remove_connection(self)


class DaemonIpcServer(QObject):
    """QTcpServer 封装。dispatch 由 daemon_main 注入实际业务实现。"""

    def __init__(self, token: str, instance_id: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.token = token
        self.instance_id = instance_id
        self.port: Optional[int] = None
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        self._connections: Dict[QTcpSocket, _Connection] = {}
        self._handlers: Dict[str, Any] = {}

    def set_handler(self, method: str, handler) -> None:
        """handler(params, session_id) -> dict（内部应抛出 SpectaError 表示失败）。"""
        self._handlers[method] = handler

    def listen(self) -> int:
        if not self._server.listen(QHostAddress("127.0.0.1"), 0):
            raise SpectaError(
                ErrorCode.INTERNAL_ERROR,
                f"IPC 监听失败: {self._server.errorString()}",
            )
        self.port = int(self._server.serverPort())
        return self.port

    def close(self) -> None:
        for conn in list(self._connections.values()):
            conn.close()
        self._connections.clear()
        self._server.close()

    # -- 连接 ----------------------------------------------------------
    def _on_new_connection(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                break
            if len(self._connections) >= IPC_MAX_CONNECTIONS:
                socket.abort()
                continue
            conn = _Connection(socket, self)
            self._connections[socket] = conn

    def remove_connection(self, conn: _Connection) -> None:
        self._connections.pop(conn.socket, None)
        conn.socket.deleteLater()

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    # -- 路由 ----------------------------------------------------------
    def dispatch(self, method: str, params: Dict[str, Any], session_id: Optional[str]) -> Dict[str, Any]:
        if method == M_PING:
            return {"pong": True, "instance_id": self.instance_id}
        handler = self._handlers.get(method)
        if handler is None:
            raise SpectaError(ErrorCode.INVALID_ARGUMENT, f"未知方法: {method}")
        return handler(params, session_id)
