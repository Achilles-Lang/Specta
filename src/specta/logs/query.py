"""日志查询：错误块、上下文、关键字匹配、分页与截断。

UI 的"仅错误"与 MCP 的 `filter_error` 调同一套逻辑，保证两端看到相同的块。
本模块是纯函数集合，不持有状态、不复制日志。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..domain.models import LOG_KIND_EVENT, LogRecord

DEFAULT_CONTEXT_LINES = 8
MAX_SELECT_LINES = 500
MAX_SELECT_BYTES = 128 * 1024

TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)")
CAUSED_BY_RE = re.compile(r"\bCaused by:\s")
EXC_CLASS_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Traceback)\b")
ERROR_WORD_RE = re.compile(r"(?i)\berrors?\b")
ZERO_ERRORS_RE = re.compile(r"(?i)\b(?:0|no|zero)\s+errors?\b")
EADDRINUSE_RE = re.compile(r"(?i)\bEADDRINUSE\b|\baddress already in use\b")
FAILED_COMPILE_RE = re.compile(r"(?i)\bfailed to compile\b|\bcompilation failed\b")
PANIC_RE = re.compile(r"(?i)^\s*(?:panic:|critical:|unhandled exception)")
MORE_RE = re.compile(r"^\s*\.\.\.\s*\d+\s+more\s*$")
SUPPRESSED_RE = re.compile(r"^\s*Suppressed:\s")
AT_RE = re.compile(r"^\s+at\s")


def is_error_line(text: Optional[str]) -> bool:
    """错误指纹判定。命中仅记录"事实"，不直接判定服务故障。"""
    if not text or not text.strip():
        return False
    if TRACEBACK_RE.search(text):
        return True
    if CAUSED_BY_RE.search(text):
        return True
    if EXC_CLASS_RE.search(text):
        return True
    if EADDRINUSE_RE.search(text):
        return True
    if FAILED_COMPILE_RE.search(text):
        return True
    if PANIC_RE.search(text):
        return True
    if ERROR_WORD_RE.search(text) and not ZERO_ERRORS_RE.search(text):
        return True
    return False


def is_continuation(text: Optional[str]) -> bool:
    """堆栈续行：缩进行、at 行、... N more、Suppressed: 等。"""
    if not text:
        return False
    if not text.strip():
        return False
    if MORE_RE.match(text) or SUPPRESSED_RE.match(text) or AT_RE.match(text):
        return True
    return text[0] in (" ", "\t")


def error_blocks(records: Sequence[LogRecord], context_lines: int) -> List[Tuple[int, int]]:
    """返回错误块在 records 中的闭区间索引列表（已合并相邻/重叠）。"""
    n = len(records)
    intervals: List[Tuple[int, int]] = []
    i = 0
    while i < n:
        record = records[i]
        if not record.error_hit:
            i += 1
            continue
        head = i
        # 若命中行本身是续行，向上找块头
        while head - 1 >= 0 and is_continuation(records[head].text):
            head -= 1
        end = i
        while end + 1 < n and is_continuation(records[end + 1].text):
            end += 1
        # Python traceback：向上并入 Traceback 行
        j = head - 1
        limit = max(0, head - context_lines)
        while j >= limit:
            if TRACEBACK_RE.search(records[j].text or ""):
                head = j
                break
            j -= 1
        lo = max(0, head - context_lines)
        hi = min(n - 1, end + context_lines)
        intervals.append((lo, hi))
        i = end + 1

    if not intervals:
        return []
    merged: List[Tuple[int, int]] = [intervals[0]]
    for lo, hi in intervals[1:]:
        last_lo, last_hi = merged[-1]
        if lo <= last_hi + 1:
            merged[-1] = (last_lo, max(last_hi, hi))
        else:
            merged.append((lo, hi))
    return merged


def _slice(records: Sequence[LogRecord], lo: int, hi: int) -> List[LogRecord]:
    return list(records[lo : hi + 1])


def error_view(
    records: Sequence[LogRecord],
    context_lines: int = DEFAULT_CONTEXT_LINES,
    max_rows: int = MAX_SELECT_LINES,
) -> Tuple[List[LogRecord], bool]:
    """从最新错误块往前取，最多 max_rows 行；返回 (记录, 是否发生截断)。"""
    blocks = error_blocks(records, context_lines)
    if not blocks:
        return [], False
    selected: List[LogRecord] = []
    truncated = False
    for lo, hi in reversed(blocks):
        chunk = _slice(records, lo, hi)
        if len(selected) + len(chunk) > max_rows:
            room = max_rows - len(selected)
            if room <= 0:
                truncated = True
                break
            # 保留该块最新（靠后）的部分
            selected = chunk[-room:] + selected
            truncated = True
            break
        selected = chunk + selected
    return selected, truncated


def find_matches(
    records: Sequence[LogRecord], keyword: str, case_sensitive: bool = False
) -> List[int]:
    if not keyword:
        return []
    needle = keyword if case_sensitive else keyword.lower()
    hits: List[int] = []
    for idx, record in enumerate(records):
        haystack = record.text if case_sensitive else (record.text or "").lower()
        if needle in haystack:
            hits.append(idx)
    return hits


@dataclass
class ViewResult:
    records: List[LogRecord]
    truncated: bool = False
    block_truncated: bool = False
    match_indices: List[int] = None  # type: ignore[assignment]
    match_count: int = 0

    def __post_init__(self) -> None:
        if self.match_indices is None:
            self.match_indices = []


def select_view(
    records: Sequence[LogRecord],
    *,
    filter_error: bool = False,
    keyword: str = "",
    only_matches: bool = False,
    context_lines: int = DEFAULT_CONTEXT_LINES,
    max_rows: int = MAX_SELECT_LINES,
) -> ViewResult:
    """UI 过滤视图。语义：先形成错误块视图，再按关键字筛选。"""
    result = ViewResult(records=[])
    if filter_error:
        base, truncated = error_view(records, context_lines, max_rows)
        result.truncated = truncated
        result.block_truncated = truncated
    else:
        base = list(records)
        if len(base) > max_rows:
            base = base[-max_rows:]
            result.truncated = True

    if keyword:
        matches = find_matches(base, keyword)
        result.match_count = len(matches)
        result.match_indices = matches
        if only_matches:
            result.records = [base[i] for i in matches]
            return result

    result.records = base
    return result


def select_recent(
    records: Sequence[LogRecord],
    *,
    lines: int = 100,
    filter_error: bool = False,
    context_lines: int = DEFAULT_CONTEXT_LINES,
    after_sequence: Optional[int] = None,
    max_bytes: int = MAX_SELECT_BYTES,
) -> Dict[str, Any]:
    """MCP get_recent_logs 的选取逻辑；返回 (records, meta)。"""
    if filter_error:
        # 错误模式：先在全部保留历史里识别错误块，再从最新块往前取
        base, block_truncated = error_view(records, context_lines, lines)
    else:
        base = [r for r in records if r.kind != LOG_KIND_EVENT or True]
        block_truncated = False
        if after_sequence is not None:
            base = [r for r in base if r.sequence > after_sequence]
        if len(base) > lines:
            base = base[-lines:]
            block_truncated = False

    if after_sequence is not None and filter_error:
        base = [r for r in base if r.sequence > after_sequence]

    # 字节预算：从最新往前累计
    total = 0
    kept: List[LogRecord] = []
    truncated = False
    for record in reversed(base):
        size = len((record.text or "").encode("utf-8"))
        if total + size > max_bytes:
            truncated = True
            break
        total += size
        kept.append(record)
    kept.reverse()
    if len(kept) < len(base):
        truncated = True

    return {
        "records": kept,
        "truncated": truncated,
        "block_truncated": block_truncated,
        "returned_lines": len(kept),
    }


def count_errors(records: Sequence[LogRecord]) -> int:
    return sum(1 for r in records if r.error_hit)
