"""每服务一个的日志视图：只读文本、搜索高亮、滚动管理、增量绘制。

渲染模型：
  * 已提交历史行是**追加**的（同一 epoch 内不会变）；
  * 活动屏幕行是**可变**的，始终位于尾部，每次刷新整段替换；
  * 顶部淘汰以"删除同样多的块"镜像（数据层是保留策略的唯一权威）；
  * 任何计数不一致都回退为全量重建，保证界面永不与数据层脱节。

时间戳以窄前缀绘制（次文字色），跨度样式只作用于正文；搜索与匹配计数都基于
正文文本，不受前缀影响。"复制全部"会剥掉前缀，避免污染。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QKeySequence,
    QTextCharFormat,
    QTextCursor,
    QTextOption,
)
from PySide6.QtWidgets import QPlainTextEdit, QPushButton, QTextEdit, QWidget

from ..domain.models import LOG_KIND_EVENT, LogRecord, Span
from . import theme

TS_COLOR = "#5A6675"
MATCH_BG = "#4A3B12"
MATCH_CURRENT_BG = "#8A6B1E"
EVENT_COLOR = theme.SUBTEXT
MAX_BACKSTOP_BLOCKS = 3400


def _ts_prefix(ts: Optional[str]) -> str:
    if not ts:
        return "        "
    try:
        text = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone()
        return local.strftime("%H:%M:%S")
    except ValueError:
        return "        "


def _cp_offsets(text: str) -> List[int]:
    """返回每个 code point 索引对应的 UTF-16 偏移（长度 = len(text)+1）。"""
    offsets = [0]
    total = 0
    for ch in text:
        total += 2 if ord(ch) > 0xFFFF else 1
        offsets.append(total)
    return offsets


def _record_key(record: LogRecord) -> Tuple:
    """判定"同一条已提交行"的键。

    `provisional=False` 的行一经产生就不再变化，因此按内容比较即可判断文档里的
    那一块是否仍然对应快照里的这一条。
    """
    return (
        record.sequence,
        record.run_id,
        record.ts,
        record.kind,
        record.text,
        len(record.spans),
    )


class ServiceLogView(QPlainTextEdit):
    at_bottom_changed = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("LogView")
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.setFrameShape(QPlainTextEdit.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.DefaultContextMenu)
        self.document().setMaximumBlockCount(MAX_BACKSTOP_BLOCKS)

        self._generation = -1
        self._epoch = -1
        self._history_appended = 0
        self._evicted_seen = 0
        self._activity_count = 0
        self._at_bottom = True
        self._pending_new = 0
        self._matches: List[int] = []
        self._current_match = -1
        self._records: List[LogRecord] = []
        # 文档里**实际渲染**的记录序列（与 _records 区分：后者会被匹配导航替换成
        # 过滤后的候选集，而它始终如实反映视图内容，增量据此对齐）。
        self._rendered: List[LogRecord] = []
        self._error_filter = False
        self._line_numbers: List[int] = []  # 记录 index -> block number

        self._jump_button = QPushButton("↓ 回到底部", self)
        self._jump_button.setObjectName("PrimaryBtn")
        self._jump_button.clicked.connect(self._on_jump_clicked)
        self._jump_button.hide()

        bar = self.verticalScrollBar()
        bar.valueChanged.connect(self._on_scrolled)
        bar.rangeChanged.connect(lambda *_: self._reposition_button())

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def apply(
        self,
        *,
        records: Sequence[LogRecord],
        matches: Sequence[int],
        current_match: int,
        generation: int,
        evicted_total: int,
        epoch: int,
        full_rebuild: bool,
        auto_scroll: bool,
    ) -> None:
        self._matches = list(matches)
        self._current_match = current_match
        records = list(records)
        rebuild = full_rebuild or generation != self._generation or epoch != self._epoch
        self._epoch = epoch
        if rebuild:
            self._pending_generation = generation
            self._rebuild(records, evicted_total)
        else:
            delta = self._incremental(records, evicted_total)
            self.note_new_lines(delta)
        self._records = records
        self._render_numbering()
        self._apply_highlights()
        self._update_jump_button(auto_scroll)

    def reset_for_epoch(self) -> None:
        self._generation = -1
        self._history_appended = 0
        self._evicted_seen = 0
        self._activity_count = 0
        self._pending_new = 0
        self._rendered = []
        self.clear()
        self._jump_button.hide()

    def copy_all_body(self) -> str:
        """复制当前视图正文（剥掉时间戳前缀与自动换行）。"""
        return "\n".join(record.text or "" for record in self._records)

    @property
    def at_bottom(self) -> bool:
        return self._at_bottom

    # ------------------------------------------------------------------
    # 重建 / 增量
    # ------------------------------------------------------------------
    def _rebuild(self, records: Sequence[LogRecord], evicted_total: int = 0) -> None:
        self.setUpdatesEnabled(False)
        try:
            self.clear()
            self._history_appended = 0
            self._activity_count = 0
            self._generation = getattr(self, "_pending_generation", self._generation)
            cursor = QTextCursor(self.document())
            first = True
            for record in records:
                if record.provisional:
                    continue
                self._append_record(cursor, record, first)
                first = False
                self._history_appended += 1
            for record in records:
                if not record.provisional:
                    continue
                self._append_record(cursor, record, first)
                first = False
                self._activity_count += 1
        finally:
            self.setUpdatesEnabled(True)
        self._pending_new = 0
        # 文档此刻展示的正是快照的保留窗口：窗口之前被淘汰的行数必须如实记下来，
        # 否则下一次增量的 delta_evict 会被算成"从 0 到 evicted_total"，导致每帧
        # 都判成不一致而反复全量重建。
        self._evicted_seen = evicted_total
        self._rendered = list(records)

    def _incremental(self, records: List[LogRecord], evicted_total: int) -> int:
        """增量更新；返回本次新增的已提交行数。异常情况回退为全量重建。

        快照布局固定为「已提交历史在前、活动屏幕在后」，而活动屏幕（provisional）
        是唯一可变的部分：它可能被后一行替换，也可能在下次刷新时"落定"为历史。
        于是增量只做三件事：镜像顶部淘汰、保留文档中已经正确且不可变的前缀、把从
        第一个"可能已变化"的行起整段重绘。

        关键点：截断必须发生在**追加之前**。旧实现先追加新历史行、再按"历史行数"
        截断，截断点落在新追加的行上，上一轮的活动行于是残留在文档中段——表现为
        过期行与多出的空行（F06）。
        """
        prev = self._rendered
        delta_evict = max(0, evicted_total - self._evicted_seen)
        if delta_evict > len(prev):
            return self._rebuild_and_report(records, evicted_total)

        kept_prev = prev[delta_evict:]
        # 上一轮快照里"已经落定"的前缀：provisional=False 的行提交后不再变化。
        immutable = 0
        for record in kept_prev:
            if record.provisional:
                break
            immutable += 1

        if immutable > len(records):
            return self._rebuild_and_report(records, evicted_total)
        for i in range(immutable):
            if _record_key(kept_prev[i]) != _record_key(records[i]):
                # 已提交的行发生了变化：数据层与视图不再一致，整体重建。
                return self._rebuild_and_report(records, evicted_total)

        new_rows = records[immutable:]

        self.setUpdatesEnabled(False)
        try:
            if delta_evict:
                self._remove_top_blocks(delta_evict)
                self._evicted_seen += delta_evict
            # 尾部缩短（活动行被终端擦除、少一行）时同样必须截断：若只在 new_rows
            # 非空时截断，则"新快照只剩稳定前缀、活动尾部消失"这一路径不会删除旧活动
            # 行，UI 会残留过期文本（R05）。因此只要文档比应保留前缀更长就截断。
            if new_rows or self.document().blockCount() > immutable:
                self._truncate_to(immutable)
            if new_rows:
                cursor = QTextCursor(self.document())
                first = True
                for record in new_rows:
                    self._append_record(cursor, record, first)
                    first = False
        finally:
            self.setUpdatesEnabled(True)

        if abs(self.document().blockCount() - len(records)) > 1:
            # 计数漂移（例如块上限兜底触发）→ 自愈为全量重建
            return self._rebuild_and_report(records, evicted_total)

        self._rendered = list(records)
        self._history_appended = sum(1 for r in records if not r.provisional)
        self._activity_count = len(records) - self._history_appended
        settled = self._history_appended
        return max(0, settled - immutable)

    def _rebuild_and_report(self, records: Sequence[LogRecord], evicted_total: int) -> int:
        self._pending_generation = self._generation
        self._rebuild(records, evicted_total)
        return 0

    # ------------------------------------------------------------------
    # 块级操作
    # ------------------------------------------------------------------
    def _is_empty(self) -> bool:
        return self.document().isEmpty() or self.document().blockCount() == 0

    def _remove_top_blocks(self, count: int) -> None:
        doc = self.document()
        block = doc.findBlockByNumber(count)
        cursor = QTextCursor(doc)
        cursor.setPosition(0)
        if block.isValid():
            cursor.setPosition(block.position(), QTextCursor.KeepAnchor)
        else:
            cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()

    def _remove_from_block(self, block_number: int) -> None:
        doc = self.document()
        block = doc.findBlockByNumber(block_number)
        if not block.isValid():
            return
        cursor = QTextCursor(doc)
        cursor.setPosition(block.position())
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()

    def _truncate_to(self, keep: int) -> None:
        """把文档裁到只保留前 keep 个块；被裁掉的部分整体删除。

        截断后文档末尾会留下一个空块，调用方应当把下一条记录以 `empty=True`
        追加，直接续写该空块，避免多出一个空行。
        """
        doc = self.document()
        cursor = QTextCursor(doc)
        if keep <= 0:
            cursor.select(QTextCursor.Document)
            cursor.removeSelectedText()
            return
        block = doc.findBlockByNumber(keep)
        if block.isValid():
            cursor.setPosition(block.position())
        else:
            cursor.movePosition(QTextCursor.End)
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()

    def _append_record(self, cursor: QTextCursor, record: LogRecord, empty: bool) -> None:
        cursor.movePosition(QTextCursor.End)
        if not empty:
            cursor.insertBlock()
        prefix_fmt = QTextCharFormat()
        prefix_fmt.setForeground(QColor(TS_COLOR))
        cursor.insertText(_ts_prefix(record.ts) + " ", prefix_fmt)

        body = record.text or ""
        if record.kind == LOG_KIND_EVENT:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(EVENT_COLOR))
            cursor.insertText(body, fmt)
            return
        if not record.spans:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(theme.TEXT))
            cursor.insertText(body, fmt)
            return
        offsets = _cp_offsets(body)
        total = len(body)
        last = 0
        for span in record.spans:
            start = max(0, min(total, span.start))
            end = max(start, min(total, span.end))
            if start > last:
                plain = QTextCharFormat()
                plain.setForeground(QColor(theme.TEXT))
                cursor.insertText(body[last:start], plain)
            if end > start:
                cursor.insertText(body[start:end], _span_format(span))
            last = end
        if last < total:
            plain = QTextCharFormat()
            plain.setForeground(QColor(theme.TEXT))
            cursor.insertText(body[last:], plain)

    def _render_numbering(self) -> None:
        """记录每个视图行对应的块号，供匹配跳转使用。"""
        self._line_numbers = []
        number = 0
        for _ in self._records:
            self._line_numbers.append(number)
            number += 1
        if len(self._line_numbers) > self.document().blockCount():
            self._line_numbers = self._line_numbers[: self.document().blockCount()]

    # ------------------------------------------------------------------
    # 高亮与跳转
    # ------------------------------------------------------------------
    def _apply_highlights(self) -> None:
        selections: List[QTextEdit.ExtraSelection] = []
        if self._matches:
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(MATCH_BG))
            current_fmt = QTextCharFormat()
            current_fmt.setBackground(QColor(MATCH_CURRENT_BG))
            for order, record_index in enumerate(self._matches):
                if record_index < 0 or record_index >= len(self._line_numbers):
                    continue
                block_no = self._line_numbers[record_index]
                block = self.document().findBlockByNumber(block_no)
                if not block.isValid():
                    continue
                selection = QTextEdit.ExtraSelection()
                cursor = QTextCursor(block)
                cursor.select(QTextCursor.LineUnderCursor)
                selection.cursor = cursor
                selection.format = current_fmt if order == self._current_match else fmt
                selections.append(selection)
        self.setExtraSelections(selections)

    def scroll_to_match(self, record_index: int) -> None:
        if record_index < 0 or record_index >= len(self._line_numbers):
            return
        block = self.document().findBlockByNumber(self._line_numbers[record_index])
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        self.setTextCursor(cursor)
        self.centerCursor()

    # ------------------------------------------------------------------
    # 滚动
    # ------------------------------------------------------------------
    def _on_scrolled(self, _value: int) -> None:
        bar = self.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 2
        if at_bottom != self._at_bottom:
            self._at_bottom = at_bottom
            if at_bottom:
                self._pending_new = 0
            self.at_bottom_changed.emit(at_bottom)
        self._reposition_button()

    def _update_jump_button(self, auto_scroll: bool) -> None:
        if self._at_bottom:
            self._pending_new = 0
            self._jump_button.hide()
            if auto_scroll:
                self._scroll_to_bottom()
            return
        if auto_scroll:
            # 用户不在底部时 auto_scroll 不生效，交由用户手动回到底部
            pass
        text = "↓ 回到底部"
        if self._pending_new > 0:
            text = f"↓ 回到底部 · 新增 {self._pending_new} 行"
        self._jump_button.setText(text)
        self._jump_button.show()
        self._reposition_button()

    def note_new_lines(self, count: int) -> None:
        if count > 0 and not self._at_bottom:
            self._pending_new += count

    def _scroll_to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_jump_clicked(self) -> None:
        self._pending_new = 0
        self._scroll_to_bottom()
        self._at_bottom = True
        self._jump_button.hide()
        self.at_bottom_changed.emit(True)

    def _reposition_button(self) -> None:
        if not self._jump_button.isVisible():
            return
        hint = self._jump_button.sizeHint()
        x = max(8, self.viewport().width() - hint.width() - 18)
        y = max(8, self.viewport().height() - hint.height() - 14)
        self._jump_button.move(x, y)
        self._jump_button.raise_()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._reposition_button()

    # ------------------------------------------------------------------
    # 复制
    # ------------------------------------------------------------------
    def keyPressEvent(self, event):  # noqa: N802
        if event.matches(QKeySequence.Copy):
            cursor = self.textCursor()
            if cursor.hasSelection():
                super().keyPressEvent(event)
                return
        super().keyPressEvent(event)


def _span_format(span: Span) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fg = span.fg or theme.TEXT
    fmt.setForeground(QColor(fg))
    if span.bg:
        fmt.setBackground(QColor(span.bg))
    if span.bold:
        fmt.setFontWeight(700)
    if span.underline:
        fmt.setFontUnderline(True)
    return fmt
