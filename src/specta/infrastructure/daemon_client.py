"""桥侧 IPC 客户端（纯 asyncio，不导入 Qt）。

职责：发现 Daemon → 必要时脱离桥生命周期自动拉起 → 连接并鉴权 → 发送请求 →
处理超时。写操作在超时后结果可能未知，绝不自动重发。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from ..domain.errors import ErrorCode, SpectaError
from ..domain.models import new_session_id
from ..domain.protocol import (
    IPC_MAX_FRAME,
    M_GET_OPERATION,
    M_HELLO,
    PROTOCOL_VERSION,
    encode_frame,
)

CONNECT_RETRY_INTERVAL_S = 0.2
CONNECT_TOTAL_TIMEOUT_S = 10.0
DEFAULT_CALL_TIMEOUT_S = 15.0
OPERATION_WAIT_S = 20.0

TERMINAL_OP_STATUSES = ("succeeded", "failed")

# asyncio 的 StreamReader 默认 limit 只有 64 KiB，而一个**合法**的 IPC 帧上限是
# IPC_MAX_FRAME（1 MiB）。读取限制必须与之对齐（再为行尾换行留 1 字节余量），否则
# 一次接近上限的日志响应会被 readline() 当成超限，抛 LimitOverrunError 并清空内部
# 缓冲，之后整条连接读取全部错位。
IPC_READ_LIMIT = IPC_MAX_FRAME + 1


class DaemonClient:
    def __init__(self, session_id: Optional[str] = None) -> None:
        self.session_id = session_id or new_session_id()
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._instance_id: Optional[str] = None
        self._request_counter = 0
        self._lock = asyncio.Lock()
        self._port: Optional[int] = None
        self._token: Optional[str] = None

    # ------------------------------------------------------------------
    # 发现与连接
    # ------------------------------------------------------------------
    @property
    def instance_id(self) -> Optional[str]:
        return self._instance_id

    def _endpoint_path(self) -> Path:
        from ..infrastructure.settings import daemon_json_path

        return daemon_json_path()

    def _read_endpoint(self) -> Optional[Dict[str, Any]]:
        path = self._endpoint_path()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return data

    async def ensure_connected(self) -> None:
        if self._writer is not None and not self._writer.is_closing():
            return
        endpoint = self._read_endpoint()
        if endpoint and await self._try_connect(endpoint):
            return
        self._launch_daemon()
        deadline = time.monotonic() + CONNECT_TOTAL_TIMEOUT_S
        last_error: Optional[str] = None
        while time.monotonic() < deadline:
            await asyncio.sleep(CONNECT_RETRY_INTERVAL_S)
            endpoint = self._read_endpoint()
            if not endpoint:
                continue
            try:
                if await self._try_connect(endpoint):
                    return
            except SpectaError as exc:
                if exc.code == ErrorCode.PROTOCOL_MISMATCH:
                    raise
                last_error = exc.message
        raise SpectaError(
            ErrorCode.DAEMON_UNAVAILABLE,
            "无法连接 Specta Daemon",
            {
                "hint": "请手动启动 Specta，或检查 %LOCALAPPDATA%\\Specta 是否可写",
                "last_error": last_error,
            },
        )

    async def _try_connect(self, endpoint: Dict[str, Any]) -> bool:
        version = endpoint.get("protocol_version")
        if version != PROTOCOL_VERSION:
            raise SpectaError(
                ErrorCode.PROTOCOL_MISMATCH,
                f"daemon.json 协议版本 {version} 与桥 {PROTOCOL_VERSION} 不一致",
                {"expected": PROTOCOL_VERSION, "got": version},
            )
        port = endpoint.get("port")
        token = endpoint.get("token")
        if not isinstance(port, int) or not isinstance(token, str):
            return False
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port, limit=IPC_READ_LIMIT),
                timeout=2.0,
            )
        except (OSError, asyncio.TimeoutError):
            return False
        try:
            response = await self._hello(reader, writer, token)
        except Exception:  # noqa: BLE001
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return False
        self._reader = reader
        self._writer = writer
        self._instance_id = response.get("instance_id")
        self._port = port
        self._token = token
        return True

    async def _hello(self, reader, writer, token: str) -> Dict[str, Any]:
        payload = {
            "id": self._next_id(),
            "method": M_HELLO,
            "params": {
                "protocol_version": PROTOCOL_VERSION,
                "token": token,
                "session_id": self.session_id,
            },
        }
        writer.write(encode_frame(payload))
        await writer.drain()
        response = await asyncio.wait_for(self._read_frame(reader), timeout=5.0)
        if not response.get("ok"):
            error = response.get("error") or {}
            raise SpectaError(
                error.get("code", ErrorCode.AUTH_FAILED),
                error.get("message", "握手失败"),
                error.get("details") or {},
            )
        return response.get("result") or {}

    def _launch_daemon(self) -> None:
        args = [sys.executable, "-m", "specta.daemon_main"]
        env = dict(os.environ)
        try:
            import specta

            src = Path(specta.__file__).resolve().parent.parent
            if (src / "specta" / "daemon_main.py").exists():
                existing = env.get("PYTHONPATH", "")
                env["PYTHONPATH"] = str(src) + (os.pathsep + existing if existing else "")
        except Exception:  # noqa: BLE001
            pass
        creationflags = 0
        if sys.platform == "win32":
            creationflags = (
                getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
        try:
            subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                env=env,
                creationflags=creationflags,
                cwd=str(Path(os.environ.get("TEMP", ".")).resolve()),
            )
        except OSError as exc:
            raise SpectaError(
                ErrorCode.DAEMON_LAUNCH_FAILED,
                f"无法启动 Specta Daemon: {exc}",
                {"manual_command": f'"{sys.executable}" -m specta.daemon_main'},
            ) from exc

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------
    def _next_id(self) -> str:
        self._request_counter += 1
        return f"req_{self._request_counter}_{uuid.uuid4().hex[:6]}"

    async def _read_frame(self, reader) -> Dict[str, Any]:
        # 调用方可能递来一个按默认 limit 建的 reader（例如被内嵌桥直接调用的场景），
        # 这里把它的读限制对齐到 IPC_READ_LIMIT，再交给 readline()，保证合法的大帧
        # 不会被误判为超限。
        limit = getattr(reader, "_limit", None)
        if isinstance(limit, int) and limit < IPC_READ_LIMIT:
            reader._limit = IPC_READ_LIMIT
        try:
            line = await reader.readline()
        except ValueError as exc:
            # readline() 在超过 limit 时把 LimitOverrunError 包成 ValueError，并清空
            # 内部缓冲。此时连接状态已不可信，按连接丢失处理，由上层重建连接。
            raise SpectaError(
                ErrorCode.CONNECTION_LOST, f"响应帧超过上限: {exc}"
            ) from exc
        if not line:
            raise SpectaError(ErrorCode.CONNECTION_LOST, "连接已断开")
        if len(line) > IPC_MAX_FRAME:
            raise SpectaError(ErrorCode.CONNECTION_LOST, "响应帧过大")
        try:
            return json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpectaError(ErrorCode.CONNECTION_LOST, f"响应帧损坏: {exc}") from exc

    async def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = DEFAULT_CALL_TIMEOUT_S,
        retry_read: bool = False,
    ) -> Any:
        await self.ensure_connected()
        attempts = 2 if retry_read else 1
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                return await self._call_once(method, params or {}, timeout)
            except SpectaError as exc:
                last_exc = exc
                if exc.code == ErrorCode.CONNECTION_LOST and attempt + 1 < attempts:
                    self._drop_connection()
                    await self.ensure_connected()
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise SpectaError(ErrorCode.INTERNAL_ERROR, "调用失败")

    async def _call_once(self, method: str, params: Dict[str, Any], timeout: float) -> Any:
        async with self._lock:
            assert self._writer is not None and self._reader is not None
            request_id = self._next_id()
            payload = {"id": request_id, "method": method, "params": params}
            self._writer.write(encode_frame(payload))
            try:
                await self._writer.drain()
            except (OSError, ConnectionError) as exc:
                self._drop_connection()
                raise SpectaError(ErrorCode.CONNECTION_LOST, f"发送失败: {exc}") from exc
            try:
                while True:
                    response = await asyncio.wait_for(self._read_frame(self._reader), timeout=timeout)
                    if response.get("id") != request_id:
                        # 允许完成顺序不同：丢弃不匹配的响应
                        continue
                    break
            except asyncio.TimeoutError as exc:
                raise SpectaError(ErrorCode.CONNECTION_LOST, "请求超时") from exc
        if not response.get("ok"):
            error = response.get("error") or {}
            raise SpectaError(
                error.get("code", ErrorCode.INTERNAL_ERROR),
                error.get("message", "调用失败"),
                error.get("details") or {},
            )
        return response.get("result")

    def _drop_connection(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
        self._writer = None
        self._reader = None

    # ------------------------------------------------------------------
    # 操作等待
    # ------------------------------------------------------------------
    async def wait_operation(
        self, operation_id: str, *, timeout: float = OPERATION_WAIT_S
    ) -> Dict[str, Any]:
        """轮询操作直到完成；超时返回 pending 标记，不视为失败。

        IPC 的 get_operation 返回 {"operation": {...}}（与其它方法的返回约定一致），
        这里统一解包为「操作本体」，调用方（桥/工具层）只面对操作字典本身。
        若不在此处解包，status 永远读不到，会一直轮询到超时并误报 pending。
        """
        deadline = time.monotonic() + timeout
        delay = 0.05
        while True:
            result = await self.call(
                M_GET_OPERATION, {"operation_id": operation_id}, timeout=5.0, retry_read=True
            )
            operation = result.get("operation") if isinstance(result, dict) else None
            if operation is None:
                # 兼容直接返回操作字典的情况
                operation = result if isinstance(result, dict) else {}
            status = operation.get("status")
            if status in TERMINAL_OP_STATUSES:
                return operation
            if time.monotonic() >= deadline:
                return {"operation_id": operation_id, "status": "pending", "pending": True}
            await asyncio.sleep(delay)
            delay = min(0.4, delay * 1.6)

    async def close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        self._writer = None
        self._reader = None
