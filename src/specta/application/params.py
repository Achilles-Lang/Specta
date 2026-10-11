"""IPC 方法参数解析与校验。

集中在此，保证 Daemon 侧对每个方法的输入校验只有一份实现；桥只做转发，不重复
业务校验。所有函数在参数非法时抛出 SpectaError(INVALID_ARGUMENT)。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ..domain.errors import ErrorCode, SpectaError
from ..domain.models import (
    ServiceSpec,
    ensure_service_id,
    spec_from_legacy,
)
from ..logs.query import MAX_SELECT_LINES

LEGACY_KEYS = ("name", "command", "cwd", "port", "ready_when", "ephemeral")


def _require_str(params: Dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SpectaError(ErrorCode.INVALID_ARGUMENT, f"{key} 必须是非空字符串", {"field": key})
    return value


def _optional_str(params: Dict[str, Any], key: str) -> Optional[str]:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SpectaError(ErrorCode.INVALID_ARGUMENT, f"{key} 必须是非空字符串", {"field": key})
    return value


def service_id_param(params: Dict[str, Any]) -> str:
    return ensure_service_id(_require_str(params, "service_id"))


def request_key_param(params: Dict[str, Any]) -> Optional[str]:
    value = params.get("request_key")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SpectaError(ErrorCode.INVALID_ARGUMENT, "request_key 必须是非空字符串")
    return value


def parse_start(params: Dict[str, Any]) -> Tuple[Optional[str], Optional[ServiceSpec], bool]:
    """返回 (service_id, spec, update_existing)。"""
    raw_spec = params.get("spec")
    legacy_present = [k for k in LEGACY_KEYS if params.get(k) is not None]

    if raw_spec is not None and legacy_present:
        raise SpectaError(
            ErrorCode.INVALID_ARGUMENT,
            "spec 与顶层旧字段不能同时提供",
            {"conflicting": legacy_present},
        )

    service_id = params.get("service_id")
    if service_id is not None:
        service_id = ensure_service_id(service_id)

    update_existing = bool(params.get("update_existing", False))

    if raw_spec is not None:
        if not isinstance(raw_spec, dict):
            raise SpectaError(ErrorCode.INVALID_ARGUMENT, "spec 必须是对象")
        payload = dict(raw_spec)
        if service_id and "service_id" not in payload:
            payload["service_id"] = service_id
        payload.pop("origin", None)  # origin 由服务器维护
        spec = ServiceSpec.from_dict(payload)
        if service_id and spec.service_id != service_id:
            raise SpectaError(ErrorCode.INVALID_ARGUMENT, "spec.service_id 与 service_id 不一致")
        return spec.service_id or service_id, spec, update_existing

    if legacy_present:
        spec = spec_from_legacy(
            service_id=service_id,
            name=params.get("name"),
            command=params.get("command"),
            cwd=params.get("cwd"),
            port=params.get("port"),
            ready_when=params.get("ready_when"),
            ephemeral=bool(params.get("ephemeral", False)),
        )
        return spec.service_id, spec, update_existing

    if service_id is None:
        raise SpectaError(
            ErrorCode.INVALID_ARGUMENT,
            "必须提供 service_id，或提供完整 spec / 顶层 name+command+cwd 以注册新服务",
        )
    return service_id, None, update_existing


def parse_logs(params: Dict[str, Any]) -> Dict[str, Any]:
    service_id = service_id_param(params)
    lines = params.get("lines", 100)
    if isinstance(lines, bool) or not isinstance(lines, int):
        raise SpectaError(ErrorCode.INVALID_ARGUMENT, "lines 必须是 1-500 的整数")
    if not (1 <= lines <= MAX_SELECT_LINES):
        raise SpectaError(ErrorCode.INVALID_ARGUMENT, f"lines 必须在 1-{MAX_SELECT_LINES} 之间")
    filter_error = params.get("filter_error", False)
    if not isinstance(filter_error, bool):
        raise SpectaError(ErrorCode.INVALID_ARGUMENT, "filter_error 必须是布尔值")
    run_id = params.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise SpectaError(ErrorCode.INVALID_ARGUMENT, "run_id 必须是非空字符串或 'all'")
    after_sequence = params.get("after_sequence")
    if after_sequence is not None:
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
            raise SpectaError(ErrorCode.INVALID_ARGUMENT, "after_sequence 必须是非负整数")
    return {
        "service_id": service_id,
        "lines": lines,
        "filter_error": filter_error,
        "run_id": run_id,
        "after_sequence": after_sequence,
    }


def parse_report(params: Dict[str, Any]) -> Dict[str, Any]:
    service_id = service_id_param(params)
    run_id = _require_str(params, "run_id")
    state = _require_str(params, "state")
    reason = params.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise SpectaError(ErrorCode.INVALID_ARGUMENT, "reason 必须是非空文本")
    return {"service_id": service_id, "run_id": run_id, "state": state, "reason": reason}


def parse_operation(params: Dict[str, Any]) -> Dict[str, Any]:
    return {"operation_id": _require_str(params, "operation_id")}
