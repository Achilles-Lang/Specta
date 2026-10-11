"""受控启动器。

由 Daemon 以 `python -m specta.launch_worker` 启动，本身不加载 Qt、不加载 MCP、
不解析 ANSI、不决定状态。它的唯一职责是把"目标进程的创建时刻"推迟到 Daemon 已经
把启动器加入 Windows Job 之后，从而消除"QProcess.start 之后再 Assign 时目标已经
创建后代"的竞态窗口。

协议（stdin/stdout，每行一个 JSON，见 domain/protocol.py）：
  stdin  <-  {"type":"launch", "run_id":..., "mode":"shell|exec", ...}
  stdout ->  child_started / output / root_exited / child_exit / launch_error

约定：
  * 在收到 launch 指令前绝不创建目标进程；EOF 或 10 秒超时直接退出。
  * stdout 只承载协议帧；自身诊断走 stderr，绝不污染协议流。
  * 目标进程 stdin 为 DEVNULL；合并 stdout/stderr 后按块转发。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

LAUNCH_TIMEOUT_S = 10.0
READ_CHUNK = 32 * 1024

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

_write_lock = threading.Lock()


def _send(message: Dict[str, Any]) -> None:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with _write_lock:
        try:
            sys.stdout.buffer.write(payload + b"\n")
            sys.stdout.buffer.flush()
        except (BrokenPipeError, ValueError):
            pass


def _read_launch_spec(timeout: float) -> Optional[Dict[str, Any]]:
    result: Dict[str, Any] = {}
    done = threading.Event()

    def reader() -> None:
        try:
            line = sys.stdin.buffer.readline()
        except Exception:  # noqa: BLE001
            line = b""
        if line:
            result["line"] = line
        done.set()

    thread = threading.Thread(target=reader, name="launch-stdin", daemon=True)
    thread.start()
    if not done.wait(timeout):
        return None
    line = result.get("line")
    if not line:
        return None
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(message, dict) or message.get("type") != "launch":
        return None
    return message


def _shell_command_line(command: str, comspec: str) -> str:
    """把整条命令装配成交给 cmd 的**命令行字符串**。

    两个必须遵守的 Windows 事实（均由实测探针确认，见 tests/unit/test_launch_worker.py）：

    1. 必须用**字符串**形式交给 CreateProcess（Popen 接受 str），不能走 list 形式。
       list 会经 subprocess.list2cmdline 拼装，而它把元素内嵌的 `"` 转义成 `\\"`，
       cmd 不认这种转义——实测 `echo ARG="v"` 会被拼成 `echo ARG=\\"v\\"`，
       反斜杠原样进入下游参数（正是「引号被吃掉」的观感来源）。

    2. `cmd /d /s /c` 带 `/s` 时执行「老式」引号处理：**去掉首字符（若为引号），
       并删掉命令行里最后一个引号**。因此当命令本身以引号开头时（典型是
       `"C:\\Program Files\\...\\java.exe" args`），必须**再多包一层引号**，
       让 cmd 剥掉的是我们补的那一层：
           "C:\\path with space\\app.exe" -Dk="v v"
         → cmd /d /s /c ""C:\\path with space\\app.exe" -Dk="v v""
         → cmd 剥引号后恰好还原原始命令。

    不以引号开头的命令保持原样：既不破坏 `set X=...&&` 这类写法，也让
    `%VAR%` 展开、管道、括号块的语义与手敲终端完全一致。
    """
    payload = command
    if command.lstrip().startswith('"'):
        payload = f'"{command}"'
    return f"{comspec} /d /s /c {payload}"


def _resolve_launch_args(spec: Dict[str, Any]) -> "tuple[Any, str]":
    """返回 (Popen 的 args, 用于报错的 argv0)。

    shell 模式返回**字符串**命令行（原因见 _shell_command_line）；
    exec 模式返回参数列表，不经 shell。
    """
    mode = spec.get("mode", "shell")
    if mode == "shell":
        command = spec.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("shell 模式缺少 command")
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return _shell_command_line(command, comspec), comspec
    if mode == "exec":
        program = spec.get("program")
        if not isinstance(program, str) or not program.strip():
            raise ValueError("exec 模式缺少 program")
        args = spec.get("args") or []
        if not isinstance(args, list):
            raise ValueError("args 必须是字符串数组")
        return [program] + [str(a) for a in args], program
    raise ValueError(f"未知启动方式: {mode}")


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=False)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    spec = _read_launch_spec(LAUNCH_TIMEOUT_S)
    if spec is None:
        # 超时或 EOF：不创建任何目标进程
        print("launch_worker: 未收到启动指令，退出", file=sys.stderr)
        return 0

    run_id = str(spec.get("run_id", ""))
    try:
        argv, argv0 = _resolve_launch_args(spec)
    except ValueError as exc:
        _send(
            {
                "type": "launch_error",
                "run_id": run_id,
                "error": {"code": "LAUNCH_FAILED", "message": str(exc), "details": {}},
            }
        )
        return 2

    cwd = spec.get("cwd") or None
    env = spec.get("env")
    if not isinstance(env, dict):
        env = None

    popen_kwargs: Dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "creationflags": _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
        "close_fds": True,
    }

    try:
        proc = subprocess.Popen(argv, **popen_kwargs)
    except FileNotFoundError as exc:
        _send(
            {
                "type": "launch_error",
                "run_id": run_id,
                "error": {
                    "code": "LAUNCH_FAILED",
                    "message": f"找不到可执行程序: {exc}",
                    "details": {"argv0": argv0},
                },
            }
        )
        return 2
    except OSError as exc:
        _send(
            {
                "type": "launch_error",
                "run_id": run_id,
                "error": {
                    "code": "LAUNCH_FAILED",
                    "message": f"创建目标进程失败: {exc}",
                    "details": {"winerror": getattr(exc, "winerror", None)},
                },
            }
        )
        return 2

    # 先报告目标已创建（真实 PID），再转入输出转发
    _send({"type": "child_started", "run_id": run_id, "pid": proc.pid})

    output_done = threading.Event()

    def pump_output() -> None:
        stream = proc.stdout
        # read1 只要有数据就返回，避免 read(n) 阻塞到攒满 32KiB 才转发
        read = getattr(stream, "read1", None) or getattr(stream, "read", None)
        try:
            while True:
                chunk = read(READ_CHUNK) if (stream and read) else b""
                if not chunk:
                    break
                _send(
                    {
                        "type": "output",
                        "run_id": run_id,
                        "data": base64.b64encode(chunk).decode("ascii"),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            print(f"launch_worker: 读取目标输出失败: {exc}", file=sys.stderr)
        finally:
            output_done.set()
            _send({"type": "child_exit", "run_id": run_id})

    def observe_exit() -> None:
        exit_code = proc.wait()
        _send({"type": "root_exited", "run_id": run_id, "exit_code": exit_code})

    reader = threading.Thread(target=pump_output, name="launch-out", daemon=True)
    watcher = threading.Thread(target=observe_exit, name="launch-exit", daemon=True)
    reader.start()
    watcher.start()

    # 根进程退出后再给输出收尾一点时间，然后退出（Job 清理由 Daemon 负责）
    watcher.join()
    output_done.wait(timeout=2.0)
    time.sleep(0.05)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
