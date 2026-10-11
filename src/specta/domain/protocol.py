"""IPC 与启动器的分帧、协议常量与消息校验。

两个独立通道共用"每行一个 JSON 对象 + LF"的分帧方式，但消息大小上限不同：
  * Daemon <-> 桥（IPC）：最大 1MiB
  * Daemon <-> 启动器（worker）：最大 128KiB，输出按块（<=32KiB 原始字节）传输
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional, Tuple

PROTOCOL_VERSION = 1

IPC_MAX_FRAME = 1024 * 1024
IPC_AUTH_TIMEOUT_MS = 3000
IPC_MAX_CONNECTIONS = 32

WORKER_MAX_FRAME = 128 * 1024
WORKER_MAX_CHUNK = 32 * 1024
WORKER_LAUNCH_TIMEOUT_S = 10.0

# 启动器 -> Daemon 的消息类型
W_CHILD_STARTED = "child_started"
W_OUTPUT = "output"
W_ROOT_EXITED = "root_exited"
W_CHILD_EXIT = "child_exit"
W_LAUNCH_ERROR = "launch_error"

# IPC 方法名
M_HELLO = "hello"
M_PING = "ping"
M_LIST_SERVICES = "list_services"
M_GET_SERVICE_STATUS = "get_service_status"
M_START_SERVICE = "start_service"
M_STOP_SERVICE = "stop_service"
M_RESTART_SERVICE = "restart_service"
M_GET_RECENT_LOGS = "get_recent_logs"
M_REPORT_SERVICE_STATE = "report_service_state"
M_GET_OPERATION = "get_operation"

IPC_METHODS = frozenset(
    {
        M_PING,
        M_LIST_SERVICES,
        M_GET_SERVICE_STATUS,
        M_START_SERVICE,
        M_STOP_SERVICE,
        M_RESTART_SERVICE,
        M_GET_RECENT_LOGS,
        M_REPORT_SERVICE_STATE,
        M_GET_OPERATION,
    }
)


class FrameError(Exception):
    """分帧错误。fatal=True 表示连接必须断开（超长或无法恢复）。"""

    def __init__(self, reason: str, fatal: bool = True) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fatal = fatal


class FrameDecoder:
    """增量 JSON Lines 解码器，正确处理半包与粘包。"""

    def __init__(self, max_frame: int = IPC_MAX_FRAME) -> None:
        self._buffer = bytearray()
        self._max_frame = max_frame

    def feed(self, data: bytes) -> List[Any]:
        """喂入字节，返回本次解析出的完整消息列表（按到达顺序）。"""
        self._buffer.extend(data)
        out: List[Any] = []
        while True:
            idx = self._buffer.find(b"\n")
            if idx < 0:
                if len(self._buffer) > self._max_frame:
                    raise FrameError("frame_too_large", fatal=True)
                break
            raw = bytes(self._buffer[:idx])
            del self._buffer[: idx + 1]
            if len(raw) > self._max_frame:
                raise FrameError("frame_too_large", fatal=True)
            if not raw.strip():
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise FrameError(f"bad_utf8: {exc}", fatal=False) from exc
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                raise FrameError(f"bad_json: {exc}", fatal=False) from exc
            if not isinstance(obj, dict):
                raise FrameError("frame_not_object", fatal=False)
            out.append(obj)
        return out

    def pending(self) -> int:
        return len(self._buffer)

    def reset(self) -> None:
        self._buffer.clear()


def encode_frame(obj: Dict[str, Any]) -> bytes:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return payload.encode("utf-8") + b"\n"


# ---------------------------------------------------------------------------
# 启动器消息
# ---------------------------------------------------------------------------


def worker_child_started(run_id: str, pid: int) -> Dict[str, Any]:
    return {"type": W_CHILD_STARTED, "run_id": run_id, "pid": int(pid)}


def worker_output(run_id: str, chunk: bytes) -> Dict[str, Any]:
    return {
        "type": W_OUTPUT,
        "run_id": run_id,
        "data": base64.b64encode(chunk).decode("ascii"),
    }


def worker_root_exited(run_id: str, exit_code: Optional[int]) -> Dict[str, Any]:
    return {"type": W_ROOT_EXITED, "run_id": run_id, "exit_code": exit_code}


def worker_child_exit(run_id: str) -> Dict[str, Any]:
    return {"type": W_CHILD_EXIT, "run_id": run_id}


def worker_launch_error(run_id: str, code: str, message: str, **details: Any) -> Dict[str, Any]:
    return {
        "type": W_LAUNCH_ERROR,
        "run_id": run_id,
        "error": {"code": code, "message": message, "details": details},
    }


def decode_worker_output(msg: Dict[str, Any]) -> bytes:
    data = msg.get("data")
    if not isinstance(data, str):
        raise FrameError("worker_output_missing_data")
    try:
        return base64.b64decode(data, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise FrameError(f"worker_output_bad_base64: {exc}") from exc


def validate_ipc_request(msg: Dict[str, Any], authed: bool) -> Tuple[str, str, Dict[str, Any]]:
    """校验一条 IPC 请求，返回 (id, method, params)。"""
    req_id = msg.get("id")
    if not isinstance(req_id, str) or not req_id:
        raise FrameError("ipc_missing_id", fatal=False)
    method = msg.get("method")
    if not isinstance(method, str) or not method:
        raise FrameError("ipc_missing_method", fatal=False)
    if not authed and method != M_HELLO:
        raise FrameError("ipc_not_authenticated", fatal=False)
    if authed and method == M_HELLO:
        raise FrameError("ipc_hello_twice", fatal=False)
    if authed and method not in IPC_METHODS:
        raise FrameError(f"ipc_unknown_method:{method}", fatal=False)
    params = msg.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise FrameError("ipc_params_not_object", fatal=False)
    return req_id, method, params


def ipc_ok(req_id: str, result: Any) -> Dict[str, Any]:
    return {"id": req_id, "ok": True, "result": result, "error": None}


def ipc_err(req_id: str, code: str, message: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "id": req_id,
        "ok": False,
        "result": None,
        "error": {"code": code, "message": message, "details": details or {}},
    }
