"""诊断日志：轮转 3 份 × 1MiB，写入 %LOCALAPPDATA%\\Specta\\diagnostics\\specta.log。

不记录 env 值、token 或服务输出正文（服务输出默认只在内存）。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from .settings import diagnostics_dir

LOGGER_NAME = "specta"
MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 3


def setup_logging(level: int = logging.INFO, path: Optional[Path] = None) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    target = path or (diagnostics_dir() / "specta.log")
    try:
        handler = RotatingFileHandler(
            str(target), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(handler)
    except OSError:
        # 无法写文件时退化为无操作，不能因此影响功能
        logger.addHandler(logging.NullHandler())
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def diagnostic_path() -> Path:
    return diagnostics_dir() / "specta.log"
