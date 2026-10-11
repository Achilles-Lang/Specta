"""实测：Daemon 冷启动延迟（Popen -> endpoint 就绪 -> 首次 ping 成功）。

用法：
    .venv\\Scripts\\python tools/startup_probe.py
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

TMP = ROOT / ".specta-startup-tmp"


def run_once(idx: int):
    app_dir = TMP / f"run{idx}"
    app_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(app_dir)
    env["SPECTA_NO_UI"] = "1"
    env["PYTHONPATH"] = str(SRC)

    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, "-m", "specta.daemon_main"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=str(ROOT),
    )
    endpoint = app_dir / "Specta" / "daemon.json"
    t_ready = None
    while time.perf_counter() - t0 < 40:
        if endpoint.exists():
            try:
                json.loads(endpoint.read_text(encoding="utf-8"))
                t_ready = time.perf_counter() - t0
                break
            except Exception:
                pass
        time.sleep(0.02)
    if t_ready is None:
        proc.kill()
        return None, None

    prev = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = str(app_dir)
    os.environ["PYTHONPATH"] = str(SRC)
    try:
        from specta.infrastructure.daemon_client import DaemonClient

        async def ping():
            c = DaemonClient(session_id=f"probe{idx}")
            try:
                t = time.perf_counter()
                await c.call("ping", {}, timeout=5.0)
                return (time.perf_counter() - t) * 1000
            finally:
                await c.close()

        ping_ms = asyncio.run(ping())
    finally:
        if prev is not None:
            os.environ["LOCALAPPDATA"] = prev

    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    return t_ready, ping_ms


shutil.rmtree(TMP, ignore_errors=True)

print("Daemon 冷启动延迟（含 Python 解释器启动）:")
readies = []
for i in range(3):
    r, p = run_once(i)
    if r is None:
        print(f"  run{i}: 未就绪")
        continue
    readies.append(r)
    print(f"  run{i}: endpoint 就绪 {r*1000:7.0f} ms | 首次 ping {p:6.1f} ms")
if readies:
    print(
        f"\n就绪中位: {sorted(readies)[len(readies)//2]*1000:.0f} ms  "
        f"最小 {min(readies)*1000:.0f} ms  最大 {max(readies)*1000:.0f} ms"
    )

shutil.rmtree(TMP, ignore_errors=True)
