"""服务配置持久化。

只保存 ServiceSpec；不保存 PID / Job 句柄 / 运行状态。ephemeral 服务不落盘。
坏配置不静默覆盖：保留原文件，报告可读条目与非法条目。

只读保护是**真的**：`load()` 发现更高 schema 版本后，Repository 进入只读状态，
之后任何 `save()` 都会被拒绝 —— 而不是只报告一次 `future_schema` 却在下次写回时
把未来格式整个覆盖成当前版本。对损坏/不完整配置的恢复写回，也会先把原文件另存备份。
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..domain.errors import ErrorCode, SpectaError
from ..domain.models import (
    MAX_SERVICES,
    ServiceSpec,
    ORIGIN_AGENT,
    ORIGIN_USER,
)
from .settings import atomic_write_json, read_json, services_json_path

SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_MAX = 1


@dataclass
class LoadResult:
    specs: List[ServiceSpec] = field(default_factory=list)
    issues: List[Dict[str, Any]] = field(default_factory=list)
    needs_recovery: bool = False
    future_schema: bool = False


class ServiceRepository:
    """加载 / 校验 / 原子保存 services.json。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or services_json_path()
        # 加载后确定的写入策略：更高 schema 版本 → 只读；损坏配置 → 恢复写回前先备份
        self._read_only = False
        self._recovery_backup_needed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def read_only(self) -> bool:
        return self._read_only

    # -- 读取 --------------------------------------------------------------
    def load(self) -> LoadResult:
        result = LoadResult()
        self._read_only = False
        self._recovery_backup_needed = False
        try:
            raw = read_json(self._path)
        except json.JSONDecodeError as exc:
            result.needs_recovery = True
            self._recovery_backup_needed = True
            result.issues.append(
                {"kind": "corrupt_file", "message": f"services.json 解析失败: {exc}"}
            )
            return result
        except OSError as exc:
            result.needs_recovery = True
            result.issues.append(
                {"kind": "read_failed", "message": f"读取 services.json 失败: {exc}"}
            )
            return result

        if raw is None:
            return result
        if not isinstance(raw, dict):
            result.needs_recovery = True
            self._recovery_backup_needed = True
            result.issues.append({"kind": "bad_root", "message": "services.json 根节点不是对象"})
            return result

        schema = raw.get("schema_version", 1)
        try:
            schema_int = int(schema)
        except (TypeError, ValueError):
            schema_int = 1
        if schema_int > SUPPORTED_SCHEMA_MAX:
            result.future_schema = True
            result.needs_recovery = True
            # 真正进入只读：防止后续任何 save 把未来格式覆盖为当前版本
            self._read_only = True
            result.issues.append(
                {
                    "kind": "future_schema",
                    "message": f"配置版本 {schema_int} 高于当前支持的 {SUPPORTED_SCHEMA_MAX}，只读保护",
                }
            )
            return result

        entries = raw.get("services", [])
        if not isinstance(entries, list):
            result.needs_recovery = True
            self._recovery_backup_needed = True
            result.issues.append({"kind": "bad_services", "message": "services 字段不是数组"})
            return result

        seen: Dict[str, ServiceSpec] = {}
        for index, entry in enumerate(entries):
            try:
                spec = ServiceSpec.from_dict(entry)
            except SpectaError as exc:
                result.needs_recovery = True
                self._recovery_backup_needed = True
                result.issues.append(
                    {
                        "kind": "invalid_entry",
                        "index": index,
                        "message": exc.message,
                        "code": exc.code,
                    }
                )
                continue
            if spec.service_id in seen:
                result.needs_recovery = True
                self._recovery_backup_needed = True
                result.issues.append(
                    {
                        "kind": "duplicate_id",
                        "index": index,
                        "service_id": spec.service_id,
                        "message": "重复的 service_id，已忽略后一条",
                    }
                )
                continue
            seen[spec.service_id] = spec

        result.specs = list(seen.values())
        return result

    # -- 写入 --------------------------------------------------------------
    def save(self, specs: List[ServiceSpec]) -> None:
        if self._read_only:
            raise SpectaError(
                ErrorCode.CONFIG_READ_ONLY,
                "配置来自更高版本（只读保护），拒绝覆盖；请升级 Specta 或先备份后手动处理",
                {"path": str(self._path)},
            )
        persisted = [s for s in specs if not s.ephemeral]
        if len(persisted) > MAX_SERVICES:
            raise SpectaError(
                ErrorCode.LIMIT_EXCEEDED,
                f"最多注册 {MAX_SERVICES} 个服务（不含 ephemeral）",
                {"limit": MAX_SERVICES, "count": len(persisted)},
            )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "services": [s.to_dict() for s in persisted],
        }
        self._backup_before_recovery_write()
        atomic_write_json(self._path, payload)
        self._recovery_backup_needed = False

    def _backup_before_recovery_write(self) -> None:
        """损坏/不完整配置的首次恢复写回前，把原文件另存一份，避免直接覆盖丢失证据。"""
        if not self._recovery_backup_needed:
            return
        try:
            if not self._path.exists():
                return
            backup = self._path.with_name(
                f"{self._path.name}.recovery-{int(time.time())}.bak"
            )
            shutil.copyfile(self._path, backup)
        except OSError:
            # 备份失败不应阻止用户修复配置，但要留下痕迹（由诊断层轮转目录承载）
            pass
