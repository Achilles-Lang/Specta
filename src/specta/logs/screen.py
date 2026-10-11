"""pyte 适配层：ANSI 屏幕 -> 可渲染的文本行 + 样式跨度。

只有本模块（以及 pipeline）依赖 pyte。UI 与查询层看到的是纯文本 + Span。

Specta 的用法很窄：只把字节喂给一个 `HistoryScreen`，从 `history.top` 取"滚出屏幕
的历史行"，并把 `buffer[0..79]` 当作"活动屏幕"投影。因此 pyte 0.8.2 中有三处在
**这一用法下**成为热点或缺陷，这里都以"等价实现整体替换"的方式打平：

  1. `Screen.draw()` 在零宽但非 combining 的字符处 `break`，丢掉同块后续内容（正确性）；
  2. `HistoryScreen` 的逐次属性包裹与 before/after 事件钩子（性能，本用法下是空操作）；
  3. `index()` 每次滚屏逐行上移 79 行（性能）——改为 O(1) 的环形行窗。

三处都写明了依据与失效条件；一旦用法越界（使用分页视口、读取 cursor.hidden、
给屏幕手动设滚动边距、改变行数），必须回退到 pyte 原生实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import unicodedata

import pyte
from pyte import screens as _pyte_screens

from ..domain.models import Span

_PATCH_FLAG = "_specta_zero_width_patch"


def install_zero_width_patch() -> None:
    """修正 pyte 0.8.2 `Screen.draw()` 在「零宽但非 combining」字符处 break 的问题。

    上游实现里，`wcwidth(c) == 0` 且 `unicodedata.combining(c) == 0` 会走到
    `else: break`，从而**丢弃同一数据块剩下的全部字符**。变体选择符（VS15/VS16，
    U+FE0E/U+FE0F）正好属于这一类：`wcwidth("\\ufe0f") == 0` 而
    `unicodedata.combining("\\ufe0f") == 0`。

    实测影响：`温度 23.4℃ 🌡️ 正常` 被渲染成 `温度 23.4℃ 🌡`，尾部内容整体丢失
    （A13「emoji 跨块不破损」会失败）。

    终端的正确语义是：零宽字符不推进光标，并附着到前一格（与 combining 标记一致）。
    这里用等价实现整体替换 draw()（HistoryScreen 未覆写 draw，因此一并生效）。
    """
    if getattr(pyte.Screen.draw, _PATCH_FLAG, False):
        return

    _wcwidth = _pyte_screens.wcwidth
    mo = _pyte_screens.mo
    Char = _pyte_screens.Char

    def draw(self, data: str) -> None:
        data = data.translate(self.g1_charset if self.charset else self.g0_charset)
        if not data:
            return
        # 把可变的属性访问提到循环外：pyte 的 Screen 走的是带 set/property 的代理
        # 对象，逐字符反复访问是解析热点。mode 在一次 draw 调用内不会变
        # （模式切换由 Stream 单独分派）。
        mode = self.mode
        autowrap = mo.DECAWM in mode
        insert_mode = mo.IRM in mode
        columns = self.columns
        buffer = self.buffer
        cursor = self.cursor
        attrs = cursor.attrs
        # 逐字符 `attrs._replace(data=char)` 会走 namedtuple 的 map/_make 间接层，
        # 实测占解析链路约 15%；位置参数构造语义完全一致，快 2.3 倍。
        fg = attrs.fg
        bg = attrs.bg
        bold = attrs.bold
        italics = attrs.italics
        underscore = attrs.underscore
        strikethrough = attrs.strikethrough
        reverse = attrs.reverse
        blink = attrs.blink

        # 行对象按 cursor.y 缓存：buffer 是环形行窗（见 _RowWindow），逐字符
        # 取行会退化成每字符一次 Python 层 __getitem__；y 一般整段不变。
        row_y = -1
        line = None

        for char in data:
            char_width = _wcwidth(char)

            if cursor.x == columns:
                if autowrap:
                    self.dirty.add(cursor.y)
                    self.carriage_return()
                    self.linefeed()
                    # 自动折行触发底部滚屏时，cursor.y 仍是底行索引，但 buffer[y] 已经
                    # 指向新的物理行（旧行已滚入 history）。行缓存只按 y 失效会继续写
                    # 旧行 → 底部折行丢内容（R01）；这里强制让下一次取行重新解析。
                    row_y = -1
                elif char_width > 0:
                    cursor.x -= char_width

            if insert_mode and char_width > 0:
                self.insert_characters(char_width)

            y = cursor.y
            if y != row_y:
                row_y = y
                line = buffer[y]
            if char_width == 1:
                line[cursor.x] = Char(
                    char, fg, bg, bold, italics, underscore, strikethrough, reverse, blink
                )
            elif char_width == 2:
                line[cursor.x] = Char(
                    char, fg, bg, bold, italics, underscore, strikethrough, reverse, blink
                )
                if cursor.x + 1 < columns:
                    line[cursor.x + 1] = Char(
                        "", fg, bg, bold, italics, underscore, strikethrough, reverse, blink
                    )
            elif char_width == 0:
                # 零宽：附着到前一格（combining / VS15 / VS16 / ZWJ 等同理），不推进光标。
                if cursor.x:
                    last = line[cursor.x - 1]
                    line[cursor.x - 1] = last._replace(
                        data=unicodedata.normalize("NFC", last.data + char)
                    )
                elif cursor.y:
                    prev = buffer[cursor.y - 1]
                    last = prev[columns - 1]
                    prev[columns - 1] = last._replace(
                        data=unicodedata.normalize("NFC", last.data + char)
                    )
            else:
                # 不可打印字符：跳过该字符，但绝不丢弃后续字符。
                continue

            if char_width > 0:
                cursor.x += char_width
                if cursor.x > columns:
                    cursor.x = columns

        self.dirty.add(cursor.y)

    setattr(draw, _PATCH_FLAG, True)
    pyte.Screen.draw = draw


install_zero_width_patch()


# ---------------------------------------------------------------------------
# 环形行窗：把 `Screen.buffer` 从"按视觉行号索引的 dict"换成"固定 n 行、只转指针"
# ---------------------------------------------------------------------------
class _RowWindow:
    """pyte `Screen.buffer` 的等价环形实现。

    `buffer[y]` 映射到物理行槽 `rows[(head + y) % n]`。pyte 的 `Screen.index()` 每次
    滚屏都要 `for y in range(top, bottom): buffer[y] = buffer[y + 1]`——对逐行日志就是
    每行一次的 79 次字典读写，实测占整条解析链路约四分之一。滚屏在语义上就是"环移
    一行"：把 `head` 前移一格，并把即将成为新底行的那个物理槽重置为全新空行。于是
    单次滚屏从 O(行数) 降到 O(1)。

    `in` / `pop` 的语义按 pyte 的 `defaultdict` 逐点对齐：读一个不存在的键会**创建**
    它（因此 `in` 变 True），所以这里用 `present` 位图跟踪"已被创建"的物理槽。
    越界键走 `extra` 旁路字典，与原生 dict 行为一致。
    """

    __slots__ = ("_rows", "_n", "_head", "_factory", "_present", "_extra")

    def __init__(self, n: int, factory) -> None:
        self._n = n
        self._factory = factory
        self._head = 0
        self._rows = [factory() for _ in range(n)]
        self._present = bytearray(n)
        self._extra: Dict[int, object] = {}

    # -- 热路径 --------------------------------------------------------
    def __getitem__(self, key: int):
        if 0 <= key < self._n:
            slot = self._head + key
            if slot >= self._n:
                slot -= self._n
            self._present[slot] = 1
            return self._rows[slot]
        extra = self._extra
        row = extra.get(key)
        if row is None:
            row = self._factory()
            extra[key] = row
        return row

    def __setitem__(self, key: int, row) -> None:
        if 0 <= key < self._n:
            slot = self._head + key
            if slot >= self._n:
                slot -= self._n
            self._rows[slot] = row
            self._present[slot] = 1
        else:
            self._extra[key] = row

    def scroll_up(self) -> None:
        """上滚一行：旧顶行已由调用方交给 history，其物理槽复用为新底行。"""
        head = self._head
        self._rows[head] = self._factory()
        self._present[head] = 0
        self._head = head + 1 if head + 1 < self._n else 0

    def scroll_down(self) -> None:
        """下滚一行：旧底行已由调用方交给 history，其物理槽复用为新顶行。"""
        tail = self._head - 1 if self._head else self._n - 1
        self._rows[tail] = self._factory()
        self._present[tail] = 0
        self._head = tail

    # -- dict 兼容面（pyte 其余方法会用到）------------------------------
    def __contains__(self, key: int) -> bool:
        if 0 <= key < self._n:
            slot = self._head + key
            if slot >= self._n:
                slot -= self._n
            return bool(self._present[slot])
        return key in self._extra

    def __len__(self) -> int:
        return self._n

    def __iter__(self):
        return iter(self.keys())

    def keys(self):
        return list(range(self._n)) + list(self._extra)

    def values(self):
        # 原生 defaultdict 只迭代"已创建"的行；这里交出全部行。未创建的行等价于
        # 空行，pyte 的调用点（擦列 / 反显 / 缩列）对空行操作都是无副作用的。
        rows = self._rows
        head = self._head
        n = self._n
        return [rows[(head + y) % n] for y in range(n)] + list(self._extra.values())

    def items(self):
        return list(zip(self.keys(), self.values()))

    def pop(self, key: int, *default):
        if 0 <= key < self._n:
            slot = self._head + key
            if slot >= self._n:
                slot -= self._n
            if self._present[slot]:
                old = self._rows[slot]
                self._rows[slot] = self._factory()
                self._present[slot] = 0
                return old
            if default:
                return default[0]
            raise KeyError(key)
        if key in self._extra:
            return self._extra.pop(key)
        if default:
            return default[0]
        raise KeyError(key)

    def clear(self) -> None:
        factory = self._factory
        self._rows = [factory() for _ in range(self._n)]
        self._present = bytearray(self._n)
        self._extra = {}
        self._head = 0

    def to_dict(self):
        """物化成原生 `defaultdict`（视觉行号即键序），只在改行数时使用。"""
        from collections import defaultdict

        out = defaultdict(self._factory)
        rows, head, n = self._rows, self._head, self._n
        for y in range(n):
            slot = (head + y) % n
            if self._present[slot]:
                out[y] = rows[slot]
        for key, row in self._extra.items():
            out[key] = row
        return out

    @classmethod
    def from_mapping(cls, mapping, n: int, factory) -> "_RowWindow":
        win = cls.__new__(cls)
        win._n = n
        win._factory = factory
        win._head = 0
        rows = []
        present = bytearray(n)
        for y in range(n):
            if y in mapping:
                rows.append(mapping[y])
                present[y] = 1
            else:
                rows.append(factory())
        win._rows = rows
        win._present = present
        win._extra = {}
        return win


_ALL_ROWS: Dict[int, frozenset] = {}


def _all_rows(lines: int) -> frozenset:
    rows = _ALL_ROWS.get(lines)
    if rows is None:
        rows = frozenset(range(lines))
        _ALL_ROWS[lines] = rows
    return rows


def install_historyscreen_fast_path() -> None:
    """去掉 `HistoryScreen.__getattribute__` 与 before/after 事件钩子的开销。

    pyte 0.8.2 的 `HistoryScreen` 覆写了 `__getattribute__`：**每一次**属性访问都要过一层
    纯 Python 函数，命中 `_wrapped` 时还会现场构造 wrapper 闭包；而屏幕滚动（`index()`）
    对每一行日志都要读写 80 余次 `self.buffer` —— 实测该覆写占了整条解析链路约四成时间。

    进一步地，`_wrapped` 里的 before/after 钩子在 Specta 的使用方式下是**可证明的空操作**：

    * `before_event(event)` 的语义是“每次写屏前把视口拉回历史底部”
      （`while self.history.position < self.history.size: self.next_page()`）。
      而 Specta 从不调用 `prev_page()`，因此 `history.position` 恒等于 `history.size`
      （`__init__` 与 `_reset_history` 都把它置为 `size`），循环体永不执行。
    * `after_event(event)` 只做两件事：翻页时截断超宽行（同样只在翻页时发生）、
      以及同步 `cursor.hidden`。Specta 既不翻页，也不读取 `cursor.hidden`。

    失效条件：一旦 Specta 将来使用 pyte 的分页视口（`prev_page` / `next_page`）或读取
    `cursor.hidden`，必须移除此优化——它依赖“从不翻页、不读隐藏光标”这一不变式。
    日志历史仍由 `history.top` 正常维护（见 `pipeline._harvest`），不受影响。
    """
    hs = _pyte_screens.HistoryScreen
    if getattr(hs, "_specta_fast_path", False):
        return
    hs._wrapped = frozenset()
    hs.__getattribute__ = object.__getattribute__
    hs._specta_fast_path = True


install_historyscreen_fast_path()


def install_rotating_buffer() -> None:
    """把 `HistoryScreen` 的 `buffer` 换成 `_RowWindow`，并把滚屏改成 O(1)。

    仅当 `margins is None`（即整个屏幕都是滚动区）时走环移快路径；一旦程序设置了
    DECSTBM 滚动边距，或缓冲区不是 `_RowWindow`，就原样回落 pyte 的实现。

    `resize` 在行数变化时先物化成原生 dict、调用原实现、再重新装箱，保证不改变
    语义。Specta 自己从不调用 `resize`（列宽变化走新建屏幕），这里只是防御。
    """
    hs = _pyte_screens.HistoryScreen
    screen = _pyte_screens.Screen
    if getattr(hs, "_specta_rotating_buffer", False):
        return

    original_init = hs.__init__
    original_index = hs.index
    original_reverse_index = hs.reverse_index
    original_resize = screen.resize

    def __init__(self, columns: int, lines: int, history: int = 100, ratio: float = 0.5) -> None:
        original_init(self, columns, lines, history, ratio)
        # 复用 pyte 自己的行工厂（lambda: StaticDefaultDict(self.default_char)）
        factory = self.buffer.default_factory
        self.buffer = _RowWindow(self.lines, factory)

    def index(self) -> None:
        cursor = self.cursor
        window = self.buffer
        if self.margins is None and type(window) is _RowWindow:
            bottom = self.lines - 1
            if cursor.y == bottom:
                self.history.top.append(window[0])
                dirty = self.dirty
                if len(dirty) < self.lines:
                    dirty.update(_all_rows(self.lines))
                window.scroll_up()
            elif cursor.y < bottom:
                cursor.y += 1
            return
        original_index(self)

    def reverse_index(self) -> None:
        cursor = self.cursor
        window = self.buffer
        if self.margins is None and type(window) is _RowWindow:
            if cursor.y == 0:
                self.history.bottom.append(window[self.lines - 1])
                dirty = self.dirty
                if len(dirty) < self.lines:
                    dirty.update(_all_rows(self.lines))
                window.scroll_down()
            elif cursor.y > 0:
                cursor.y -= 1
            return
        original_reverse_index(self)

    def resize(self, lines=None, columns=None):
        new_lines = lines or self.lines
        new_columns = columns or self.columns
        if new_lines == self.lines and new_columns == self.columns:
            return
        window = self.buffer
        if type(window) is _RowWindow:
            self.buffer = window.to_dict()
        original_resize(self, lines, columns)
        if type(self.buffer) is not _RowWindow:
            factory = getattr(self.buffer, "default_factory", None) or window._factory
            self.buffer = _RowWindow.from_mapping(self.buffer, self.lines, factory)

    hs.__init__ = __init__
    hs.index = index
    hs.reverse_index = reverse_index
    screen.resize = resize
    hs._specta_rotating_buffer = True


install_rotating_buffer()

# 暗色可读 16 色表
ANSI_PALETTE: Dict[str, str] = {
    "black": "#484F58",
    "red": "#FF7B72",
    "green": "#7EE787",
    "brown": "#D29922",
    "yellow": "#D29922",
    "blue": "#79C0FF",
    "magenta": "#D2A8FF",
    "cyan": "#76E3EA",
    "white": "#B1BAC4",
    "brightblack": "#6E7681",
    "brightred": "#FFA198",
    "brightgreen": "#56D364",
    "brightbrown": "#E3B341",
    "brightyellow": "#E3B341",
    "brightblue": "#A5D6FF",
    "brightmagenta": "#E2C5FF",
    "brightcyan": "#B3F0FF",
    "brightwhite": "#F0F6FC",
}

DEFAULT_FG = "#E6EDF3"
DEFAULT_BG = "#0D1117"

# 默认样式单元格的"除 data 外的全部字段"，用于一行是否单一样式的一次性判定。
_PLAIN_TAIL = ("default", "default", False, False, False, False, False, False)


def color_to_hex(value: Optional[str], is_bg: bool = False) -> Optional[str]:
    """把 pyte 的颜色值转成 #RRGGBB；default/未知回退 None（表示用默认色）。"""
    if not value or value == "default":
        return None
    if value.startswith("#"):
        return value
    if len(value) == 6:
        try:
            int(value, 16)
        except ValueError:
            return None
        return "#" + value.lower()
    mapped = ANSI_PALETTE.get(value)
    if mapped:
        return mapped
    return None


@dataclass
class RenderedLine:
    text: str
    spans: List[Span] = field(default_factory=list)
    wrapped: bool = False  # 该行是从上一行自动折行而来（物理行，非逻辑行）


def render_line(row, columns: int) -> RenderedLine:
    """把 pyte 的一行缓冲（x -> Char）渲染成文本 + 跨度。

    宽字符（CJK / emoji）在 pyte 里占两个单元格，第二格是空占位。因此**列索引不等于
    文本的 code point 索引**；这里维护一张列 -> code point 的偏移表，span 一律以
    code point 为单位输出（规格 6.3）。

    性能：pyte 的行是 StaticDefaultDict，未写入的列不会占键，因此 `max(row)` 就是
    最后一个有内容的列；旧实现固定遍历全部 120 列，对绝大多数短日志行做了十倍以上的
    无用功。此外日志行绝大多数是"整行默认样式"，这里为它单开一条快路径：一趟扫描
    顺便判样式是否统一，命中则直接产出单 span，省掉逐列样式元组与二次比较。
    """
    try:
        last = max(row)
    except ValueError:  # 空行：没有任何写入过的列
        return RenderedLine(text="")
    end_cols = min(columns, last + 1)
    if end_cols <= 0:
        return RenderedLine(text="")

    parts, plain, last_sig = _scan_plain(row, end_cols)
    if plain:
        if last_sig < 0:
            return RenderedLine(text="")
        end = last_sig + 1
        text = "".join(parts[:end])
        return RenderedLine(
            text=text,
            spans=[Span(0, len(text), None, None, False, False)],
            wrapped=end >= columns and columns > 0,
        )
    return _render_styled(row, end_cols, columns)


def _scan_plain(row, end_cols: int):
    """一趟扫描：收集单元格文本，判断整行是否为默认样式，并记录最后一个非空白列。

    样式判定用 `char[1:]` 与默认"尾字段"元组整体比较（namedtuple 的切片比较走 C 层
    元组比较），比逐个属性取 8 次再判断快约 25%。它把 italics/strikethrough/blink
    也算进"非默认"，比必要更严格——这些强调不在 Span 里，走完整路径结果相同。
    """
    parts: List[str] = []
    plain = True
    last_sig = -1
    for x in range(end_cols):
        char = row[x]
        data = char.data
        parts.append(data)
        if data != " ":
            last_sig = x
        if plain and char[1:] != _PLAIN_TAIL:
            plain = False
    return parts, plain, last_sig


def _render_styled(row, end_cols: int, columns: int) -> RenderedLine:
    """带样式行的完整渲染（彩色 / 加粗 / 反转 / 下划线）。"""
    chars: List[str] = []
    styles: List[Tuple[Optional[str], Optional[str], bool, bool]] = []
    offsets: List[int] = [0]  # 列索引 -> 该列之前累计的 code point 数
    total = 0
    for x in range(end_cols):
        char = row[x]
        data = char.data
        chars.append(data)
        total += len(data)
        offsets.append(total)
        fg = color_to_hex(char.fg)
        bg = color_to_hex(char.bg, is_bg=True)
        bold = bool(char.bold)
        underline = bool(char.underscore)
        if char.reverse:
            fg, bg = bg, fg
            if fg is None and bg is None:
                fg, bg = DEFAULT_BG, DEFAULT_FG
        styles.append((fg, bg, bold, underline))

    # 去掉尾部空白（默认样式）
    end = end_cols
    while end > 0:
        idx = end - 1
        fg, bg, bold, underline = styles[idx]
        if chars[idx] == " " and fg is None and bg is None and not bold and not underline:
            end -= 1
        else:
            break

    if end <= 0:
        return RenderedLine(text="")

    text = "".join(chars[:end])
    spans: List[Span] = []
    start = 0
    cur = styles[0]
    for idx in range(1, end):
        if styles[idx] != cur:
            spans.append(_span(offsets[start], offsets[idx], cur))
            start = idx
            cur = styles[idx]
    spans.append(_span(offsets[start], offsets[end], cur))
    wrapped = end >= columns and columns > 0
    return RenderedLine(text=text, spans=spans, wrapped=wrapped)


def _span(start: int, end: int, style: Tuple[Optional[str], Optional[str], bool, bool]) -> Span:
    fg, bg, bold, underline = style
    return Span(start=start, end=end, fg=fg, bg=bg, bold=bold, underline=underline)


def strip_ansi(text: str) -> str:
    """移除 ANSI 转义序列，供写入诊断或纯文本导出的场景。"""
    import re

    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]", "", text)


def new_screen(columns: int, rows: int, history: int) -> pyte.HistoryScreen:
    return pyte.HistoryScreen(columns, rows, history=history)


def new_stream(screen: "pyte.HistoryScreen") -> pyte.Stream:
    return pyte.Stream(screen)


__all__ = [
    "RenderedLine",
    "render_line",
    "new_screen",
    "new_stream",
    "color_to_hex",
    "strip_ansi",
    "install_zero_width_patch",
    "install_historyscreen_fast_path",
    "install_rotating_buffer",
    "ANSI_PALETTE",
    "DEFAULT_FG",
    "DEFAULT_BG",
]
