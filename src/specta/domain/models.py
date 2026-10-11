"""领域数据模型：配置、运行事实、Agent 判断、日志记录、操作记录。

本模块只依赖标准库，不导入 Qt / MCP / psutil，便于单元测试与跨进程复用
（桥、启动器都可以安全导入）。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import ErrorCode, SpectaError, invalid_argument

# ---------------------------------------------------------------------------
# 限额（规格 6.1）
# ---------------------------------------------------------------------------

MAX_SERVICES = 50
MAX_RUNNING_SERVICES = 10
MAX_PORTS_PER_SERVICE = 16
MAX_ENV_ENTRIES = 64
MAX_ARGS = 64
MAX_NAME_LEN = 100
MAX_REASON_LEN = 2000
MAX_READY_REGEX_LEN = 256

ID_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}\Z")
SLUG_PATTERN = re.compile(r"[^a-zA-Z0-9._-]+")

SUPPORTED_ENCODINGS = ("utf-8", "gb18030")

# lifecycle 事实状态（规格 7）
LIFECYCLE_STOPPED = "stopped"
LIFECYCLE_STARTING = "starting"
LIFECYCLE_RUNNING = "running"
LIFECYCLE_STOPPING = "stopping"
LIFECYCLE_EXITED = "exited"
LIFECYCLE_FAILED = "failed"

TERMINAL_LIFECYCLES = (LIFECYCLE_STOPPED, LIFECYCLE_EXITED, LIFECYCLE_FAILED)

# Agent 判断状态（规格 4 / 5）
ASSESS_UNKNOWN = "unknown"
ASSESS_READY = "ready"
ASSESS_ERROR = "error"

# 操作状态
OP_QUEUED = "queued"
OP_RUNNING = "running"
OP_SUCCEEDED = "succeeded"
OP_FAILED = "failed"

ACTION_START = "start"
ACTION_STOP = "stop"
ACTION_RESTART = "restart"

ORIGIN_USER = "user"
ORIGIN_AGENT = "agent"

LAUNCH_MODE_SHELL = "shell"
LAUNCH_MODE_EXEC = "exec"

LOG_KIND_OUTPUT = "output"
LOG_KIND_EVENT = "event"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_rfc3339(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_rfc3339(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        value = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex


def new_operation_id() -> str:
    return "op_" + uuid.uuid4().hex


def new_session_id() -> str:
    return "bridge_" + uuid.uuid4().hex


def slugify(name: str, fallback: str = "service") -> str:
    slug = SLUG_PATTERN.sub("-", name.strip()).strip("-._").lower()
    slug = slug[:64]
    if not slug or not ID_PATTERN.match(slug):
        return fallback
    return slug


# ---------------------------------------------------------------------------
# 启动方式与可保存配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadyWhen:
    """Agent 提供的就绪观察条件；命中与否与服务整体健康状态分开。"""

    port: Optional[int] = None
    log_regex: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if self.port is not None:
            out["port"] = self.port
        if self.log_regex is not None:
            out["log_regex"] = self.log_regex
        return out

    @staticmethod
    def from_dict(data: Any) -> Optional["ReadyWhen"]:
        if data is None:
            return None
        if not isinstance(data, dict):
            raise invalid_argument("ready_when 必须是对象")
        port = data.get("port")
        log_regex = data.get("log_regex")
        if port is not None:
            port = _coerce_port(port, "ready_when.port")
        if log_regex is not None:
            if not isinstance(log_regex, str) or not log_regex.strip():
                raise invalid_argument("ready_when.log_regex 必须是非空字符串")
            if len(log_regex) > MAX_READY_REGEX_LEN:
                raise invalid_argument(
                    f"ready_when.log_regex 长度不能超过 {MAX_READY_REGEX_LEN}"
                )
        if port is None and log_regex is None:
            return None
        return ReadyWhen(port=port, log_regex=log_regex)


@dataclass(frozen=True)
class LaunchSpec:
    """启动方式。两种模式互斥：shell（cmd /d /s /c）与 exec（程序 + 参数）。"""

    mode: str
    command: Optional[str] = None
    program: Optional[str] = None
    args: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        if self.mode == LAUNCH_MODE_SHELL:
            return {"mode": self.mode, "command": self.command}
        return {"mode": self.mode, "program": self.program, "args": list(self.args)}

    @staticmethod
    def from_dict(data: Any) -> "LaunchSpec":
        if not isinstance(data, dict):
            raise invalid_argument("launch 必须是对象")
        mode = data.get("mode", LAUNCH_MODE_SHELL)
        if mode == LAUNCH_MODE_SHELL:
            command = data.get("command")
            if not isinstance(command, str) or not command.strip():
                raise invalid_argument("shell 模式必须提供非空 command")
            return LaunchSpec(mode=mode, command=command)
        if mode == LAUNCH_MODE_EXEC:
            program = data.get("program")
            if not isinstance(program, str) or not program.strip():
                raise invalid_argument("exec 模式必须提供非空 program")
            args = data.get("args", [])
            if isinstance(args, str):
                raise invalid_argument("exec 模式 args 必须是字符串数组")
            if not isinstance(args, (list, tuple)):
                raise invalid_argument("exec 模式 args 必须是字符串数组")
            if len(args) > MAX_ARGS:
                raise invalid_argument(f"args 项数不能超过 {MAX_ARGS}")
            norm_args: List[str] = []
            for item in args:
                if not isinstance(item, str):
                    raise invalid_argument("exec 模式 args 每项必须是字符串")
                norm_args.append(item)
            return LaunchSpec(mode=mode, program=program, args=tuple(norm_args))
        raise invalid_argument(f"未知的启动方式: {mode!r}")


@dataclass(frozen=True)
class ServiceSpec:
    """服务的可保存配置。"""

    service_id: str
    name: str
    launch: LaunchSpec
    cwd: str
    env: Dict[str, str] = field(default_factory=dict)
    ports: Tuple[int, ...] = ()
    encoding: str = "utf-8"
    ready_when: Optional[ReadyWhen] = None
    origin: str = ORIGIN_USER
    ephemeral: bool = False

    # -- 序列化 ------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "service_id": self.service_id,
            "name": self.name,
            "launch": self.launch.to_dict(),
            "cwd": self.cwd,
            "env": dict(self.env),
            "ports": list(self.ports),
            "encoding": self.encoding,
            "ready_when": self.ready_when.to_dict() if self.ready_when else None,
            "origin": self.origin,
            "ephemeral": bool(self.ephemeral),
        }

    @staticmethod
    def from_dict(data: Any) -> "ServiceSpec":
        if not isinstance(data, dict):
            raise invalid_argument("服务配置必须是对象")
        service_id = data.get("service_id")
        name = data.get("name")
        if not isinstance(service_id, str) or not ID_PATTERN.match(service_id):
            raise invalid_argument(
                "service_id 必须匹配 [a-zA-Z0-9][a-zA-Z0-9._-]{0,63}"
            )
        if not isinstance(name, str) or not name.strip():
            raise invalid_argument("name 必须是非空字符串")
        if len(name) > MAX_NAME_LEN:
            raise invalid_argument(f"name 长度不能超过 {MAX_NAME_LEN}")

        launch = LaunchSpec.from_dict(data.get("launch"))

        cwd = data.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            raise invalid_argument("cwd 必须是非空字符串")
        cwd_path = Path(cwd)
        if not cwd_path.is_absolute():
            raise invalid_argument("cwd 必须是绝对路径")

        env = data.get("env", {}) or {}
        if not isinstance(env, dict):
            raise invalid_argument("env 必须是键值对象")
        if len(env) > MAX_ENV_ENTRIES:
            raise invalid_argument(f"env 项数不能超过 {MAX_ENV_ENTRIES}")
        norm_env: Dict[str, str] = {}
        for key, value in env.items():
            if not isinstance(key, str) or not key:
                raise invalid_argument("env 键必须是非空字符串")
            if value is None:
                value = ""
            if not isinstance(value, str):
                value = str(value)
            norm_env[key] = value

        raw_ports = data.get("ports", []) or []
        if isinstance(raw_ports, (int, str)):
            raw_ports = [raw_ports]
        if not isinstance(raw_ports, (list, tuple)):
            raise invalid_argument("ports 必须是整数数组")
        ports: List[int] = []
        for item in raw_ports:
            port = _coerce_port(item, "ports")
            if port not in ports:
                ports.append(port)
        if len(ports) > MAX_PORTS_PER_SERVICE:
            raise invalid_argument(f"每服务最多声明 {MAX_PORTS_PER_SERVICE} 个端口")

        encoding = data.get("encoding") or "utf-8"
        if encoding not in SUPPORTED_ENCODINGS:
            raise invalid_argument(
                f"encoding 只支持 {', '.join(SUPPORTED_ENCODINGS)}"
            )

        ready_when = ReadyWhen.from_dict(data.get("ready_when"))
        if ready_when and ready_when.port is not None and ready_when.port not in ports:
            raise invalid_argument("ready_when.port 必须包含在 ports 中")

        origin = data.get("origin", ORIGIN_USER)
        if origin not in (ORIGIN_USER, ORIGIN_AGENT):
            raise invalid_argument("origin 只能是 user 或 agent")

        return ServiceSpec(
            service_id=service_id,
            name=name,
            launch=launch,
            cwd=str(cwd_path),
            env=norm_env,
            ports=tuple(ports),
            encoding=encoding,
            ready_when=ready_when,
            origin=origin,
            ephemeral=bool(data.get("ephemeral", False)),
        )

    # -- 语义比较 ----------------------------------------------------------
    def equivalent_to(self, other: "ServiceSpec") -> bool:
        """忽略 origin / ephemeral 的配置等价比较（规格 7 的配置冲突判定）。"""
        return (
            _norm_path(self.cwd) == _norm_path(other.cwd)
            and self.launch == other.launch
            and dict(self.env) == dict(other.env)
            and tuple(self.ports) == tuple(other.ports)
            and self.encoding == other.encoding
            and self.ready_when == other.ready_when
            and self.name == other.name
        )

    def with_origin(self, origin: str) -> "ServiceSpec":
        return replace(self, origin=origin)


def _norm_path(path: str) -> str:
    try:
        return str(Path(path).resolve()).casefold()
    except OSError:  # pragma: no cover - 极端路径
        return path.casefold()


def _coerce_port(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise invalid_argument(f"{label} 必须是 1-65535 的整数")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        raise invalid_argument(f"{label} 必须是 1-65535 的整数")
    if not (1 <= value <= 65535):
        raise invalid_argument(f"{label} 必须在 1-65535 之间")
    return value


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """字符区间样式。索引基于 Unicode code point 的半开区间 [start, end)。"""

    start: int
    end: int
    fg: Optional[str] = None
    bg: Optional[str] = None
    bold: bool = False
    underline: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "fg": self.fg,
            "bg": self.bg,
            "bold": self.bold,
            "underline": self.underline,
        }


@dataclass
class LogRecord:
    sequence: int
    run_id: Optional[str]
    ts: str
    text: str
    spans: List[Span] = field(default_factory=list)
    kind: str = LOG_KIND_OUTPUT
    provisional: bool = False
    # 内部元信息（不外传）
    started_at: float = 0.0
    wrapped_from: Optional[int] = None
    error_hit: bool = False

    def to_dict(self, with_spans: bool = True) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "sequence": self.sequence,
            "run_id": self.run_id,
            "ts": self.ts,
            "text": self.text,
            "kind": self.kind,
            "provisional": self.provisional,
        }
        if with_spans:
            out["spans"] = [s.to_dict() for s in self.spans]
        return out

    def meta_dict(self) -> Dict[str, Any]:
        """MCP 返回：不含颜色跨度，节约上下文。"""
        return self.to_dict(with_spans=False)


# ---------------------------------------------------------------------------
# 运行事实、判断、操作
# ---------------------------------------------------------------------------


@dataclass
class PortObservation:
    port: int
    listening: bool
    owner_pids: List[int] = field(default_factory=list)
    owned_by_service: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "port": self.port,
            "listening": self.listening,
            "owner_pids": list(self.owner_pids),
            "owned_by_service": self.owned_by_service,
        }


@dataclass
class ServiceFacts:
    run_id: Optional[str] = None
    lifecycle: str = LIFECYCLE_STOPPED
    pid: Optional[int] = None
    launcher_pid: Optional[int] = None
    started_at: Optional[str] = None
    exit_code: Optional[int] = None
    stop_requested: bool = False
    port_observations: List[PortObservation] = field(default_factory=list)
    ready_observation: Optional[Dict[str, Any]] = None
    error_match_count: int = 0
    last_error: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "lifecycle": self.lifecycle,
            "pid": self.pid,
            "launcher_pid": self.launcher_pid,
            "started_at": self.started_at,
            "exit_code": self.exit_code,
            "stop_requested": self.stop_requested,
            "port_observations": [p.to_dict() for p in self.port_observations],
            "ready_observation": self.ready_observation,
            "error_match_count": self.error_match_count,
            "last_error": self.last_error,
        }


@dataclass
class AgentAssessment:
    run_id: Optional[str]
    state: str
    reason: str
    reported_at: str
    session_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "reason": self.reason,
            "reported_at": self.reported_at,
            "session_id": self.session_id,
        }


@dataclass
class Operation:
    operation_id: str
    service_id: str
    action: str
    source: str  # ui / agent
    session_id: Optional[str] = None
    request_key: Optional[str] = None
    status: str = OP_QUEUED
    accepted_at: str = field(default_factory=lambda: to_rfc3339(utc_now()) or "")
    completed_at: Optional[str] = None
    run_id: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "service_id": self.service_id,
            "action": self.action,
            "source": self.source,
            "session_id": self.session_id,
            "status": self.status,
            "accepted_at": self.accepted_at,
            "completed_at": self.completed_at,
            "run_id": self.run_id,
            "result": self.result,
            "error": self.error,
        }


def build_snapshot(
    spec: ServiceSpec,
    facts: ServiceFacts,
    assessment: Optional[AgentAssessment],
    config_revision: int,
    active_operation: Optional[Operation],
    last_operation: Optional[Operation],
    queued_operations: int,
    log_meta: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "service_id": spec.service_id,
        "name": spec.name,
        "origin": spec.origin,
        "ephemeral": spec.ephemeral,
        "cwd": spec.cwd,
        "ports": list(spec.ports),
        "config_revision": config_revision,
        "facts": facts.to_dict(),
        "assessment": assessment.to_dict() if assessment else None,
        "active_operation_id": active_operation.operation_id if active_operation else None,
        "last_operation": last_operation.to_dict() if last_operation else None,
        "queued_operations": queued_operations,
        "logs": dict(log_meta),
    }


def summarize_spec(spec: ServiceSpec) -> Dict[str, Any]:
    """list_services 用的摘要，不返回 env。"""
    return {
        "service_id": spec.service_id,
        "name": spec.name,
        "origin": spec.origin,
        "cwd": spec.cwd,
        "launch": spec.launch.to_dict(),
        "ports": list(spec.ports),
        "ephemeral": spec.ephemeral,
    }


def spec_from_legacy(
    service_id: Optional[str],
    name: Optional[str],
    command: Optional[str],
    cwd: Optional[str],
    port: Optional[int] = None,
    ready_when: Any = None,
    ephemeral: bool = False,
) -> ServiceSpec:
    """兼容旧调用：顶层 name/command/cwd/port 转换为 shell spec。"""
    if not name or not command or not cwd:
        raise invalid_argument("新注册服务必须提供 name、command、cwd")
    payload: Dict[str, Any] = {
        "service_id": service_id or slugify(name),
        "name": name,
        "launch": {"mode": LAUNCH_MODE_SHELL, "command": command},
        "cwd": cwd,
        "ports": [port] if port is not None else [],
        "ready_when": ready_when,
        "ephemeral": ephemeral,
    }
    return ServiceSpec.from_dict(payload)


def ensure_service_id(value: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise SpectaError(
            ErrorCode.INVALID_ARGUMENT,
            "service_id 必须匹配 [a-zA-Z0-9][a-zA-Z0-9._-]{0,63}",
            {"service_id": value},
        )
    return value


def ensure_list(value: Any, allowed: Sequence[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise invalid_argument(f"{label} 只能是 {'/'.join(allowed)}")
    return value
