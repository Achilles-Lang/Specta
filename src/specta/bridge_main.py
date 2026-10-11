"""MCP stdio 桥入口：python -m specta.bridge_main

官方 SDK 负责 stdio 协议；本进程只做三件事：
  * 发现 Daemon（读 %LOCALAPPDATA%\\Specta\\daemon.json），必要时脱离桥生命周期自启；
  * 把 MCP 工具调用转发给 Daemon（IPC），并把操作等待到完成；
  * 把结果按 {ok, data, error} 结构化返回。

硬约束：stdout 是 MCP 协议通道。本进程绝不向 stdout 打印任何内容；所有日志走 stderr。
"""

from __future__ import annotations

import asyncio
import logging
import sys

from .bridge.tools import build_server
from .infrastructure.daemon_client import DaemonClient


def _configure_logging() -> None:
    """日志一律写到 stderr，避免污染 stdout 上的 MCP 协议流。"""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.WARNING)


async def _amain() -> int:
    client = DaemonClient()
    server = build_server(client)
    try:
        await server.run_stdio_async()
    finally:
        await client.close()
    return 0


def main(argv=None) -> int:
    _configure_logging()
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:  # pragma: no cover
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
