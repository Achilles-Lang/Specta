"""单服务日志摄入管线。

三层模型（规格 9）：

  1. 原始字节层：保留每服务最近 2MiB 未经解析的原始字节（附 run_id / 接收时间 /
     offset），供窗口列宽变化时重放。
  2. 解析层：pyte `HistoryScreen(columns, rows=80)`。屏幕高度固定 80 行，与窗口
     像素高度解耦 —— 拖高窗口不会改变日志历史。
  3. 记录层：输出历史 + Specta 事件 + 活动屏幕，UI 与 MCP 共用同一份快照。

关键语义：
  * 光标上移回写只更新当前屏幕行，不追加中间态；滚出屏幕顶部的行才成为输出历史。
  * 输出历史与 Specta 事件分开保存：resize 重放只重建输出历史，事件按“事件发生前
    已产生的输出行数”重新插回，不会因为重放而丢失或错位。
  * sequence 在单服务生命周期内严格单调递增：清屏与重放都会继续分配更大的序号，
    不会回退。
  * 清屏丢弃原始字节，因此 resize 无法把已清内容重放回来。
  * 待解析字节按 run 归属：`_pending` 仍是原始 bytearray（保持既有内部契约），
    另用 `_pending_spans` 记录每段字节属于哪个运行。运行切换后，切换前积压的旧
    运行输出仍归属旧运行，不会因为共享一个全局“当前运行”而被误标为新运行（R02）。

换行归一化（无 PTY 的补偿）：目标进程接的是管道而不是终端，终端驱动通常提供的
ONLCR（把单独的 LF 补成 CRLF）因此缺失。若不做处理，Node/Java/Go 这类只发 LF 的
程序在 pyte 的屏幕模型里会逐行"阶梯"式右移。这里在解码后的文本层把"前面不是 CR 的
LF"补成 CRLF，等价于终端的行为；`\r`（进度条回写）不受影响。
"""

from __future__ import annotations

import codecs
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

from ..domain.models import (
    LOG_KIND_EVENT,
    LOG_KIND_OUTPUT,
    LogRecord,
)
from .query import is_error_line
from .screen import new_screen, new_stream, render_line

RAW_LIMIT = 2 * 1024 * 1024
CONTENT_BUDGET = 4 * 1024 * 1024
MAX_RETAINED_ROWS = 3000
MAX_EVENT_ROWS = 800
PENDING_LIMIT = 4 * 1024 * 1024
PARSE_CHUNK = 8192
PARSE_TIME_SLICE_S = 0.008

SCREEN_ROWS = 80
DEFAULT_COLUMNS = 120
MIN_COLUMNS = 40
MAX_COLUMNS = 400
HISTORY_CAPACITY = 20000

MAX_LINE_CHARS = 64 * 1024

EVENT_PREFIX = "[Specta]"

_LONE_LF_RE = re.compile(r"(?<!\r)\n")


def normalize_newlines(text: str) -> str:
    """把孤立的 LF 补成 CRLF（等价终端 ONLCR），保证跨语言输出不阶梯式错位。"""
    return _LONE_LF_RE.sub("\r\n", text)


@dataclass
class Snapshot:
    records: List[LogRecord]
    epoch: int
    revision: int
    retained_rows: int
    current_run_id: Optional[str]
    columns: int
    history_evicted: bool
    replay_complete: bool
    capture_loss: bool
    error_match_count: int
    generation: int = 1
    evicted_total: int = 0

    def meta(self) -> Dict[str, Any]:
        return {
            "epoch": self.epoch,
            "revision": self.revision,
            "retained_rows": self.retained_rows,
            "current_run_id": self.current_run_id,
            "columns": self.columns,
            "history_evicted": self.history_evicted,
            "replay_complete": self.replay_complete,
            "capture_loss": self.capture_loss,
            "error_match_count": self.error_match_count,
            "generation": self.generation,
            "evicted_total": self.evicted_total,
        }


# 待解析字节的归属片段：(字节数, run_id, ts)。
_PendingSpan = Tuple[int, Optional[str], str]


class LogPipeline:
    def __init__(
        self,
        service_id: str,
        encoding: str = "utf-8",
        columns: int = DEFAULT_COLUMNS,
        rows: int = SCREEN_ROWS,
    ) -> None:
        self.service_id = service_id
        self.encoding = encoding
        self._rows = rows
        self._columns = _clamp_columns(columns)

        self._screen = new_screen(self._columns, self._rows, HISTORY_CAPACITY)
        self._stream = new_stream(self._screen)
        self._decoder = _new_decoder(encoding)

        # 输出历史（已滚出屏幕的逻辑行）
        self._output: Deque[LogRecord] = deque()
        self._content_bytes = 0
        self._output_appended = 0     # 累计追加的输出行数（单调，不因淘汰回退）
        self._evicted_total = 0       # 累计淘汰的输出行数

        # Specta 事件：(事件发生前已产生的输出行数, 记录)
        self._events: Deque[Tuple[int, LogRecord]] = deque()

        self._next_sequence = 1
        self._max_emitted = 0

        self._settled_errors = 0
        self._activity_errors = 0
        self._error_count = 0

        self._raw_blocks: Deque[Dict[str, Any]] = deque()
        self._raw_start_offset = 0
        self._raw_total = 0
        # 运行边界：(原始字节 offset, run_id, ts)。重放时按边界恢复运行归属与屏幕状态。
        self._run_boundaries: List[Tuple[int, str, str]] = []

        # 待解析字节：bytearray 本体 + 逐段归属。切换运行时不改动已排队的字节，
        # 解析时按片段自身的 run_id 归属记录，避免旧积压被标成新运行（R02）。
        self._pending = bytearray()
        self._pending_spans: Deque[_PendingSpan] = deque()
        self._last_ts: str = ""

        self._epoch = 1
        self._revision = 1
        self._generation = 1
        self._history_evicted = False
        self._replay_complete = True
        self._capture_loss = False

        self._current_run_id: Optional[str] = None
        # 屏幕上"当前活跃内容"所属的运行：活动行会原地改写，其归属必须跟随最后一次
        # 被绘入屏幕的字节，而不是全局当前运行（否则旧运行积压绘入后会被标成新运行）。
        self._active_run_id: Optional[str] = None

        self._activity: List[LogRecord] = []
        self._activity_key: Tuple = ()
        self._dirty = True

    # ------------------------------------------------------------------
    # 基本信息
    # ------------------------------------------------------------------
    @property
    def columns(self) -> int:
        return self._columns

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    @property
    def current_run_id(self) -> Optional[str]:
        return self._current_run_id

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    @property
    def replay_complete(self) -> bool:
        return self._replay_complete

    @property
    def error_count(self) -> int:
        return self._error_count

    # ------------------------------------------------------------------
    # 运行边界与事件
    # ------------------------------------------------------------------
    def set_run(self, run_id: str, ts: str, separator: Optional[str] = None) -> None:
        # 上一运行的屏幕内容先冻结为输出历史，避免旧 run 的活动行与新 run 的序号冲突
        self._commit_activity()
        # 记录运行边界：此偏移之后的原始字节属于新运行，重放时据此还原 run_id
        self._run_boundaries.append((self._raw_total, run_id, ts))
        self._current_run_id = run_id
        self.append_event(separator or f"---- 运行 {run_id} 开始 ----", ts)

    def append_event(self, text: str, ts: str, run_id: Optional[str] = None) -> LogRecord:
        self._commit_activity()
        record = LogRecord(
            sequence=self._alloc_sequence(),
            run_id=run_id or self._current_run_id,
            ts=ts,
            text=f"{EVENT_PREFIX} {text}",
            spans=[],
            kind=LOG_KIND_EVENT,
            provisional=False,
            started_at=time.monotonic(),
        )
        self._events.append((self._output_appended, record))
        while len(self._events) > MAX_EVENT_ROWS:
            self._events.popleft()
        self._dirty = True
        self._revision += 1
        return record

    def _alloc_sequence(self) -> int:
        seq = self._next_sequence
        self._next_sequence += 1
        if seq > self._max_emitted:
            self._max_emitted = seq
        return seq

    # ------------------------------------------------------------------
    # 摄入
    # ------------------------------------------------------------------
    def feed_output(self, data: bytes, ts: str, run_id: Optional[str] = None) -> None:
        if run_id:
            self._current_run_id = run_id
        if not data:
            return
        self._last_ts = ts
        self._append_raw(data, ts, self._current_run_id)
        self._extend_pending(data, ts, self._current_run_id)
        if len(self._pending) > PENDING_LIMIT:
            drop = len(self._pending) - PENDING_LIMIT
            self._drop_pending(drop)
            self._capture_loss = True
            self._decoder = _new_decoder(self.encoding)
            self.append_event(f"过载丢弃 {drop} 字节待解析数据（解析状态已重置）", ts)
        self.pump()

    # -- 待解析队列（bytearray + 归属片段）--------------------------------
    def _ensure_spans(self) -> None:
        """保证归属片段覆盖全部待解析字节。

        正常路径下 `_extend_pending` 始终同步维护片段；只有测试直接改写 `_pending`
        这类旁路才可能留下未归属字节，按当前运行补一段（追加到末尾，保持字节顺序）。
        """
        if not self._pending:
            self._pending_spans.clear()
            return
        covered = 0
        for count, _run, _ts in self._pending_spans:
            covered += count
        gap = len(self._pending) - covered
        if gap > 0:
            self._pending_spans.append((gap, self._current_run_id, self._last_ts))

    def _extend_pending(self, data: bytes, ts: str, run_id: Optional[str]) -> None:
        self._pending.extend(data)
        if self._pending_spans and self._pending_spans[-1][1] == run_id:
            count, run, _old_ts = self._pending_spans[-1]
            self._pending_spans[-1] = (count + len(data), run, ts)
        else:
            self._pending_spans.append((len(data), run_id, ts))

    def _drop_pending(self, count: int) -> None:
        """从队首丢弃 count 字节（待解析上限淘汰走此路径）。"""
        self._ensure_spans()
        del self._pending[:count]
        while count > 0 and self._pending_spans:
            size, run, ts = self._pending_spans[0]
            if size <= count:
                self._pending_spans.popleft()
                count -= size
            else:
                self._pending_spans[0] = (size - count, run, ts)
                count = 0
        if not self._pending:
            self._pending_spans.clear()

    def _take_pending_chunk(self, size: int) -> Tuple[bytes, Optional[str], str]:
        """从队首取出至多 size 字节；不跨运行边界，返回 (chunk, run_id, ts)。"""
        self._ensure_spans()
        if not self._pending_spans:  # 兜底：无归属信息时按当前运行处理
            chunk = bytes(self._pending[:size])
            del self._pending[: len(chunk)]
            return chunk, self._current_run_id, self._last_ts
        span_size, run_id, ts = self._pending_spans[0]
        take = min(size, span_size)
        chunk = bytes(self._pending[:take])
        del self._pending[:take]
        if take >= span_size:
            self._pending_spans.popleft()
        else:
            self._pending_spans[0] = (span_size - take, run_id, ts)
        return chunk, run_id, ts

    def pump(self, max_seconds: float = PARSE_TIME_SLICE_S) -> bool:
        """在受控时间片内推进解析；返回是否仍有待解析数据。"""
        if not self._pending:
            return False
        deadline = time.perf_counter() + max_seconds
        progressed = False
        # 解析期间逐块切换"当前运行"：每个积压块按自身 run_id 归属，切到新运行后
        # 队列里的旧块依旧标旧运行。循环结束后恢复最新的当前运行。
        current_run = self._current_run_id
        while self._pending:
            chunk, run_id, ts = self._take_pending_chunk(PARSE_CHUNK)
            self._current_run_id = run_id
            text = self._decoder.decode(chunk)
            if text:
                self._feed_text(text, ts)
                progressed = True
            if time.perf_counter() >= deadline:
                break
        self._current_run_id = current_run
        if progressed:
            self._recompute_activity()
        return bool(self._pending)

    def drain(self, max_seconds: float = 5.0) -> None:
        """在给定预算内把待解析数据全部消化（测试与退出收尾用）。"""
        deadline = time.perf_counter() + max_seconds
        while self._pending and time.perf_counter() < deadline:
            self.pump(max(0.05, min(0.25, deadline - time.perf_counter())))
        self._recompute_activity()

    def _feed_text(self, text: str, ts: str) -> None:
        if not text:
            return
        # 记录此刻绘入屏幕的字节属于哪个运行；活动行归属据此，而非全局当前运行。
        self._active_run_id = self._current_run_id
        text = normalize_newlines(text)
        if len(text) > MAX_LINE_CHARS * 4:
            for i in range(0, len(text), MAX_LINE_CHARS):
                self._stream.feed(text[i : i + MAX_LINE_CHARS])
        else:
            self._stream.feed(text)
        self._harvest(ts)

    # ------------------------------------------------------------------
    # 输出历史收割
    # ------------------------------------------------------------------
    def _harvest(self, ts: str) -> None:
        top = self._screen.history.top
        if not top:
            return
        lines = list(top)
        top.clear()
        for line in lines:
            rendered = render_line(line, self._columns)
            record = LogRecord(
                sequence=self._alloc_sequence(),
                run_id=self._current_run_id,
                ts=ts,
                text=rendered.text,
                spans=rendered.spans,
                kind=LOG_KIND_OUTPUT,
                provisional=False,
                started_at=time.monotonic(),
            )
            record.error_hit = is_error_line(rendered.text)
            self._append_output(record)

    def _append_output(self, record: LogRecord) -> None:
        self._output.append(record)
        self._output_appended += 1
        self._content_bytes += len((record.text or "").encode("utf-8"))
        if record.error_hit:
            self._settled_errors += 1
        self._error_count = self._settled_errors + self._activity_errors
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        evicted = False
        while self._output and (
            len(self._output) > MAX_RETAINED_ROWS or self._content_bytes > CONTENT_BUDGET
        ):
            old = self._output.popleft()
            self._content_bytes -= len((old.text or "").encode("utf-8"))
            if old.error_hit:
                self._settled_errors = max(0, self._settled_errors - 1)
            self._evicted_total += 1
            evicted = True
        if evicted:
            self._history_evicted = True
        self._error_count = self._settled_errors + self._activity_errors

    # ------------------------------------------------------------------
    # 活动屏幕
    # ------------------------------------------------------------------
    def _recompute_activity(self) -> None:
        # 按整个有效屏幕区域生成投影，而不是按光标位置裁切：光标上移（ESC[nA）只是
        # 移动后续写入位置，并不删除光标下方的既有内容；按 cursor_y 裁切会让仍在屏幕
        # 上的行从快照里凭空消失。真正的擦除由终端转义序列改屏幕缓冲来体现。
        rows: List[LogRecord] = []
        for y in range(0, self._rows):
            rendered = render_line(self._screen.buffer[y], self._columns)
            record = LogRecord(
                sequence=self._next_sequence + y,
                run_id=self._active_run_id,
                ts=self._last_ts,
                text=rendered.text,
                spans=rendered.spans,
                kind=LOG_KIND_OUTPUT,
                provisional=True,
                wrapped_from=None,
            )
            record.error_hit = is_error_line(rendered.text)
            rows.append(record)
        # 去掉尾部空行，避免每次换行都留一行幽灵空行
        while rows and not rows[-1].text:
            rows.pop()
        if rows and rows[-1].sequence > self._max_emitted:
            self._max_emitted = rows[-1].sequence
        key = tuple((r.sequence, r.run_id, r.text, len(r.spans)) for r in rows)
        if key != self._activity_key:
            self._activity_key = key
            self._revision += 1
        self._activity = rows
        self._activity_errors = sum(1 for r in rows if r.error_hit)
        self._error_count = self._settled_errors + self._activity_errors
        self._dirty = False

    def _commit_activity(self) -> None:
        """把当前活动屏幕行冻结为输出历史，并把实时投影重置为空屏幕。

        冻结与重置必须成对：否则刚写入历史的行会在下一次 `_recompute_activity`
        里被当成"新的活动行"再次输出，产生重复。
        """
        if self._dirty or not self._activity:
            self._recompute_activity()
        if not self._activity:
            return
        for record in self._activity:
            frozen = LogRecord(
                sequence=self._alloc_sequence(),
                run_id=record.run_id,
                ts=record.ts,
                text=record.text,
                spans=list(record.spans),
                kind=LOG_KIND_OUTPUT,
                provisional=False,
                started_at=record.started_at,
            )
            frozen.error_hit = is_error_line(record.text)
            self._append_output(frozen)
        self._reset_screen()

    def _reset_screen(self) -> None:
        """丢弃实时投影（屏幕 + 活动行），输出历史保持不变。"""
        self._screen = new_screen(self._columns, self._rows, HISTORY_CAPACITY)
        self._stream = new_stream(self._screen)
        self._activity = []
        self._activity_key = ()
        self._activity_errors = 0
        self._error_count = self._settled_errors + self._activity_errors
        self._dirty = False

    # ------------------------------------------------------------------
    # 原始字节层
    # ------------------------------------------------------------------
    def _append_raw(self, data: bytes, ts: str, run_id: Optional[str] = None) -> None:
        self._raw_blocks.append(
            {"offset": self._raw_total, "ts": ts, "data": data, "run_id": run_id}
        )
        self._raw_total += len(data)
        while self._raw_bytes() > RAW_LIMIT and len(self._raw_blocks) > 1:
            dropped = self._raw_blocks.popleft()
            self._raw_start_offset = self._raw_blocks[0]["offset"]
            self._replay_complete = False
            del dropped
        # 已被淘汰偏移之前的运行边界也要丢弃，避免重放时归错运行
        while self._run_boundaries and self._run_boundaries[0][0] < self._raw_start_offset:
            self._run_boundaries.pop(0)

    def _raw_bytes(self) -> int:
        return sum(len(b["data"]) for b in self._raw_blocks)

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def _merge_events(self, output: List[LogRecord]) -> List[LogRecord]:
        events = list(self._events)
        if not events:
            return output
        merged: List[LogRecord] = []
        ei = 0
        n = len(events)
        for idx, row in enumerate(output):
            while ei < n:
                relative = events[ei][0] - self._evicted_total
                if relative <= idx:
                    merged.append(events[ei][1])
                    ei += 1
                else:
                    break
            merged.append(row)
        while ei < n:
            merged.append(events[ei][1])
            ei += 1
        return merged

    def snapshot(self, max_rows: Optional[int] = None) -> Snapshot:
        if self._dirty or not self._activity:
            self._recompute_activity()
        records = self._merge_events(list(self._output)) + list(self._activity)
        if max_rows and len(records) > max_rows:
            records = records[-max_rows:]
        return Snapshot(
            records=records,
            epoch=self._epoch,
            revision=self._revision,
            retained_rows=len(records),
            current_run_id=self._current_run_id,
            columns=self._columns,
            history_evicted=self._history_evicted,
            replay_complete=self._replay_complete,
            capture_loss=self._capture_loss,
            error_match_count=self._error_count,
            generation=self._generation,
            evicted_total=self._evicted_total,
        )

    def all_records(self) -> List[LogRecord]:
        return self.snapshot().records

    # ------------------------------------------------------------------
    # 清屏 / 重建
    # ------------------------------------------------------------------
    def clear(self, ts: str) -> None:
        self._output.clear()
        self._events.clear()
        self._content_bytes = 0
        self._settled_errors = 0
        self._activity_errors = 0
        self._error_count = 0
        self._activity = []
        self._activity_key = ()
        self._output_appended = 0
        self._evicted_total = 0
        self._screen = new_screen(self._columns, self._rows, HISTORY_CAPACITY)
        self._stream = new_stream(self._screen)
        self._decoder = _new_decoder(self.encoding)
        # 清屏必须同时丢掉尚未解析的字节：否则下一次 pump 会把清屏前的输出重新
        # 呈现在新 epoch 里，UI 与 Agent 都会读到"已清掉"的内容。
        self._pending.clear()
        self._pending_spans.clear()
        self._raw_blocks.clear()
        self._raw_start_offset = 0
        self._raw_total = 0
        self._run_boundaries.clear()
        # 序号继续向前，清屏不回退
        self._next_sequence = self._max_emitted + 1
        self._epoch += 1
        self._generation += 1
        self._revision += 1
        self._history_evicted = False
        self._capture_loss = False
        self._replay_complete = False
        self._active_run_id = None
        self._dirty = True
        self.append_event("日志已清屏（Agent 后续也无法读取这些日志）", ts)

    def resize(self, columns: int) -> bool:
        """窗口列宽变化。返回是否发生了重放。"""
        new_cols = _clamp_columns(columns)
        if new_cols == self._columns:
            return False
        replayed = False
        if self._raw_start_offset == 0 and self._raw_blocks:
            self._rebuild_from_raw(new_cols)
            replayed = True
        else:
            self._commit_activity()
            self._columns = new_cols
            self._screen = new_screen(new_cols, self._rows, HISTORY_CAPACITY)
            self._stream = new_stream(self._screen)
            self._activity = []
            self._activity_key = ()
            self._replay_complete = False
            self.append_event(
                "窗口宽度变化：部分旧终端布局按原宽保留", self._last_ts or _now_ts()
            )
        self._generation += 1
        self._dirty = True
        self._revision += 1
        return replayed

    def _rebuild_from_raw(self, columns: int) -> None:
        """以新列宽重放完整原始输出；Specta 事件按原位置重新插回。"""
        events_before = [
            (max(0, ordinal - self._evicted_total), record) for ordinal, record in self._events
        ]

        self._columns = columns
        self._screen = new_screen(columns, self._rows, HISTORY_CAPACITY)
        self._stream = new_stream(self._screen)
        self._decoder = _new_decoder(self.encoding)
        self._output.clear()
        self._content_bytes = 0
        self._settled_errors = 0
        self._activity = []
        self._activity_key = ()
        self._output_appended = 0
        self._evicted_total = 0
        self._error_count = 0
        self._pending.clear()
        self._pending_spans.clear()
        self._active_run_id = None

        boundaries = sorted(self._run_boundaries)
        bi = 0
        self._current_run_id = None
        for block in self._raw_blocks:
            offset = block["offset"]
            # 到达运行边界：冻结上一运行的活动屏幕并重置投影（等价于当时的 set_run），
            # 保证旧运行的行不会被重新归属到新运行。
            while bi < len(boundaries) and boundaries[bi][0] <= offset:
                _boundary_offset, boundary_run, _boundary_ts = boundaries[bi]
                bi += 1
                self._commit_activity()
                self._current_run_id = boundary_run
            block_run = block.get("run_id")
            if block_run is not None:
                self._current_run_id = block_run
            text = self._decoder.decode(block["data"])
            if text:
                self._last_ts = block["ts"]
                self._feed_text(text, block["ts"])
        # 活跃流重放：**不做 final flush**。此刻并没有目标进程 EOF，原始窗口尾部可能
        # 正处在多字节字符中间；final=True 会把未完成字符替换为 U+FFFD 并清掉 decoder
        # 的待续字节，导致分块多字节字符（如分块中文）变乱码（R04）。这里保留待续状态，
        # 后续 feed_output 可继续拼接。
        tail = self._decoder.decode(b"")
        if tail:
            self._feed_text(tail, self._last_ts)

        rebased: Deque[Tuple[int, LogRecord]] = deque()
        total = self._output_appended
        for ordinal, record in events_before:
            rebased.append((min(ordinal, total), record))
        self._events = rebased
        self._replay_complete = True
        self._dirty = True


def _new_decoder(encoding: str):
    return codecs.getincrementaldecoder(encoding)("replace")


def _clamp_columns(value: int) -> int:
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        return DEFAULT_COLUMNS
    return max(MIN_COLUMNS, min(MAX_COLUMNS, ivalue))


def _now_ts() -> str:
    from ..domain.models import to_rfc3339, utc_now

    return to_rfc3339(utc_now()) or ""


__all__ = [
    "LogPipeline",
    "Snapshot",
    "normalize_newlines",
    "SCREEN_ROWS",
    "DEFAULT_COLUMNS",
    "MIN_COLUMNS",
    "MAX_COLUMNS",
    "MAX_RETAINED_ROWS",
    "MAX_EVENT_ROWS",
    "CONTENT_BUDGET",
    "RAW_LIMIT",
    "PENDING_LIMIT",
    "EVENT_PREFIX",
]
