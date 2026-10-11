"""ManagedService：一个服务的运行实例。

持有该服务的配置、运行事实、Agent 判断、QProcess（托管受控启动器）、每次运行的
Windows Job 以及 LogPipeline。它不知道 MCP、不知道控件；只负责单个服务内部的状态
转换，跨入口的操作协调由 ServiceManager 负责。

启动握手（规格 8.1）：

    create Job → QProcess 起 launch_worker → 启动器加入 Job（失败即 JOB_ASSIGN_FAILED）
    → 向启动器 stdin 下发 launch spec → 启动器创建目标 → child_started(pid) → running
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from ..domain.errors import ErrorCode, SpectaError
from ..domain.models import (
    LIFECYCLE_EXITED,
    LIFECYCLE_FAILED,
    LIFECYCLE_RUNNING,
    LIFECYCLE_STARTING,
    LIFECYCLE_STOPPED,
    LIFECYCLE_STOPPING,
    ACTION_START,
    ACTION_STOP,
    AgentAssessment,
    Operation,
    PortObservation,
    ServiceFacts,
    ServiceSpec,
    build_snapshot,
    new_run_id,
    to_rfc3339,
    utc_now,
)
from ..domain.protocol import (
    FrameDecoder,
    FrameError,
    W_CHILD_EXIT,
    W_CHILD_STARTED,
    W_LAUNCH_ERROR,
    W_OUTPUT,
    W_ROOT_EXITED,
    WORKER_MAX_FRAME,
    decode_worker_output,
)
from ..infrastructure.windows_job import WindowsJob, allow_no_job
from ..logs.pipeline import PARSE_TIME_SLICE_S, LogPipeline

START_TIMEOUT_MS = 15000
STOP_TIMEOUT_MS = 5000
SETTLE_MS = 100
STOP_POLL_MS = 200
LOGS_COALESCE_MS = 50

# 就绪正则：单次匹配超时（规格上限 20ms）与扫描窗口/预算上限。
# 无超时的正则会在 Qt 事件循环里长时间阻塞，连 Stop 都点不动。
READY_REGEX_TIMEOUT_S = 0.02
READY_SCAN_WINDOW = 400
READY_SCAN_BUDGET = 400

_ready_regex_cache: Dict[str, Any] = {}


def _compile_ready_regex(pattern: str):
    """编译并缓存就绪正则；非法模式返回 None（调用方视作不匹配）。"""
    if pattern in _ready_regex_cache:
        return _ready_regex_cache[pattern]
    compiled = None
    try:
        import regex as _regex

        compiled = _regex.compile(pattern)
    except Exception:  # noqa: BLE001
        compiled = None
    _ready_regex_cache[pattern] = compiled
    return compiled


def _worker_src_path() -> Optional[str]:
    """开发态下把 src 目录塞进 PYTHONPATH，保证 `-m specta.launch_worker` 可解析。"""
    try:
        import specta

        src = Path(specta.__file__).resolve().parent.parent
        if (src / "specta" / "launch_worker.py").exists():
            return str(src)
    except Exception:  # noqa: BLE001
        pass
    return None


class ManagedService(QObject):
    facts_changed = Signal(str)
    logs_changed = Signal(str, int, int)
    assessment_changed = Signal(str)
    # service_id, operation_id, ok, payload
    operation_done = Signal(str, str, bool, dict)
    # 停止完成但需要继续重启（由 Manager 继续端口释放观察并再次启动）
    stop_done_for_restart = Signal(str, str)
    # 启动前要求端口预检（Manager 处理）
    needs_port_preflight = Signal(str, str)

    def __init__(self, spec: ServiceSpec, columns: int = 120, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self.pipeline = LogPipeline(spec.service_id, spec.encoding, columns)
        self.facts = ServiceFacts()
        self.assessment: Optional[AgentAssessment] = None
        self.config_revision = 1

        self._process: Optional[QProcess] = None
        self._job: Optional[WindowsJob] = None
        self._decoder = FrameDecoder(WORKER_MAX_FRAME)
        self._current_operation: Optional[Operation] = None
        self._restart_after_stop = False
        self._launch_sent = False
        self._child_started = False
        self._root_exited = False
        self._child_exit_seen = False
        self._stop_requested = False
        self._stop_finished_reported = False
        self._worker_stderr: List[str] = []

        self._start_timer = QTimer(self)
        self._start_timer.setSingleShot(True)
        self._start_timer.timeout.connect(self._on_start_timeout)

        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.timeout.connect(self._finish_exit)

        self._stop_timer = QTimer(self)
        self._stop_timer.setInterval(STOP_POLL_MS)
        self._stop_timer.timeout.connect(self._poll_stop)
        self._stop_deadline = 0.0

        self._logs_timer = QTimer(self)
        self._logs_timer.setSingleShot(True)
        self._logs_timer.setInterval(LOGS_COALESCE_MS)
        self._logs_timer.timeout.connect(self._emit_logs)

        self._ready_scanned_seq = 0

        # 解析推进定时器：feed_output 只跑一个时间片，剩余 pending 必须由事件循环继续
        # 消费，否则突发输出后若不再有新日志，尾部内容会永久滞留在待解析队列里。
        self._parse_timer = QTimer(self)
        self._parse_timer.setSingleShot(True)
        self._parse_timer.setInterval(0)
        self._parse_timer.timeout.connect(self._drain_parse)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def service_id(self) -> str:
        return self.spec.service_id

    @property
    def running(self) -> bool:
        return self.facts.lifecycle in (LIFECYCLE_STARTING, LIFECYCLE_RUNNING, LIFECYCLE_STOPPING)

    def apply_spec(self, spec: ServiceSpec) -> None:
        self.spec = spec
        self.pipeline.encoding = spec.encoding
        self.config_revision += 1

    def snapshot(self, active_operation: Optional[Operation], last_operation: Optional[Operation], queued: int) -> Dict[str, Any]:
        snap = self.pipeline.snapshot()
        self.facts.error_match_count = snap.error_match_count
        return build_snapshot(
            spec=self.spec,
            facts=self.facts,
            assessment=self.assessment,
            config_revision=self.config_revision,
            active_operation=active_operation,
            last_operation=last_operation,
            queued_operations=queued,
            log_meta={
                "epoch": snap.epoch,
                "revision": snap.revision,
                "retained_rows": snap.retained_rows,
                "history_evicted": snap.history_evicted,
                "replay_complete": snap.replay_complete,
                "capture_loss": snap.capture_loss,
            },
        )

    def log_snapshot(self):
        return self.pipeline.snapshot()

    # ------------------------------------------------------------------
    # 判断与事实
    # ------------------------------------------------------------------
    def report_assessment(self, run_id: str, state: str, reason: str, session_id: Optional[str]) -> None:
        self.assessment = AgentAssessment(
            run_id=run_id,
            state=state,
            reason=reason,
            reported_at=to_rfc3339(utc_now()) or "",
            session_id=session_id,
        )
        self.assessment_changed.emit(self.service_id)

    def update_port_observations(self, observations: List[PortObservation]) -> None:
        self.facts.port_observations = observations
        self._refresh_ready_observation()
        self.facts_changed.emit(self.service_id)

    def _refresh_ready_observation(self, log_regex_matched: Optional[bool] = None) -> None:
        """刷新就绪观察。

        `log_regex_matched` 为 None 时按最近的日志窗口重新扫描；当调用方（增量就绪
        检查）已经确定命中时传入 True，避免"扫描窗口看不到、但确实已命中"的不一致。
        """
        ready = self.spec.ready_when
        if not ready:
            self.facts.ready_observation = None
            return
        matched = True
        details: Dict[str, Any] = {}
        if ready.port is not None:
            port_ok = any(
                o.port == ready.port and o.listening and o.owned_by_service
                for o in self.facts.port_observations
            )
            details["port"] = ready.port
            details["port_matched"] = port_ok
            matched = matched and port_ok
        if ready.log_regex:
            if log_regex_matched is None:
                log_regex_matched = self._scan_ready_regex(ready.log_regex)
            details["log_regex"] = ready.log_regex
            details["log_regex_matched"] = log_regex_matched
            matched = matched and log_regex_matched
        self.facts.ready_observation = {
            "matched": matched,
            "checked_at": to_rfc3339(utc_now()),
            "details": details,
        }

    def _scan_ready_regex(self, pattern: str) -> bool:
        """只扫描**当前运行**的日志，且带匹配超时与扫描预算。

        不过滤 run_id 时，上一运行遗留的日志会让新运行的 ready_observation 误判为已命中。
        """
        compiled = _compile_ready_regex(pattern)
        if compiled is None:
            return False
        run_id = self.facts.run_id
        records = self.pipeline.snapshot().records
        budget = READY_SCAN_BUDGET
        for record in records[-READY_SCAN_WINDOW:]:
            if run_id is not None and record.run_id != run_id:
                continue
            budget -= 1
            if budget < 0:
                break
            try:
                if compiled.search(record.text or "", timeout=READY_REGEX_TIMEOUT_S):
                    return True
            except TimeoutError:  # pragma: no cover
                return False
        return False

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    def begin_start(self, operation: Operation, columns: int) -> None:
        if self.running:
            # 已 running 的重复 Start：不产生新 run_id
            self._complete_ok(
                operation,
                {
                    "already_running": True,
                    "run_id": self.facts.run_id,
                    "pid": self.facts.pid,
                },
            )
            return

        self._current_operation = operation
        self._restart_after_stop = False
        self._reset_run_state()
        if columns and columns != self.pipeline.columns:
            self.pipeline.resize(columns)

        run_id = new_run_id()
        self.facts.run_id = run_id
        self.facts.lifecycle = LIFECYCLE_STARTING
        self.facts.pid = None
        self.facts.exit_code = None
        self.facts.started_at = None
        self.facts.stop_requested = False
        self.facts.last_error = None
        self.assessment = None
        self.pipeline.set_run(run_id, to_rfc3339(utc_now()) or "")
        self.facts_changed.emit(self.service_id)

        try:
            self._job = WindowsJob.create()
        except Exception as exc:  # noqa: BLE001
            self._fail_operation(
                operation,
                ErrorCode.JOB_ASSIGN_FAILED,
                f"创建 Job 失败: {exc}",
                {},
            )
            return

        env = self._build_target_env(columns)
        self._env_for_target = env

        process = QProcess(self)
        process.setProcessChannelMode(QProcess.SeparateChannels)
        process.setWorkingDirectory(str(Path(os.environ.get("TEMP", ".")).resolve()))
        qenv = QProcessEnvironment.systemEnvironment()
        src_path = _worker_src_path()
        if src_path:
            existing = qenv.value("PYTHONPATH")
            qenv.insert("PYTHONPATH", src_path + (os.pathsep + existing if existing else ""))
        process.setProcessEnvironment(qenv)
        process.started.connect(self._on_process_started)
        process.finished.connect(self._on_process_finished)
        process.errorOccurred.connect(self._on_process_error)
        process.readyReadStandardOutput.connect(self._on_worker_stdout)
        process.readyReadStandardError.connect(self._on_worker_stderr)
        self._process = process
        self._decoder = FrameDecoder(WORKER_MAX_FRAME)
        self._launch_sent = False
        self._child_started = False
        self._root_exited = False
        self._child_exit_seen = False

        self._start_timer.start(START_TIMEOUT_MS)
        process.start(sys.executable, ["-m", "specta.launch_worker"])

    def _reset_run_state(self) -> None:
        self._stop_requested = False
        self._stop_finished_reported = False
        self._stop_timer.stop()
        self._settle_timer.stop()
        self._ready_scanned_seq = 0

    def _build_target_env(self, columns: int) -> Dict[str, str]:
        env: Dict[str, str] = {}
        for key, value in os.environ.items():
            env[key] = value
        env.update(self.spec.env)
        no_color = env.get("NO_COLOR") not in (None, "")
        if not no_color:
            env.setdefault("FORCE_COLOR", "1")
            env.setdefault("CLICOLOR_FORCE", "1")
            env.setdefault("PY_COLORS", "1")
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONIOENCODING", self.spec.encoding)
        env["COLUMNS"] = str(columns or self.pipeline.columns)
        return env

    def _on_process_started(self) -> None:
        launcher_pid = int(self._process.processId()) if self._process else 0
        self.facts.launcher_pid = launcher_pid
        job = self._job
        if job is None:
            self._abort_launch(ErrorCode.JOB_ASSIGN_FAILED, "Job 未创建", {})
            return
        if not job.assign_pid(launcher_pid):
            if allow_no_job():
                job._degraded = True  # 开发降级：仅靠 taskkill 兜底
            else:
                self._abort_launch(
                    ErrorCode.JOB_ASSIGN_FAILED,
                    "无法把启动器加入 Job（宿主 Job 可能禁止 breakaway）",
                    {"launcher_pid": launcher_pid},
                )
                return
        # 归属已确认，才下发启动指令
        self._send_launch_spec()

    def _send_launch_spec(self) -> None:
        if self._process is None or self._launch_sent:
            return
        launch = self.spec.launch
        spec: Dict[str, Any] = {
            "type": "launch",
            "run_id": self.facts.run_id,
            "mode": launch.mode,
            "cwd": self.spec.cwd,
            "env": getattr(self, "_env_for_target", {}),
            "columns": self.pipeline.columns,
        }
        if launch.mode == "shell":
            spec["command"] = launch.command
        else:
            spec["program"] = launch.program
            spec["args"] = list(launch.args)
        payload = json.dumps(spec, ensure_ascii=False).encode("utf-8") + b"\n"
        self._launch_sent = True
        self._process.write(payload)

    def _on_start_timeout(self) -> None:
        if self._child_started:
            return
        self._abort_launch(ErrorCode.LAUNCH_FAILED, "启动超时：未收到目标进程创建确认", {})

    def _abort_launch(self, code: str, message: str, details: Dict[str, Any]) -> None:
        self.facts.lifecycle = LIFECYCLE_FAILED
        self.facts.last_error = {"code": code, "message": message, "details": details}
        self._start_timer.stop()
        self._kill_process()
        self._cleanup_job()
        operation = self._current_operation
        self._current_operation = None
        if operation is not None:
            self._fail_operation(operation, code, message, details, emit_facts=True)
        else:
            self.facts_changed.emit(self.service_id)

    # ------------------------------------------------------------------
    # 停止
    # ------------------------------------------------------------------
    def begin_stop(self, operation: Operation, restart_after: bool = False) -> None:
        if self.facts.lifecycle in (LIFECYCLE_STOPPED, LIFECYCLE_EXITED, LIFECYCLE_FAILED):
            if restart_after:
                self._current_operation = operation
                self._restart_after_stop = True
                self.stop_done_for_restart.emit(self.service_id, operation.operation_id)
            else:
                self._complete_ok(operation, {"already_stopped": True, "run_id": self.facts.run_id})
            return

        self._current_operation = operation
        self._restart_after_stop = restart_after
        # 必须同时置位内部标志：_finish_exit() 用 self._stop_requested 判定"这是停止
        # 而非自然退出"。只设 facts.stop_requested 会导致停止被当成自然退出，
        # _current_operation 被消费却不上报完成 —— Stop 操作会永远停在 running。
        self._stop_requested = True
        self.facts.stop_requested = True
        self.facts.lifecycle = LIFECYCLE_STOPPING
        self.facts_changed.emit(self.service_id)

        job = self._job
        if job is not None and not job.degraded:
            job.terminate()
        self._kill_process(taskkill=True)
        self._stop_deadline = time.monotonic() + STOP_TIMEOUT_MS / 1000.0
        self._stop_timer.start()

    def _poll_stop(self) -> None:
        job = self._job
        empty = job.is_empty() if job is not None else True
        alive = self._process is not None and self._process.state() != QProcess.NotRunning
        if empty and not alive:
            self._stop_timer.stop()
            self._finish_stop(success=True)
            return
        if time.monotonic() >= self._stop_deadline:
            self._stop_timer.stop()
            self._finish_stop(success=False)

    def _finish_stop(self, success: bool) -> None:
        operation = self._current_operation
        self._current_operation = None
        self._cleanup_job()
        if success:
            self.facts.lifecycle = LIFECYCLE_STOPPED
            self.facts.pid = None
            self.facts.started_at = None
            self.pipeline.append_event("服务已停止", to_rfc3339(utc_now()) or "")
            self.facts_changed.emit(self.service_id)
            if self._restart_after_stop and operation is not None:
                self.stop_done_for_restart.emit(self.service_id, operation.operation_id)
                return
            if operation is not None:
                self._complete_ok(operation, {"stopped": True, "run_id": self.facts.run_id})
        else:
            self.facts.lifecycle = LIFECYCLE_RUNNING if self._child_started else LIFECYCLE_STOPPING
            self.facts_changed.emit(self.service_id)
            if operation is not None:
                self._fail_operation(
                    operation,
                    ErrorCode.STOP_TIMEOUT,
                    "停止超时：仍有进程未退出",
                    {"service_id": self.service_id},
                )

    # ------------------------------------------------------------------
    # 进程 / Job
    # ------------------------------------------------------------------
    def _kill_process(self, taskkill: bool = False) -> None:
        process = self._process
        if process is None:
            return
        pid = int(process.processId()) if process.state() != QProcess.NotRunning else 0
        if process.state() != QProcess.NotRunning:
            process.kill()
        if taskkill and pid:
            import subprocess

            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError:
                pass

    def _cleanup_job(self) -> None:
        job = self._job
        if job is None:
            return
        try:
            if not job.degraded:
                job.terminate()
        finally:
            job.close()
        self._job = None

    # ------------------------------------------------------------------
    # 工作进程消息
    # ------------------------------------------------------------------
    def _on_worker_stdout(self) -> None:
        if self._process is None:
            return
        data = bytes(self._process.readAllStandardOutput())
        if not data:
            return
        try:
            frames = self._decoder.feed(data)
        except FrameError as exc:
            if exc.fatal:
                self._abort_launch(
                    ErrorCode.LAUNCH_PROTOCOL_ERROR,
                    f"启动器协议错误: {exc.reason}",
                    {},
                )
                return
            self.pipeline.append_event(f"启动器协议告警: {exc.reason}", to_rfc3339(utc_now()) or "")
            self._schedule_logs()
            return
        for frame in frames:
            self._handle_worker_frame(frame)

    def _handle_worker_frame(self, frame: Dict[str, Any]) -> None:
        kind = frame.get("type")
        ts = to_rfc3339(utc_now()) or ""
        if kind == W_CHILD_STARTED:
            self._child_started = True
            self._start_timer.stop()
            self.facts.pid = int(frame.get("pid") or 0)
            self.facts.lifecycle = LIFECYCLE_RUNNING
            # 目标进程创建确认的时刻 = 服务启动时间；不要复用 Daemon 的启动时间
            self.facts.started_at = ts or to_rfc3339(utc_now())
            operation = self._current_operation
            self._current_operation = None
            self.facts_changed.emit(self.service_id)
            if operation is not None:
                self._complete_ok(
                    operation,
                    {"run_id": self.facts.run_id, "pid": self.facts.pid, "already_running": False},
                )
        elif kind == W_OUTPUT:
            try:
                chunk = decode_worker_output(frame)
            except FrameError as exc:
                self.pipeline.append_event(f"启动器输出解码失败: {exc.reason}", ts)
                self._schedule_logs()
                return
            self.pipeline.feed_output(chunk, ts, self.facts.run_id)
            # feed_output 只跑一个时间片；把剩余解析工作交给事件循环继续推进
            self._schedule_parse()
            self._check_ready_regex()
            self._schedule_logs()
        elif kind == W_ROOT_EXITED:
            self._root_exited = True
            code = frame.get("exit_code")
            self.facts.exit_code = int(code) if isinstance(code, int) else None
            self._settle_timer.start(SETTLE_MS)
        elif kind == W_CHILD_EXIT:
            self._child_exit_seen = True
            if self._root_exited and not self._settle_timer.isActive():
                self._settle_timer.start(SETTLE_MS)
        elif kind == W_LAUNCH_ERROR:
            error = frame.get("error") or {}
            self._abort_launch(
                error.get("code", ErrorCode.LAUNCH_FAILED),
                error.get("message", "启动失败"),
                error.get("details") or {},
            )

    def _check_ready_regex(self) -> None:
        ready = self.spec.ready_when
        if not ready or not ready.log_regex:
            return
        compiled = _compile_ready_regex(ready.log_regex)
        if compiled is None:
            return
        run_id = self.facts.run_id
        records = self.pipeline.snapshot().records

        def _matched(text: str) -> bool:
            try:
                return bool(compiled.search(text or "", timeout=READY_REGEX_TIMEOUT_S))
            except TimeoutError:  # pragma: no cover
                return False

        # 第 1 遍：活动行（provisional）。活动行会**原地改写**（例如 CR 后重写同一行），
        # sequence 不变但文本已变，必须每次重扫；数量 ≤ 屏幕行数，开销可控。先扫它们
        # 可避免大 backlog 把"当前可见行"的检测饿死（R03）。
        for record in records:
            if not record.provisional:
                continue
            if run_id is not None and record.run_id != run_id:
                continue
            if _matched(record.text):
                self._refresh_ready_observation(True)
                return

        # 第 2 遍：新提交行。已提交行不可变，按单调序号补扫，并受扫描预算约束；
        # 预算按调用逐步推进游标，跨多次调用最终覆盖全部新增行。
        newest = self._ready_scanned_seq
        budget = READY_SCAN_BUDGET
        for record in records:
            if record.provisional:
                continue
            if run_id is not None and record.run_id != run_id:
                continue
            if record.sequence <= self._ready_scanned_seq:
                continue
            budget -= 1
            if budget < 0:
                break
            if record.sequence > newest:
                newest = record.sequence
            if _matched(record.text):
                self._ready_scanned_seq = max(self._ready_scanned_seq, newest)
                self._refresh_ready_observation(True)
                return
        self._ready_scanned_seq = max(self._ready_scanned_seq, newest)

    def _on_worker_stderr(self) -> None:
        if self._process is None:
            return
        data = bytes(self._process.readAllStandardError())
        if not data:
            return
        text = data.decode("utf-8", "replace").strip()
        if text:
            self._worker_stderr.append(text)
            self._worker_stderr = self._worker_stderr[-20:]
            # 启动器诊断是 Specta 事件，不算目标输出
            for line in text.splitlines()[-3:]:
                self.pipeline.append_event(f"启动器: {line}", to_rfc3339(utc_now()) or "")
            self._schedule_logs()

    def _on_process_finished(self, exit_code: int, status) -> None:
        self._start_timer.stop()
        if not self._root_exited:
            self._root_exited = True
            if self._child_started:
                self.facts.exit_code = int(exit_code)
            else:
                # 启动器在创建目标前就退出了
                self.facts.last_error = {
                    "code": ErrorCode.LAUNCH_FAILED,
                    "message": f"启动器提前退出（code={exit_code}）",
                    "details": {},
                }
                self.facts.lifecycle = LIFECYCLE_FAILED
                operation = self._current_operation
                self._current_operation = None
                if operation is not None:
                    self._fail_operation(
                        operation,
                        ErrorCode.LAUNCH_FAILED,
                        self.facts.last_error["message"],
                        {},
                    )
                else:
                    self.facts_changed.emit(self.service_id)
                self._cleanup_job()
                return
        if not self._settle_timer.isActive():
            self._settle_timer.start(SETTLE_MS)

    def _on_process_error(self, error) -> None:
        if error == QProcess.FailedToStart:
            self._abort_launch(
                ErrorCode.LAUNCH_FAILED, "无法启动受控启动器（python -m specta.launch_worker）", {}
            )

    def _finish_exit(self) -> None:
        was_stop = self._stop_requested
        operation = self._current_operation
        self._current_operation = None
        self._stop_requested = False
        # 进程已经退出，停止轮询；否则 _poll_stop 会在退出后再跑一次 _finish_stop
        self._stop_timer.stop()
        self._cleanup_job()
        self._kill_process()
        self.facts.pid = None
        self.facts.started_at = None
        self.pipeline.append_event(
            f"进程退出（exit={self.facts.exit_code}）" if self.facts.exit_code is not None else "进程退出",
            to_rfc3339(utc_now()) or "",
        )
        if was_stop:
            self.facts.lifecycle = LIFECYCLE_STOPPED
            self.facts_changed.emit(self.service_id)
            if self._restart_after_stop and operation is not None:
                self.stop_done_for_restart.emit(self.service_id, operation.operation_id)
                return
            if operation is not None:
                self._complete_ok(operation, {"stopped": True, "run_id": self.facts.run_id})
        else:
            self.facts.lifecycle = LIFECYCLE_EXITED
            self.facts_changed.emit(self.service_id)
            if operation is not None:
                # 启动操作在 child_started 时已完成；此处不再重复
                pass
        self._schedule_logs()

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------
    def _schedule_logs(self) -> None:
        if not self._logs_timer.isActive():
            self._logs_timer.start()

    def _schedule_parse(self) -> None:
        """待解析数据未清空时，安排下一个解析时间片。"""
        if self.pipeline.has_pending and not self._parse_timer.isActive():
            self._parse_timer.start()

    def _drain_parse(self) -> None:
        """推进一个解析时间片；仍有剩余则继续排队，直到清空。"""
        more = self.pipeline.pump(PARSE_TIME_SLICE_S)
        if more:
            self._parse_timer.start()
        else:
            self._schedule_logs()
        # 就绪标记也可能因异步解析推进才进入可查询日志（此时没有新的输出帧），
        # 因此在每个时间片结束后补一次就绪检查，否则尾部标记会被永久漏检（R03）。
        self._check_ready_regex()

    def _emit_logs(self) -> None:
        snap = self.pipeline.snapshot()
        self.logs_changed.emit(self.service_id, snap.epoch, snap.revision)

    def _complete_ok(self, operation: Operation, result: Dict[str, Any]) -> None:
        self.operation_done.emit(self.service_id, operation.operation_id, True, result)

    def _fail_operation(
        self,
        operation: Operation,
        code: str,
        message: str,
        details: Dict[str, Any],
        emit_facts: bool = False,
    ) -> None:
        self.operation_done.emit(
            self.service_id,
            operation.operation_id,
            False,
            {"error": {"code": code, "message": message, "details": details or {}}},
        )
        if emit_facts:
            self.facts_changed.emit(self.service_id)

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """Daemon 退出时清理该服务。"""
        self._start_timer.stop()
        self._settle_timer.stop()
        self._stop_timer.stop()
        self._parse_timer.stop()
        self._logs_timer.stop()
        # 退出收尾：明确消费剩余待解析数据，不依赖下一条服务日志才推进
        self.pipeline.drain(max_seconds=0.5)
        self._kill_process(taskkill=True)
        self._cleanup_job()
