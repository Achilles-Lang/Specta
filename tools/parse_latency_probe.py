r"""F04 实测：真实 ManagedService 路径下，突发输出后解析积压能否清空。

用法：
    .venv\Scripts\python tools/parse_latency_probe.py

口径（审查报告 §4.3 要求分别报告）：
  - 输入字节 / 输入行数        ：本轮注入的原始数据
  - 尾部 marker 到达耗时        ：从注入结束到 marker 出现在快照，仅靠 Qt 事件循环推进
  - 结束时刻待解析字节          ：300ms（或 marker 到达）后 pipeline._pending 的残留
  - 丢弃字节 / capture_loss     ：过载降级是否被触发
  - 纯解析吞吐（参考）          ：裸 LogPipeline + drain() 的 rows/s（不含 UI/事件循环开销）

与旧版 limits_probe 的区别：不再在结束时手动 drain()，从而暴露真实服务路径的积压；
这里验证的是「产品源码的 QTimer 分片解析」是否真的把 pending 排空。
"""

import gc
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import psutil  # noqa: E402
from PySide6.QtCore import QEventLoop, QElapsedTimer, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from specta.application.managed_service import ManagedService  # noqa: E402
from specta.domain.models import ServiceSpec  # noqa: E402
from specta.domain.protocol import worker_output  # noqa: E402
from specta.logs.pipeline import LogPipeline  # noqa: E402

BURST_LINES = 22_000
LINE = b"burst_line\n"
MARKER = b"FINAL_BURST_MARKER\n"
BLOCK = 32768
DEADLINE_MS = 300

app = QApplication.instance() or QApplication([])
proc = psutil.Process()


def rss_mb() -> float:
    gc.collect()
    return proc.memory_info().rss / 1024 / 1024


def spec(tmp: Path) -> ServiceSpec:
    return ServiceSpec.from_dict(
        {
            "service_id": "probe",
            "name": "probe",
            "launch": {"mode": "exec", "program": sys.executable, "args": []},
            "cwd": str(tmp),
            "origin": "agent",
        }
    )


def pump_until_marker(service: ManagedService, deadline_ms: int):
    """只让 Qt 事件循环推进，按 10ms 轮询快照，记录 marker 到达耗时。"""
    timer = QElapsedTimer()
    timer.start()
    loop = QEventLoop()
    QTimer.singleShot(deadline_ms, loop.quit)
    state = {"found": False, "at_ms": None}

    def poll():
        if state["found"]:
            return
        texts = [r.text for r in service.log_snapshot().records]
        if any("FINAL_BURST_MARKER" in t for t in texts):
            state["found"] = True
            state["at_ms"] = timer.elapsed()
            loop.quit()

    ticker = QTimer()
    ticker.timeout.connect(poll)
    ticker.start(10)
    loop.exec()
    ticker.stop()
    if state["at_ms"] is None:
        state["at_ms"] = timer.elapsed()
    return state


def run_managed(tmp: Path):
    service = ManagedService(spec(tmp))
    service.facts.run_id = "run"
    data = LINE * BURST_LINES + MARKER
    rss_before = rss_mb()
    t0 = time.perf_counter()
    for off in range(0, len(data), BLOCK):
        service._handle_worker_frame(worker_output("run", data[off : off + BLOCK]))
    feed_ms = (time.perf_counter() - t0) * 1000
    pending_after_feed = len(service.pipeline._pending)
    rss_peak = rss_mb()

    state = pump_until_marker(service, DEADLINE_MS)
    pending_at_end = len(service.pipeline._pending)
    snap = service.log_snapshot()

    print("== 真实 ManagedService 路径 ==")
    print(f"输入字节          : {len(data)}")
    print(f"输入行数          : {BURST_LINES + 1}")
    print(f"注入耗时          : {feed_ms:.1f} ms")
    print(f"注入结束待解析    : {pending_after_feed} B")
    print(f"marker 到达       : {state['found']} @ {state['at_ms']} ms（预算 {DEADLINE_MS} ms）")
    print(f"结束待解析        : {pending_at_end} B")
    print(f"capture_loss      : {snap.capture_loss}")
    print(f"history_evicted   : {snap.history_evicted}")
    print(f"保留输出行        : {len(snap.records)}")
    print(f"RSS 注入前/注入后 : {rss_before:.1f} / {rss_peak:.1f} MB")
    return state["found"], state["at_ms"], pending_at_end


def run_bare():
    """裸 LogPipeline 纯解析吞吐（参考值，不含事件循环/UI 开销）。"""
    total_lines = 400_000
    p = LogPipeline("bare", columns=120)
    p.set_run("run", "t0")
    payload = LINE * 2000
    t0 = time.perf_counter()
    for _ in range(total_lines // 2000):
        p.feed_output(payload, "t", "run")
        p.drain()
    elapsed = time.perf_counter() - t0
    print()
    print("== 裸 LogPipeline 纯解析（参考） ==")
    print(f"行数              : {total_lines}")
    print(f"耗时              : {elapsed:.2f} s")
    print(f"吞吐              : {total_lines / elapsed:,.0f} rows/s")


if __name__ == "__main__":
    run_bare()
    found, at_ms, pending = run_managed(Path.cwd() / ".rv-probe-work")
    print()
    print(f"PASS={found}，尾部残留={pending} B，marker 耗时={at_ms} ms")
