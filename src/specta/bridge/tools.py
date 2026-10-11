"""MCP 工具契约（规格第 10 节）。

第一版固定暴露 7 个工具。只接受声明字段；成功与失败都返回统一的结构化信封
`{ok, data, error}`（`error` 仅在失败时非空）。工具只做参数整理与调用转发，
业务校验在 Daemon 侧（application/params.py）。

协议错误标志（审查报告 F13）：业务失败（信封 ok=false）必须让 MCP 的
`CallToolResult.is_error=True`，否则 Agent/宿主会把失败误判为协议成功。因此所有工具统一
返回 `CallToolResult`：`structured_content` 即 `{ok,data,error}`，正文为其 JSON，
失败时置 `is_error=True`。这样成功/失败形态一致，也与 README 展示的直接信封一致
（不再受 SDK 默认 `{"result": ...}` 外层包裹影响）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from .. import __version__
from ..domain.errors import ErrorCode, SpectaError
from ..infrastructure.daemon_client import DaemonClient

SERVER_NAME = "specta"

TERMINAL_STATUSES = ("succeeded", "failed")

INSTRUCTIONS = (
    "Specta 是一个本地开发运行台：它托管开发进程、保留多路日志，并向你提供启停、"
    "状态查询与错误上下文读取。\n"
    "典型流程：list_services → （必要时）start_service → get_service_status / "
    "get_recent_logs → 自己判断就绪/异常 → report_service_state → 改代码后 "
    "restart_service 并再次检查。\n"
    "注意：start_service 成功仅表示目标进程创建成功，不代表 HTTP 健康或应用就绪；"
    "请继续用 get_service_status 与 get_recent_logs 确认。日志只在内存中保留"
    "（每服务 3000 行 / 4MiB 内容 / 2MiB 原始字节），Daemon 退出后不恢复。"
)


# ---------------------------------------------------------------------------
# 返回封装
# ---------------------------------------------------------------------------
def _ok(data: Any) -> Dict[str, Any]:
    return {"ok": True, "data": data, "error": None}


def _err(code: str, message: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"ok": False, "data": None, "error": {"code": code, "message": message, "details": details or {}}}


def _tool_result(payload: Dict[str, Any]) -> CallToolResult:
    """把工具信封统一成 `CallToolResult`，使协议错误标志与业务错误一致（F13）。

    - 成功：`is_error=False`，`structured_content={ok:true,data,error:null}`。
    - 失败：`is_error=True`，`structured_content={ok:false,data:null,error:{code,message,details}}`。

    两种形态完全一致（均为直接信封，无 SDK 默认 `result` 外层包裹），正文为同一份 JSON，
    机器与模型两侧读到的内容一致。
    """
    body = json.dumps(payload, ensure_ascii=False)
    return CallToolResult(
        content=[TextContent(type="text", text=body)],
        structured_content=payload,
        is_error=not payload.get("ok", False),
    )


async def _call(client: DaemonClient, method: str, params: Dict[str, Any], *, read: bool) -> Dict[str, Any]:
    try:
        result = await client.call(method, params, retry_read=read)
    except SpectaError as exc:
        return _err(exc.code, exc.message, exc.details)
    except Exception as exc:  # noqa: BLE001
        return _err(ErrorCode.INTERNAL_ERROR, f"桥调用失败: {exc}")
    return _ok(result)


def _operation_result(operation: Dict[str, Any]) -> Dict[str, Any]:
    operation_id = operation.get("operation_id")
    if operation.get("status") == "succeeded":
        return _ok(
            {
                "operation_id": operation_id,
                "status": operation.get("status"),
                "run_id": operation.get("run_id"),
                "result": operation.get("result"),
                "snapshot": None,
            }
        )
    error = operation.get("error") or {}
    details = dict(error.get("details") or {})
    if operation_id:
        details.setdefault("operation_id", operation_id)
    return _err(
        error.get("code", ErrorCode.INTERNAL_ERROR),
        error.get("message", "操作失败"),
        details,
    )


async def _submit(
    client: DaemonClient, method: str, params: Dict[str, Any], request_key: Optional[str]
) -> Dict[str, Any]:
    payload = dict(params)
    payload["request_key"] = request_key or uuid.uuid4().hex
    response = await _call(client, method, payload, read=False)
    if not response["ok"]:
        return response
    operation = ((response.get("data") or {}).get("operation")) or {}
    if operation.get("status") in TERMINAL_STATUSES:
        return _operation_result(operation)
    operation_id = operation.get("operation_id")
    if not operation_id:
        return _err(ErrorCode.INTERNAL_ERROR, "Daemon 未返回 operation_id")
    completed = await client.wait_operation(operation_id)
    if completed.get("pending"):
        return _err(
            ErrorCode.OPERATION_PENDING,
            f"操作仍在执行或结果未知（{operation_id}）；请用 get_service_status 查询，不要盲目重做",
            {"operation_id": operation_id, "status": completed.get("status")},
        )
    return _operation_result(completed)


# ---------------------------------------------------------------------------
# 工具注册
# ---------------------------------------------------------------------------
def register_tools(server: MCPServer, client: DaemonClient) -> None:
    @server.tool(
        name="list_services",
        description="列出所有已注册服务（ID、名称、来源、cwd、启动方式、端口与当前状态）。不返回环境变量。",
    )
    async def list_services() -> CallToolResult:
        return _tool_result(await _call(client, "list_services", {}, read=True))

    @server.tool(
        name="get_service_status",
        description=(
            "取得服务的完整快照：进程事实（lifecycle/pid/退出码/端口观察/错误命中数）、"
            "Agent 判断、当前与最近一次操作、日志元信息。进程存在不等于服务就绪。"
        ),
    )
    async def get_service_status(service_id: str) -> CallToolResult:
        return _tool_result(await _call(client, "get_service_status", {"service_id": service_id}, read=True))

    @server.tool(
        name="start_service",
        description=(
            "启动服务。新 ID 必须同时提供完整 spec（name/launch/cwd，可选 env/ports/encoding/"
            "ready_when/ephemeral）。已有 ID 可以只给 service_id。"
            "成功只代表目标进程创建成功，不代表应用就绪——请继续查询状态与日志。"
        ),
    )
    async def start_service(
        service_id: Optional[str] = None,
        spec: Optional[Dict[str, Any]] = None,
        update_existing: bool = False,
        request_key: Optional[str] = None,
        name: Optional[str] = None,
        command: Optional[str] = None,
        cwd: Optional[str] = None,
        port: Optional[int] = None,
        ready_when: Optional[Dict[str, Any]] = None,
        ephemeral: Optional[bool] = None,
    ) -> CallToolResult:
        params: Dict[str, Any] = {}
        if service_id is not None:
            params["service_id"] = service_id
        if spec is not None:
            params["spec"] = spec
        if update_existing:
            params["update_existing"] = True
        if name is not None:
            params["name"] = name
        if command is not None:
            params["command"] = command
        if cwd is not None:
            params["cwd"] = cwd
        if port is not None:
            params["port"] = port
        if ready_when is not None:
            params["ready_when"] = ready_when
        if ephemeral is not None:
            params["ephemeral"] = ephemeral
        return _tool_result(await _submit(client, "start_service", params, request_key))

    @server.tool(
        name="restart_service",
        description="重启服务：停止整组进程 → 确认端口释放 → 按已保存配置重新启动。返回新的 run_id。",
    )
    async def restart_service(service_id: str, request_key: Optional[str] = None) -> CallToolResult:
        return _tool_result(await _submit(client, "restart_service", {"service_id": service_id}, request_key))

    @server.tool(
        name="stop_service",
        description="停止服务：硬终止整组进程并清理 Job（保留配置）。对已停止的服务返回成功且无操作。",
    )
    async def stop_service(service_id: str, request_key: Optional[str] = None) -> CallToolResult:
        return _tool_result(await _submit(client, "stop_service", {"service_id": service_id}, request_key))

    @server.tool(
        name="get_recent_logs",
        description=(
            "读取日志。lines 为最大逻辑行数（1-500）。filter_error=true 时返回完整错误块"
            "（含默认前后各 8 行上下文，保留堆栈）。默认只返回当前/最后一次运行；"
            "跨运行查询请传 run_id='all'，此时每条记录带各自 run_id。"
            "after_sequence 只用于读取新行；若返回的 revision 表明活动行被改写，需重新取完整快照。"
        ),
    )
    async def get_recent_logs(
        service_id: str,
        lines: int = 100,
        filter_error: bool = False,
        run_id: Optional[str] = None,
        after_sequence: Optional[int] = None,
    ) -> CallToolResult:
        params: Dict[str, Any] = {
            "service_id": service_id,
            "lines": lines,
            "filter_error": filter_error,
        }
        if run_id is not None:
            params["run_id"] = run_id
        if after_sequence is not None:
            params["after_sequence"] = after_sequence
        return _tool_result(await _call(client, "get_recent_logs", params, read=True))

    @server.tool(
        name="report_service_state",
        description=(
            "上报你对当前运行的判断：state 为 ready / error / unknown，reason 为非空说明"
            "（最多 2000 字）。必须携带当前 run_id；旧运行或已退出运行的迟到判断会被拒绝。"
            "unknown 表示撤销当前运行的明确判断。"
        ),
    )
    async def report_service_state(
        service_id: str, run_id: str, state: str, reason: str
    ) -> CallToolResult:
        return _tool_result(
            await _call(
                client,
                "report_service_state",
                {"service_id": service_id, "run_id": run_id, "state": state, "reason": reason},
                read=False,
            )
        )


def build_server(client: DaemonClient) -> MCPServer:
    server = MCPServer(
        name=SERVER_NAME,
        title="Specta",
        instructions=INSTRUCTIONS,
        version=__version__,
    )
    register_tools(server, client)
    return server
