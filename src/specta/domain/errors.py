"""统一错误码与异常。

规格第 13 节定义了全部错误码。应用层与 IPC 层、桥层都使用这里的同一种结构，
以保证工具返回、IPC 响应、UI 行内提示三处得到一致的 code/message/details。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class ErrorCode:
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    NOT_FOUND = "NOT_FOUND"
    CONFIG_CONFLICT = "CONFIG_CONFLICT"
    CONFIG_READ_ONLY = "CONFIG_READ_ONLY"
    PORT_IN_USE = "PORT_IN_USE"
    PORT_CHECK_FAILED = "PORT_CHECK_FAILED"
    JOB_ASSIGN_FAILED = "JOB_ASSIGN_FAILED"
    LAUNCH_FAILED = "LAUNCH_FAILED"
    LAUNCH_PROTOCOL_ERROR = "LAUNCH_PROTOCOL_ERROR"
    STOP_TIMEOUT = "STOP_TIMEOUT"
    PORT_RELEASE_TIMEOUT = "PORT_RELEASE_TIMEOUT"
    STALE_RUN = "STALE_RUN"
    SAVE_FAILED = "SAVE_FAILED"
    BUSY = "BUSY"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    REQUEST_KEY_CONFLICT = "REQUEST_KEY_CONFLICT"
    OPERATION_PENDING = "OPERATION_PENDING"
    DAEMON_LAUNCH_FAILED = "DAEMON_LAUNCH_FAILED"
    DAEMON_UNAVAILABLE = "DAEMON_UNAVAILABLE"
    AUTH_FAILED = "AUTH_FAILED"
    PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"
    CONNECTION_LOST = "CONNECTION_LOST"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class SpectaError(Exception):
    """带结构化错误码的应用异常。"""

    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: Dict[str, Any] = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        if self.details:
            return f"{self.code}: {self.message} {self.details}"
        return f"{self.code}: {self.message}"


def invalid_argument(message: str, **details: Any) -> SpectaError:
    return SpectaError(ErrorCode.INVALID_ARGUMENT, message, details or None)


def not_found(message: str, **details: Any) -> SpectaError:
    return SpectaError(ErrorCode.NOT_FOUND, message, details or None)


def internal_error(message: str, **details: Any) -> SpectaError:
    return SpectaError(ErrorCode.INTERNAL_ERROR, message, details or None)
