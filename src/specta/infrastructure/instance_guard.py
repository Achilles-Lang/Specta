"""单实例守卫。

动态端口绑定成功不代表只有一个 Daemon —— 互斥、端口与 token 的发布必须分开：
只有取得互斥权的进程才可以发布 daemon.json 并启动完整应用；失去互斥权的候选
进程直接退出，由桥重新发现已运行的实例。

使用 QLockFile：stale time 设为 0（长驻应用不能因为超过默认阈值就被当作过期），
互斥权持有至进程退出。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from PySide6.QtCore import QLockFile

from .settings import daemon_lock_path


class SingleInstanceGuard:
    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or daemon_lock_path()
        self._lock = QLockFile(str(self._path))
        self._lock.setStaleLockTime(0)
        self._held = False

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> bool:
        if self._held:
            return True
        self._held = bool(self._lock.tryLock(0))
        return self._held

    def holder_info(self) -> Tuple[Optional[int], Optional[str]]:
        """返回当前锁持有者的 (pid, 应用名)。"""
        info = self._lock.getLockInfo()
        if not info:
            return None, None
        pid = getattr(info, "pid", None) if not isinstance(info, tuple) else info[0]
        appname = (
            getattr(info, "appname", None) if not isinstance(info, tuple) else info[2]
        )
        return pid, appname

    def release(self) -> None:
        if self._held:
            try:
                self._lock.unlock()
            finally:
                self._held = False

    @property
    def held(self) -> bool:
        return self._held
