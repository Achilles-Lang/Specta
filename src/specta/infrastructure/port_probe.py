"""端口探测。

覆盖 IPv4/IPv6 与通配监听。查询失败不能当成端口空闲 —— 必须显式返回
PORT_CHECK_FAILED，避免把"查不到"误判为"没占用"。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from ..domain.errors import ErrorCode, SpectaError
from ..domain.models import PortObservation

try:
    import psutil
except ImportError:  # pragma: no cover - 依赖缺失
    psutil = None  # type: ignore[assignment]

WILDCARD_HOSTS = {"0.0.0.0", "::", "::0", "*"}


class PortProbeError(SpectaError):
    def __init__(self, message: str, **details) -> None:
        super().__init__(ErrorCode.PORT_CHECK_FAILED, message, details or None)


def _listeners() -> Dict[int, List[int]]:
    """返回 {port: [pid, ...]}，包含通配监听。"""
    if psutil is None:
        raise PortProbeError("psutil 不可用，无法查询端口")
    result: Dict[int, List[int]] = {}
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError) as exc:
        raise PortProbeError(f"查询端口失败: {exc}") from exc
    for conn in conns:
        if conn.status != psutil.CONN_LISTEN:
            continue
        if not conn.laddr:
            continue
        port = conn.laddr.port
        pid = conn.pid
        bucket = result.setdefault(int(port), [])
        if pid is not None and pid not in bucket:
            bucket.append(int(pid))
    return result


def _owned_pids(service_pids: Optional[Sequence[int]]) -> set:
    """服务「所属进程集合」= 记录的 pid 及其全部后代。

    端口常常不是由被直接创建的那个进程绑定的：`npm run dev` 由 node 子进程监听、
    受控启动器由真正的解释器监听（venv redirector 场景）、gunicorn 由 worker 监听等。
    只比对 pid/launcher_pid 会把"自己的服务"误判成外部占用，因此这里按进程树判断。
    """
    if psutil is None:
        return set()
    owned = set()
    for pid in service_pids or ():
        if not pid:
            continue
        pid = int(pid)
        owned.add(pid)
        try:
            for child in psutil.Process(pid).children(recursive=True):
                owned.add(child.pid)
        except psutil.Error:
            continue
    return owned


def snapshot(ports: Sequence[int], service_pids: Optional[Sequence[int]] = None) -> List[PortObservation]:
    """查询若干端口的监听状态与占用 PID。"""
    listeners = _listeners()
    owned = _owned_pids(service_pids)
    observations: List[PortObservation] = []
    for port in ports:
        pids = listeners.get(int(port), [])
        obs = PortObservation(
            port=int(port),
            listening=bool(pids),
            owner_pids=list(pids),
            owned_by_service=bool(pids) and all(p in owned for p in pids),
        )
        observations.append(obs)
    return observations


def check_available(port: int) -> Tuple[bool, List[int]]:
    """启动前预检：返回 (是否可用, 占用者 PID 列表)。"""
    listeners = _listeners()
    pids = listeners.get(int(port), [])
    return (not pids, list(pids))


def first_conflict(ports: Sequence[int]) -> Optional[Tuple[int, List[int]]]:
    for port in ports:
        ok, pids = check_available(port)
        if not ok:
            return int(port), pids
    return None
