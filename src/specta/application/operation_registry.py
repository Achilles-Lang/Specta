"""操作注册表：全局 operation_id、按服务 FIFO、去重与结果缓存。

同一服务的生命周期操作串行；不同服务可并行。已完成结果保留 15 分钟或 1000 条，
先到限额先淘汰；活动操作永不被淘汰。每服务最多 8 个等待中的操作，超过返回 BUSY。

关键不变量：**进入终态的操作必须离开等待队列**。否则一个已失败的启动会留在队列里，
被后续 `next_queued()` 当成待执行操作重新置为 running —— 用户随后提交的 Stop 会先
触发那个已被拒绝的启动真正运行命令。因此 `complete()` 负责把操作从队列摘除，
`next_queued()` 也只在操作确实处于 queued 状态时才派发。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from ..domain.errors import ErrorCode, SpectaError
from ..domain.models import (
    OP_FAILED,
    OP_QUEUED,
    OP_RUNNING,
    OP_SUCCEEDED,
    Operation,
    new_operation_id,
    to_rfc3339,
    utc_now,
)

MAX_QUEUED_PER_SERVICE = 8
RETAIN_SECONDS = 15 * 60
RETAIN_MAX = 1000


class OperationRegistry:
    def __init__(self) -> None:
        self._operations: Dict[str, Operation] = {}
        self._completed_order: Deque[str] = deque()
        self._completed_at: Dict[str, float] = {}
        self._queued: Dict[str, Deque[str]] = {}
        self._active: Dict[str, Optional[str]] = {}
        self._last_completed: Dict[str, str] = {}
        # (session_id, request_key) -> (operation_id, fingerprint)
        self._dedupe: Dict[Tuple[str, str], Tuple[str, str]] = {}

    # -- 创建 --------------------------------------------------------------
    def create(
        self,
        *,
        service_id: str,
        action: str,
        source: str,
        session_id: Optional[str] = None,
        request_key: Optional[str] = None,
        fingerprint: str = "",
    ) -> Operation:
        operation, _fresh = self.create_or_get(
            service_id=service_id,
            action=action,
            source=source,
            session_id=session_id,
            request_key=request_key,
            fingerprint=fingerprint,
        )
        return operation

    def create_or_get(
        self,
        *,
        service_id: str,
        action: str,
        source: str,
        session_id: Optional[str] = None,
        request_key: Optional[str] = None,
        fingerprint: str = "",
    ) -> Tuple[Operation, bool]:
        """返回 (操作, 是否新建)。

        命中去重缓存时返回已存在的操作并令 `fresh=False`，调用方据此跳过业务校验与
        再次入队，避免同一个 request_key 被执行两次。
        """
        self.prune()
        if request_key:
            existing = self._dedupe.get((session_id or "", request_key))
            if existing is not None:
                operation_id, stored_fp = existing
                if stored_fp != fingerprint:
                    raise SpectaError(
                        ErrorCode.REQUEST_KEY_CONFLICT,
                        "相同的 request_key 被用于不同的参数",
                        {"request_key": request_key, "operation_id": operation_id},
                    )
                operation = self._operations.get(operation_id)
                if operation is not None:
                    return operation, False

        queue = self._queued.setdefault(service_id, deque())
        if len(queue) >= MAX_QUEUED_PER_SERVICE:
            raise SpectaError(
                ErrorCode.BUSY,
                f"服务 {service_id} 的等待操作已达上限 {MAX_QUEUED_PER_SERVICE}",
                {"service_id": service_id, "queued": len(queue)},
            )

        operation = Operation(
            operation_id=new_operation_id(),
            service_id=service_id,
            action=action,
            source=source,
            session_id=session_id,
            request_key=request_key,
            status=OP_QUEUED,
        )
        self._operations[operation.operation_id] = operation
        if request_key:
            self._dedupe[(session_id or "", request_key)] = (operation.operation_id, fingerprint)
        queue.append(operation.operation_id)
        return operation, True

    # -- 调度 --------------------------------------------------------------
    def next_queued(self, service_id: str) -> Optional[Operation]:
        """取出下一个可执行操作；若已有活动操作则返回 None。

        跳过任何已不处于 queued 状态的操作（被取消、已失败或已完成），它们是
        「离开队列但不该被执行」的残留，重新派发会导致用户未授权的命令被执行。
        """
        if self._active.get(service_id):
            return None
        queue = self._queued.get(service_id)
        if not queue:
            return None
        while queue:
            operation_id = queue.popleft()
            operation = self._operations.get(operation_id)
            if operation is None:
                continue
            if operation.status != OP_QUEUED:
                continue
            operation.status = OP_RUNNING
            self._active[service_id] = operation_id
            return operation
        return None

    def mark_running(self, operation: Operation) -> None:
        operation.status = OP_RUNNING
        self._active[operation.service_id] = operation.operation_id

    def complete(
        self,
        operation: Operation,
        *,
        status: str,
        run_id: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> None:
        operation.status = status
        operation.completed_at = to_rfc3339(utc_now())
        if run_id is not None:
            operation.run_id = run_id
        operation.result = result
        operation.error = error
        # 终态操作必须离开等待队列，否则会被后续 next_queued 重新派发
        self._remove_from_queue(operation.service_id, operation.operation_id)
        if self._active.get(operation.service_id) == operation.operation_id:
            self._active[operation.service_id] = None
        self._completed_order.append(operation.operation_id)
        self._completed_at[operation.operation_id] = time.monotonic()
        self._last_completed[operation.service_id] = operation.operation_id
        self.prune()

    def succeed(self, operation: Operation, result: Optional[Dict[str, Any]] = None, run_id: Optional[str] = None) -> None:
        self.complete(operation, status=OP_SUCCEEDED, run_id=run_id, result=result)

    def fail(self, operation: Operation, code: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        self.complete(
            operation,
            status=OP_FAILED,
            error={"code": code, "message": message, "details": details or {}},
        )

    def _remove_from_queue(self, service_id: str, operation_id: str) -> None:
        queue = self._queued.get(service_id)
        if not queue:
            return
        try:
            queue.remove(operation_id)
        except ValueError:
            pass

    # -- 查询 --------------------------------------------------------------
    def get(self, operation_id: str) -> Optional[Operation]:
        return self._operations.get(operation_id)

    def active(self, service_id: str) -> Optional[Operation]:
        operation_id = self._active.get(service_id)
        if not operation_id:
            return None
        return self._operations.get(operation_id)

    def last_completed(self, service_id: str) -> Optional[Operation]:
        operation_id = self._last_completed.get(service_id)
        if not operation_id:
            return None
        return self._operations.get(operation_id)

    def queued_count(self, service_id: str) -> int:
        return len(self._queued.get(service_id, ()))

    def has_work(self, service_id: str, *, exclude: Optional[str] = None) -> bool:
        """是否有活动或排队中的操作。

        `exclude` 用于「当前正在做 admission 校验的那个操作」：它此刻已经占据队列
        位置，但不应把自己算成"服务忙碌"，否则更新配置会被自己的操作判定为冲突。
        """
        active = self._active.get(service_id)
        if active and active != exclude:
            return True
        queue = self._queued.get(service_id)
        if queue:
            for operation_id in queue:
                if operation_id != exclude:
                    return True
        return False

    def drop_service(self, service_id: str) -> None:
        self._queued.pop(service_id, None)
        self._active.pop(service_id, None)
        self._last_completed.pop(service_id, None)

    # -- 清理 --------------------------------------------------------------
    def prune(self) -> None:
        now = time.monotonic()
        while self._completed_order:
            operation_id = self._completed_order[0]
            completed_at = self._completed_at.get(operation_id, now)
            too_old = (now - completed_at) > RETAIN_SECONDS
            too_many = len(self._completed_order) > RETAIN_MAX
            if not too_old and not too_many:
                break
            self._completed_order.popleft()
            self._completed_at.pop(operation_id, None)
            self._operations.pop(operation_id, None)
