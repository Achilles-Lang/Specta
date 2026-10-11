"""日志导出（第一版 UTF-8 纯文本）。"""

from __future__ import annotations

from typing import Sequence

from ..domain.models import LOG_KIND_EVENT, LogRecord

EVENT_PREFIX = "[Specta]"


def record_line(record: LogRecord) -> str:
    return record.text or ""


def build_export_text(
    *,
    service_id: str,
    name: str,
    cwd: str,
    run_id: str,
    exported_at: str,
    filtered: bool,
    records: Sequence[LogRecord],
) -> str:
    header = [
        f"# Specta log export",
        f"# service_id: {service_id}",
        f"# name: {name}",
        f"# cwd: {cwd}",
        f"# run_id: {run_id}",
        f"# exported_at: {exported_at}",
        f"# filtered: {'yes' if filtered else 'no'}",
        f"# lines: {len(records)}",
        "#",
    ]
    body = [record_line(r) for r in records]
    return "\n".join(header + body) + "\n"


def default_export_filename(service_id: str, stamp: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in service_id)
    return f"Specta_{safe}_{stamp}.log"
