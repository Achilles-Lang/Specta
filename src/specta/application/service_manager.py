"""ServiceManager：应用操作的统一入口。

UI 与 IPC 是两个入口，但都汇聚到这里，不能各自实现一套启停逻辑。它负责服务注册、
配置冲突规则、来源权限、启停协调、运行状态查询、Agent 判断上报、日志查询，以及
端口探测与重启时的端口释放观察。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, QTimer, Signal

from ..domain.errors import ErrorCode, SpectaError
from ..domain.models import (
    ACTION_RESTART,
    ACTION_START,
    ACTION_STOP,
    ASSESS_ERROR,
    ASSESS_READY,
    ASSESS_UNKNOWN,
    LIFECYCLE_FAILED,
    LIFECYCLE_RUNNING,
    LIFECYCLE_STARTING,
    MAX_RUNNING_SERVICES,
    MAX_SERVICES,
    ORIGIN_AGENT,
    ORIGIN_USER,
    OP_FAILED,
    OP_SUCCEEDED,
    Operation,
    PortObservation,
    ServiceSpec,
    ensure_service_id,
    slugify,
    summarize_spec,
    to_rfc3339,
    utc_now,
)
from ..infrastructure import port_probe
from ..infrastructure.repository import LoadResult, ServiceRepository
from ..logs import query as log_query
from .managed_service import ManagedService
from .operation_registry import OperationRegistry

PORT_RELEASE_TIMEOUT_S = 5.0
PORT_RELEASE_POLL_S = 0.2
PROBE_INTERVAL_S = 2.0


class _ProbeBridge(QObject):
    probed = Signal(str, object)
    release_checked = Signal(str, str, object)


class ServiceManager(QObject):
    service_added = Signal(str)
    service_removed = Signal(str)
    service_changed = Signal(str)
    logs_changed = Signal(str, int, int)
    operation_changed = Signal(str)
    assessment_changed = Signal(str)
    error_occurred = Signal(str, dict)
    recovery_needed = Signal(object)

    def __init__(self, repository: ServiceRepository, context_lines_provider=None, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._repository = repository
        self._registry = OperationRegistry()
        self._services: Dict[str, ManagedService] = {}
        self._order: List[str] = []
        self._context_lines_provider = context_lines_provider
        self._bridge = _ProbeBridge()
        self._bridge.probed.connect(self._on_probed)
        self._bridge.release_checked.connect(self._on_release_checked)
        self._probe_stop = threading.Event()
        self._probe_thread: Optional[threading.Thread] = None
        self._pending_restart: Dict[str, str] = {}
        self._watch_active: Dict[str, bool] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def load_from_disk(self) -> LoadResult:
        result = self._repository.load()
        for spec in result.specs:
            self._register_internal(spec)
        if result.needs_recovery or result.issues:
            self.recovery_needed.emit(
                {"issues": result.issues, "future_schema": result.future_schema}
            )
        return result

    def start_probing(self) -> None:
        if self._probe_thread and self._probe_thread.is_alive():
            return
        self._probe_stop.clear()
        self._probe_thread = threading.Thread(target=self._probe_loop, name="specta-port-probe", daemon=True)
        self._probe_thread.start()

    def shutdown(self) -> None:
        self._probe_stop.set()
        for service in list(self._services.values()):
            service.shutdown()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_services(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for service_id in self._order:
            service = self._services.get(service_id)
            if service is None:
                continue
            summary = summarize_spec(service.spec)
            summary["lifecycle"] = service.facts.lifecycle
            summary["run_id"] = service.facts.run_id
            summary["pid"] = service.facts.pid
            out.append(summary)
        return out

    def service_ids(self) -> List[str]:
        return list(self._order)

    def get(self, service_id: str) -> ManagedService:
        service = self._services.get(service_id)
        if service is None:
            raise SpectaError(ErrorCode.NOT_FOUND, f"服务不存在: {service_id}", {"service_id": service_id})
        return service

    def get_snapshot(self, service_id: str) -> Dict[str, Any]:
        service = self.get(service_id)
        return service.snapshot(
            active_operation=self._registry.active(service_id),
            last_operation=self._registry.last_completed(service_id),
            queued=self._registry.queued_count(service_id),
        )

    def get_operation(self, operation_id: str) -> Dict[str, Any]:
        if not isinstance(operation_id, str) or not operation_id:
            raise SpectaError(ErrorCode.INVALID_ARGUMENT, "operation_id 必须是非空字符串")
        operation = self._registry.get(operation_id)
        if operation is None:
            raise SpectaError(
                ErrorCode.NOT_FOUND, f"操作不存在或已淘汰: {operation_id}", {"operation_id": operation_id}
            )
        return operation.to_dict()

    # ------------------------------------------------------------------
    # 注册 / 编辑 / 删除
    # ------------------------------------------------------------------
    def _register_internal(self, spec: ServiceSpec) -> ManagedService:
        service = ManagedService(spec, columns=self._default_columns(), parent=self)
        service.facts_changed.connect(self.service_changed)
        service.assessment_changed.connect(self.assessment_changed)
        service.logs_changed.connect(self.logs_changed)
        service.operation_done.connect(self._on_operation_done)
        service.stop_done_for_restart.connect(self._on_stop_done_for_restart)
        self._services[spec.service_id] = service
        if spec.service_id not in self._order:
            self._order.append(spec.service_id)
        return service

    def create_or_replace_user_service(self, spec: ServiceSpec) -> None:
        """UI 新增/编辑 user 服务。

        先构造候选配置集合并**落盘成功**，再提交内存变更 —— 保存失败时文件与内存都
        保持原样，不会出现"界面报错但服务其实已经注册/改名"的不一致。
        """
        ensure_service_id(spec.service_id)
        existing = self._services.get(spec.service_id)
        if existing is None:
            if len(self._services) >= MAX_SERVICES:
                raise SpectaError(
                    ErrorCode.LIMIT_EXCEEDED,
                    f"最多注册 {MAX_SERVICES} 个服务",
                    {"limit": MAX_SERVICES},
                )
            self._commit_specs(self._ordered_specs() + [spec])
            self._register_internal(spec)
            self.service_added.emit(spec.service_id)
            self.service_changed.emit(spec.service_id)
            return
        if existing.running or self._registry.has_work(spec.service_id):
            raise SpectaError(
                ErrorCode.BUSY,
                "服务正在运行或有操作排队，不能修改配置",
                {"service_id": spec.service_id},
            )
        candidate = [
            spec if sid == spec.service_id else self._services[sid].spec
            for sid in self._order
            if sid in self._services
        ]
        self._commit_specs(candidate)
        existing.apply_spec(spec)
        self.service_changed.emit(spec.service_id)

    def delete_service(self, service_id: str, source: str = "ui") -> None:
        service = self.get(service_id)
        if source != "ui":
            raise SpectaError(
                ErrorCode.CONFIG_READ_ONLY, "只有界面可以删除服务", {"service_id": service_id}
            )
        if service.running or self._registry.has_work(service_id):
            raise SpectaError(
                ErrorCode.BUSY, "请先停止服务再删除", {"service_id": service_id}
            )
        candidate = [
            self._services[sid].spec
            for sid in self._order
            if sid != service_id and sid in self._services
        ]
        self._commit_specs(candidate)
        self._services.pop(service_id, None)
        if service_id in self._order:
            self._order.remove(service_id)
        self._registry.drop_service(service_id)
        self.service_removed.emit(service_id)

    def _ordered_specs(self) -> List[ServiceSpec]:
        return [self._services[sid].spec for sid in self._order if sid in self._services]

    def _commit_specs(self, specs: List[ServiceSpec]) -> None:
        self._repository.save(specs)

    def _persist(self) -> None:
        self._commit_specs(self._ordered_specs())

    # ------------------------------------------------------------------
    # 启动 / 停止 / 重启
    # ------------------------------------------------------------------
    def submit_start(
        self,
        service_id: Optional[str],
        spec: Optional[ServiceSpec],
        *,
        source: str,
        session_id: Optional[str] = None,
        request_key: Optional[str] = None,
        update_existing: bool = False,
    ) -> Operation:
        fingerprint = _fingerprint(
            {
                "action": ACTION_START,
                "service_id": service_id,
                "spec": spec.to_dict() if spec else None,
                "update_existing": bool(update_existing),
            }
        )
        operation, fresh = self._registry.create_or_get(
            service_id=service_id or (spec.service_id if spec else ""),
            action=ACTION_START,
            source=source,
            session_id=session_id,
            request_key=request_key,
            fingerprint=fingerprint,
        )
        if not fresh:
            # 命中去重缓存：直接返回既有操作，不重复校验与入队
            return operation

        try:
            target = self._resolve_start_target(
                service_id, spec, source, update_existing, operation.operation_id
            )
        except SpectaError as exc:
            # 校验失败：complete() 会把操作从等待队列摘除，绝不会被后续 Stop 触发执行
            self._registry.fail(operation, exc.code, exc.message, exc.details)
            self.operation_changed.emit(operation.operation_id)
            raise

        return self._enqueue_and_run(operation, target.service_id)

    def _resolve_start_target(
        self,
        service_id: Optional[str],
        spec: Optional[ServiceSpec],
        source: str,
        update_existing: bool,
        exclude_operation_id: Optional[str] = None,
    ) -> ServiceSpec:
        if service_id:
            ensure_service_id(service_id)
        if spec is not None and service_id and spec.service_id != service_id:
            raise SpectaError(
                ErrorCode.INVALID_ARGUMENT, "spec.service_id 与 service_id 不一致"
            )
        existing = self._services.get(service_id) if service_id else None

        if existing is None:
            if spec is None:
                raise SpectaError(
                    ErrorCode.NOT_FOUND, f"服务不存在: {service_id}", {"service_id": service_id}
                )
            if len(self._services) >= MAX_SERVICES:
                raise SpectaError(
                    ErrorCode.LIMIT_EXCEEDED, f"最多注册 {MAX_SERVICES} 个服务", {"limit": MAX_SERVICES}
                )
            created = spec.with_origin(ORIGIN_USER if source == "ui" else ORIGIN_AGENT)
            # 先落盘再注册：保存失败时内存里不会多出一个"看得见但没存下"的服务
            self._commit_specs(self._ordered_specs() + [created])
            self._register_internal(created)
            self.service_added.emit(created.service_id)
            return created

        # 已存在
        if spec is None or spec.equivalent_to(existing.spec):
            return existing.spec

        # 配置不同。注意排除当前这次 admission 操作自身，否则它刚占的队列位会把
        # update_existing 更新误判为"服务忙碌/配置冲突"。
        if existing.running or self._registry.has_work(
            existing.service_id, exclude=exclude_operation_id
        ):
            raise SpectaError(
                ErrorCode.CONFIG_CONFLICT,
                "服务正在运行或忙碌，不能更新配置",
                {"service_id": existing.service_id},
            )
        if existing.spec.origin == ORIGIN_USER and source != "ui":
            raise SpectaError(
                ErrorCode.CONFIG_READ_ONLY,
                "Agent 不能修改用户创建的服务配置，请在界面中编辑",
                {"service_id": existing.service_id},
            )
        if not update_existing:
            raise SpectaError(
                ErrorCode.CONFIG_CONFLICT,
                "已存在同 ID 的不同配置；如需更新请显式传 update_existing=true",
                {"service_id": existing.service_id},
            )
        updated = spec.with_origin(existing.spec.origin)
        candidate = [
            updated if sid == existing.service_id else self._services[sid].spec
            for sid in self._order
            if sid in self._services
        ]
        self._commit_specs(candidate)
        existing.apply_spec(updated)
        self.service_changed.emit(existing.service_id)
        return updated

    def submit_stop(
        self,
        service_id: str,
        *,
        source: str,
        session_id: Optional[str] = None,
        request_key: Optional[str] = None,
    ) -> Operation:
        service = self.get(service_id)
        operation, fresh = self._registry.create_or_get(
            service_id=service_id,
            action=ACTION_STOP,
            source=source,
            session_id=session_id,
            request_key=request_key,
            fingerprint=_fingerprint(
                {"service_id": service_id, "action": ACTION_STOP}
            ),
        )
        if not fresh:
            return operation
        return self._enqueue_and_run(operation, service.service_id)

    def submit_restart(
        self,
        service_id: str,
        *,
        source: str,
        session_id: Optional[str] = None,
        request_key: Optional[str] = None,
    ) -> Operation:
        self.get(service_id)
        operation, fresh = self._registry.create_or_get(
            service_id=service_id,
            action=ACTION_RESTART,
            source=source,
            session_id=session_id,
            request_key=request_key,
            fingerprint=_fingerprint(
                {"service_id": service_id, "action": ACTION_RESTART}
            ),
        )
        if not fresh:
            return operation
        return self._enqueue_and_run(operation, service_id)

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------
    def _enqueue_and_run(self, operation: Operation, service_id: str) -> Operation:
        self.operation_changed.emit(operation.operation_id)
        self._dispatch(service_id)
        return operation

    def _dispatch(self, service_id: str) -> None:
        operation = self._registry.next_queued(service_id)
        if operation is None:
            return
        service = self._services.get(service_id)
        if service is None:
            self._registry.fail(operation, ErrorCode.NOT_FOUND, "服务已被删除", {})
            self.operation_changed.emit(operation.operation_id)
            return
        self.operation_changed.emit(operation.operation_id)
        try:
            if operation.action == ACTION_START:
                self._do_start(service, operation)
            elif operation.action == ACTION_STOP:
                service.begin_stop(operation, restart_after=False)
            elif operation.action == ACTION_RESTART:
                if service.running:
                    service.begin_stop(operation, restart_after=True)
                else:
                    self._do_start(service, operation)
        except SpectaError as exc:
            self._registry.complete(
                operation,
                status=OP_FAILED,
                error=exc.to_dict(),
            )
            self.operation_changed.emit(operation.operation_id)
            self.error_occurred.emit(service_id, exc.to_dict())
            self._dispatch(service_id)

    def _do_start(self, service: ManagedService, operation: Operation) -> None:
        # 幂等优先：配置相同且已在运行 → 直接成功，不进入端口冲突 / 运行上限判断。
        # 否则声明了自己监听端口的服务，重复 Start 会被它自己占用的端口拒绝。
        if service.running:
            self._registry.complete(
                operation,
                status=OP_SUCCEEDED,
                run_id=service.facts.run_id,
                result={
                    "already_running": True,
                    "run_id": service.facts.run_id,
                    "pid": service.facts.pid,
                },
            )
            self.operation_changed.emit(operation.operation_id)
            self._dispatch(service.service_id)
            return
        conflict = self._preflight_ports(service.spec)
        if conflict is not None:
            port, pids = conflict
            self._registry.fail(
                operation,
                ErrorCode.PORT_IN_USE,
                f"端口 {port} 已被占用",
                {"port": port, "owner_pids": pids},
            )
            self.operation_changed.emit(operation.operation_id)
            self.error_occurred.emit(
                service.service_id,
                {
                    "code": ErrorCode.PORT_IN_USE,
                    "message": f"端口 {port} 已被占用",
                    "details": {"port": port, "owner_pids": pids},
                },
            )
            self._dispatch(service.service_id)
            return
        running_count = sum(1 for s in self._services.values() if s.running)
        if running_count >= MAX_RUNNING_SERVICES:
            self._registry.fail(
                operation,
                ErrorCode.LIMIT_EXCEEDED,
                f"同时运行的服务不能超过 {MAX_RUNNING_SERVICES}",
                {"limit": MAX_RUNNING_SERVICES},
            )
            self.operation_changed.emit(operation.operation_id)
            self._dispatch(service.service_id)
            return
        service.begin_start(operation, self._default_columns())

    def _preflight_ports(self, spec: ServiceSpec) -> Optional[Tuple[int, List[int]]]:
        for port in spec.ports:
            try:
                ok, pids = port_probe.check_available(port)
            except SpectaError as exc:
                raise
            if not ok:
                return port, pids
        return None

    def _on_operation_done(self, service_id: str, operation_id: str, ok: bool, payload: dict) -> None:
        operation = self._registry.get(operation_id)
        if operation is None:
            self._dispatch(service_id)
            return
        if ok:
            self._registry.complete(
                operation, status=OP_SUCCEEDED, run_id=payload.get("run_id"), result=payload
            )
        else:
            error = payload.get("error") or {}
            self._registry.complete(
                operation,
                status=OP_FAILED,
                error=error,
            )
            self.error_occurred.emit(service_id, error)
        self.operation_changed.emit(operation_id)
        self._dispatch(service_id)

    def _on_stop_done_for_restart(self, service_id: str, operation_id: str) -> None:
        operation = self._registry.get(operation_id)
        service = self._services.get(service_id)
        if operation is None or service is None:
            return
        ports = list(service.spec.ports)
        if not ports:
            self._do_start(service, operation)
            return
        self._pending_restart[service_id] = operation_id
        self._start_release_watch(service_id, operation_id, ports)

    def _start_release_watch(self, service_id: str, operation_id: str, ports: Sequence[int]) -> None:
        if self._watch_active.get(service_id):
            return
        self._watch_active[service_id] = True

        def worker() -> None:
            deadline = time.monotonic() + PORT_RELEASE_TIMEOUT_S
            remaining: List[Tuple[int, List[int]]] = []
            while True:
                remaining = []
                for port in ports:
                    try:
                        ok, pids = port_probe.check_available(port)
                    except SpectaError:
                        ok, pids = False, []
                    if not ok:
                        remaining.append((port, pids))
                if not remaining or time.monotonic() >= deadline:
                    break
                time.sleep(PORT_RELEASE_POLL_S)
            self._bridge.release_checked.emit(service_id, operation_id, remaining)

        threading.Thread(target=worker, name="specta-port-release", daemon=True).start()

    def _on_release_checked(self, service_id: str, operation_id: str, remaining: object) -> None:
        self._watch_active[service_id] = False
        operation = self._registry.get(operation_id)
        service = self._services.get(service_id)
        if operation is None or service is None:
            return
        conflicts = list(remaining or [])
        if conflicts:
            port, pids = conflicts[0]
            error_operation = operation
            self._registry.complete(
                error_operation,
                status=OP_FAILED,
                error={
                    "code": ErrorCode.PORT_RELEASE_TIMEOUT,
                    "message": f"端口 {port} 未在 {PORT_RELEASE_TIMEOUT_S:.0f} 秒内释放",
                    "details": {"port": port, "owner_pids": pids},
                },
            )
            self.operation_changed.emit(operation_id)
            self.error_occurred.emit(
                service_id,
                {
                    "code": ErrorCode.PORT_RELEASE_TIMEOUT,
                    "message": f"端口 {port} 未释放，已取消重启",
                    "details": {"port": port, "owner_pids": pids},
                },
            )
            self._dispatch(service_id)
            return
        self._do_start(service, operation)

    # ------------------------------------------------------------------
    # Agent 判断
    # ------------------------------------------------------------------
    def report_service_state(
        self,
        service_id: str,
        run_id: str,
        state: str,
        reason: str,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        service = self.get(service_id)
        if state not in (ASSESS_READY, ASSESS_ERROR, ASSESS_UNKNOWN):
            raise SpectaError(
                ErrorCode.INVALID_ARGUMENT, "state 只能是 ready/error/unknown", {"state": state}
            )
        if not isinstance(reason, str) or not reason.strip():
            raise SpectaError(ErrorCode.INVALID_ARGUMENT, "reason 不能为空")
        if len(reason) > 2000:
            raise SpectaError(ErrorCode.INVALID_ARGUMENT, "reason 最多 2000 字")
        if not run_id or service.facts.run_id != run_id:
            raise SpectaError(
                ErrorCode.STALE_RUN,
                "上报的 run_id 不是当前运行",
                {"expected": service.facts.run_id, "got": run_id},
            )
        if service.facts.lifecycle not in (LIFECYCLE_RUNNING, LIFECYCLE_STARTING):
            raise SpectaError(
                ErrorCode.STALE_RUN,
                "服务当前没有在运行，无法上报判断",
                {"lifecycle": service.facts.lifecycle},
            )
        service.report_assessment(run_id, state, reason, session_id)
        return service.snapshot(
            active_operation=self._registry.active(service_id),
            last_operation=self._registry.last_completed(service_id),
            queued=self._registry.queued_count(service_id),
        )

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def get_recent_logs(
        self,
        service_id: str,
        *,
        lines: int = 100,
        filter_error: bool = False,
        run_id: Optional[str] = None,
        after_sequence: Optional[int] = None,
    ) -> Dict[str, Any]:
        service = self.get(service_id)
        snap = service.log_snapshot()
        records = snap.records

        if run_id and run_id != "all":
            filtered = [r for r in records if r.run_id == run_id]
            if not filtered and run_id != snap.current_run_id:
                raise SpectaError(
                    ErrorCode.NOT_FOUND,
                    f"指定运行的日志不存在或已被淘汰: {run_id}",
                    {"service_id": service_id, "run_id": run_id, "available": sorted({r.run_id for r in records if r.run_id})},
                )
            records = filtered
        elif run_id is None:
            current = snap.current_run_id
            if current:
                records = [r for r in records if r.run_id == current]

        context_lines = self._context_lines_provider() if self._context_lines_provider else 8
        result = log_query.select_recent(
            records,
            lines=lines,
            filter_error=filter_error,
            context_lines=context_lines,
            after_sequence=after_sequence,
        )
        payload: Dict[str, Any] = {
            "service_id": service_id,
            "run_id": run_id or snap.current_run_id,
            "epoch": snap.epoch,
            "revision": snap.revision,
            "records": [r.meta_dict() for r in result["records"]],
            "history_evicted": snap.history_evicted,
            "capture_loss": snap.capture_loss,
            "truncated": result["truncated"],
            "block_truncated": result["block_truncated"],
            "returned_lines": result["returned_lines"],
            "replay_complete": snap.replay_complete,
            "error_match_count": snap.error_match_count,
        }
        if run_id == "all":
            payload["run_id"] = "all"
        return payload

    def clear_logs(self, service_id: str) -> None:
        service = self.get(service_id)
        service.pipeline.clear(to_rfc3339(utc_now()) or "")
        snap = service.pipeline.snapshot()
        self.logs_changed.emit(service_id, snap.epoch, snap.revision)

    def resize_logs(self, columns: int) -> None:
        for service in self._services.values():
            if service.pipeline.resize(columns):
                snap = service.pipeline.snapshot()
                self.logs_changed.emit(service.service_id, snap.epoch, snap.revision)

    # ------------------------------------------------------------------
    # 端口探测线程
    # ------------------------------------------------------------------
    def _probe_loop(self) -> None:
        while not self._probe_stop.is_set():
            for service_id in list(self._order):
                service = self._services.get(service_id)
                if service is None:
                    continue
                ports = list(service.spec.ports)
                if not ports:
                    continue
                if not service.running:
                    if service.facts.port_observations:
                        self._bridge.probed.emit(service_id, [])
                    continue
                pids = [p for p in (service.facts.pid, service.facts.launcher_pid) if p]
                try:
                    observations = port_probe.snapshot(ports, pids)
                except SpectaError:
                    continue
                self._bridge.probed.emit(service_id, observations)
            if self._probe_stop.wait(PROBE_INTERVAL_S):
                break

    def _on_probed(self, service_id: str, observations: object) -> None:
        service = self._services.get(service_id)
        if service is None:
            return
        service.update_port_observations(list(observations or []))

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _default_columns(self) -> int:
        return 120

    def running_count(self) -> int:
        return sum(1 for s in self._services.values() if s.running)


def _fingerprint(payload: Dict[str, Any]) -> str:
    import hashlib
    import json

    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
