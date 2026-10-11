"""Windows Job Object 封装。

每次服务运行创建一个 Job（`KILL_ON_JOB_CLOSE`，禁止 breakaway），把受控启动器
加入 Job，启动器再创建的整棵进程树都会继承该 Job 归属。Daemon 退出或崩溃时
Windows 会连带清理 Job 内全部进程，从根上消灭"孤儿进程占端口"。

平台说明：非 Windows 上创建 Job 会失败，调用方应据此降级（用 taskkill 兜底）。
本模块所有 ctypes 调用只写内核 API，避免额外的 C 扩展依赖。

已知限制（规格 16-R1）：若 Daemon 自身运行在一个不允许 breakaway 的 Job 内
（例如被某些 IDE/宿主进程托管），AssignProcessToJobObject 会返回失败。此时
Daemon 返回 JOB_ASSIGN_FAILED；若设置环境变量 SPECTA_ALLOW_NO_JOB=1，可退化为
"仅靠 taskkill /T /F 兜底"的开发模式（生产不建议）。
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import List, Optional

IS_WINDOWS = sys.platform == "win32"

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000

JobObjectBasicProcessIdList = 3
JobObjectExtendedLimitInformation = 9

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_INFORMATION = 0x0400

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class JobError(Exception):
    def __init__(self, message: str, winerror: Optional[int] = None) -> None:
        super().__init__(message)
        self.winerror = winerror


if IS_WINDOWS:  # pragma: no cover - 平台相关
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", ctypes.c_uint32),
            ("NumberOfProcessIdsInList", ctypes.c_uint32),
            ("ProcessIdList", ctypes.c_size_t * 1),
        ]


def allow_no_job() -> bool:
    return os.environ.get("SPECTA_ALLOW_NO_JOB", "").strip() in ("1", "true", "yes")


class WindowsJob:
    """一个服务运行对应一个 Job。"""

    def __init__(self, handle: Optional[int] = None, degraded: bool = False) -> None:
        self._handle = handle
        self._closed = False
        self._degraded = degraded
        self._pids: List[int] = []

    # -- 生命周期 ----------------------------------------------------------
    @classmethod
    def create(cls) -> "WindowsJob":
        if not IS_WINDOWS:
            return cls(handle=None, degraded=True)
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise JobError("CreateJobObject 失败", ctypes.get_last_error())
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            ctypes.c_void_p(handle),
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            err = ctypes.get_last_error()
            _kernel32.CloseHandle(ctypes.c_void_p(handle))
            raise JobError("SetInformationJobObject 失败", err)
        return cls(handle=handle)

    @property
    def degraded(self) -> bool:
        return self._degraded or self._handle is None

    @property
    def valid(self) -> bool:
        return IS_WINDOWS and self._handle is not None and not self._closed

    # -- 归属 --------------------------------------------------------------
    def assign_pid(self, pid: int) -> bool:
        """把进程加入 Job。失败返回 False（调用方据此决定是否放行）。"""
        if self.degraded or self._handle is None:
            self._pids.append(pid)
            return True
        hproc = _kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_INFORMATION,
            False,
            int(pid),
        )
        if not hproc:
            return False
        try:
            ok = _kernel32.AssignProcessToJobObject(ctypes.c_void_p(self._handle), hproc)
        finally:
            _kernel32.CloseHandle(hproc)
        if not ok:
            return False
        self._pids.append(pid)
        return True

    def list_pids(self) -> List[int]:
        """返回 Job 内当前仍存活的进程 PID 列表。"""
        if self.degraded or self._handle is None:
            return list(self._pids)
        # 先探知需要的缓冲大小
        size = ctypes.sizeof(_JOBOBJECT_BASIC_PROCESS_ID_LIST) + 8 * 1024
        buf = ctypes.create_string_buffer(size)
        ret_len = ctypes.c_uint32(0)
        ok = _kernel32.QueryInformationJobObject(
            ctypes.c_void_p(self._handle),
            JobObjectBasicProcessIdList,
            buf,
            size,
            ctypes.byref(ret_len),
        )
        if not ok:
            return []
        header = ctypes.cast(buf, ctypes.POINTER(_JOBOBJECT_BASIC_PROCESS_ID_LIST)).contents
        count = header.NumberOfProcessIdsInList
        if count == 0:
            return []
        pid_array = (ctypes.c_size_t * count).from_buffer(buf, 8)
        return [int(pid_array[i]) for i in range(count)]

    def is_empty(self) -> bool:
        return len(self.list_pids()) == 0

    # -- 终止 --------------------------------------------------------------
    def terminate(self, exit_code: int = 1) -> bool:
        if self.degraded or self._handle is None:
            return False
        ok = bool(_kernel32.TerminateJobObject(ctypes.c_void_p(self._handle), exit_code))
        return ok

    def close(self) -> None:
        """关闭句柄。KILL_ON_JOB_CLOSE 会在此刻清理 Job 内剩余进程。"""
        if self._closed:
            return
        self._closed = True
        if self._handle is not None:
            try:
                _kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            except Exception:  # noqa: BLE001
                pass
            self._handle = None

    def __del__(self):  # pragma: no cover - 兜底
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
