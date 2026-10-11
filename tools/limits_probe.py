"""A15/A25 实测：单服务日志管道的持续负载上限与内存有界性。

用法：
    .venv\\Scripts\\python tools/limits_probe.py

向一个 LogPipeline 连续灌入大量行，校验输出历史 / 事件 / 活动屏幕 / 内容预算 /
原始字节层各自守住上限，并观察进程 RSS 是否随输入量增长。
"""

import gc
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import psutil  # noqa: E402

from specta.logs.pipeline import (  # noqa: E402
    CONTENT_BUDGET,
    MAX_EVENT_ROWS,
    MAX_RETAINED_ROWS,
    RAW_LIMIT,
    SCREEN_ROWS,
    LogPipeline,
)

TOTAL_LINES = 400_000
BATCH = 2000

proc = psutil.Process()


def rss_mb() -> float:
    gc.collect()
    return proc.memory_info().rss / 1024 / 1024


pipe = LogPipeline("limits", encoding="utf-8", columns=120)
base = rss_mb()
print(f"baseline RSS = {base:.1f} MB")

line = (
    "2026-10-11 17:00:00 INFO [worker-7] 处理中 "
    "订单号=ORD-2026-10-11000000 状态=OK 耗时=12ms 中文负载填充 1234567890\n"
).encode("utf-8")
line_bytes = len(line)

t0 = time.perf_counter()
sent = 0
peak = base
while sent < TOTAL_LINES:
    step = min(BATCH, TOTAL_LINES - sent)
    pipe.feed_output(line * step, "2026-10-11T17:00:00", run_id="run-load")
    sent += step
    if sent % 50_000 == 0:
        m = rss_mb()
        peak = max(peak, m)
        print(
            f"  fed {sent:>7} lines | RSS={m:6.1f} MB | out={len(pipe._output):>5} "
            f"ev={len(pipe._events):>4} act={len(pipe._activity):>3} "
            f"| raw={pipe._raw_bytes()/1024/1024:.2f} MiB "
            f"| pending={len(pipe._pending)/1024/1024:.2f} MiB"
        )

pipe.drain(max_seconds=30)
elapsed = time.perf_counter() - t0
after = rss_mb()
snap = pipe.snapshot()

print("\n=== 结果 ===")
print(f"总输入        : {TOTAL_LINES} 行 / {TOTAL_LINES*line_bytes/1024/1024:.1f} MiB 原始字节")
print(f"耗时          : {elapsed:.2f} s ({TOTAL_LINES/elapsed:,.0f} 行/s)")
print(f"RSS           : base={base:.1f} -> after={after:.1f} MB (峰值 {peak:.1f} MB, 增长 {after-base:.1f} MB)")
print(f"输出历史行    : {len(pipe._output)} (上限 {MAX_RETAINED_ROWS})")
print(f"事件行        : {len(pipe._events)} (上限 {MAX_EVENT_ROWS})")
print(f"活动屏幕行    : {len(pipe._activity)} (上限 {SCREEN_ROWS})")
print(f"快照总行      : {snap.retained_rows}")
print(f"内容字节      : {pipe._content_bytes/1024/1024:.2f} MiB (预算 {CONTENT_BUDGET/1024/1024:.0f} MiB)")
print(f"原始字节层    : {pipe._raw_bytes()/1024/1024:.2f} MiB (上限 {RAW_LIMIT/1024/1024:.0f} MiB)")
print(f"待解析        : {len(pipe._pending)} bytes")
print(f"capture_loss  : {pipe._capture_loss}")
print(f"history_evicted: {snap.history_evicted}")

ok = True
if len(pipe._output) > MAX_RETAINED_ROWS:
    print("FAIL: 输出历史行数超上限"); ok = False
if len(pipe._events) > MAX_EVENT_ROWS:
    print("FAIL: 事件行数超上限"); ok = False
if len(pipe._activity) > SCREEN_ROWS:
    print("FAIL: 活动屏幕行数超上限"); ok = False
if pipe._content_bytes > CONTENT_BUDGET:
    print("FAIL: 内容字节超预算"); ok = False
if pipe._raw_bytes() > RAW_LIMIT + line_bytes:
    print("FAIL: 原始字节超上限"); ok = False
if len(pipe._pending) != 0:
    print("FAIL: drain 后仍有待解析数据"); ok = False
if (after - base) > 120:
    print(f"FAIL: 内存增长过大 {after-base:.1f} MB"); ok = False

print("\nLIMITS", "OK" if ok else "FAIL")
sys.exit(0 if ok else 1)
