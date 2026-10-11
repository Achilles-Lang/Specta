"""应用目录、配置路径与原子 JSON 读写。

目录布局（规格 12）：

    %LOCALAPPDATA%\\Specta\\
      daemon.lock
      daemon.json
      services.json
      ui.json
      diagnostics\\specta.log
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Optional

from ..domain.errors import ErrorCode, SpectaError

APP_DIR_NAME = "Specta"

DEFAULT_FONT_SIZE = 13
MIN_FONT_SIZE = 11
MAX_FONT_SIZE = 18
DEFAULT_CONTEXT_LINES = 8
MIN_CONTEXT_LINES = 0
MAX_CONTEXT_LINES = 30

CLOSE_BEHAVIOR_TRAY = "tray"
CLOSE_BEHAVIOR_EXIT = "exit"


def app_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        path = Path(base) / APP_DIR_NAME
    else:  # pragma: no cover - 非 Windows 兜底
        path = Path.home() / f".{APP_DIR_NAME.lower()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def daemon_json_path() -> Path:
    return app_dir() / "daemon.json"


def services_json_path() -> Path:
    return app_dir() / "services.json"


def ui_json_path() -> Path:
    return app_dir() / "ui.json"


def daemon_lock_path() -> Path:
    return app_dir() / "daemon.lock"


def diagnostics_dir() -> Path:
    path = app_dir() / "diagnostics"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# 原子读写
# ---------------------------------------------------------------------------


def read_json(path: Path) -> Optional[Any]:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return None
    return json.loads(text)


def atomic_write_json(path: Path, payload: Any) -> None:
    """同目录临时文件 -> flush+fsync -> 原子替换。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception as exc:  # noqa: BLE001
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise SpectaError(
            ErrorCode.SAVE_FAILED, f"写入 {path.name} 失败: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# UI 设置
# ---------------------------------------------------------------------------


@dataclass
class AppSettings:
    font_size: int = DEFAULT_FONT_SIZE
    context_lines: int = DEFAULT_CONTEXT_LINES
    close_behavior: str = CLOSE_BEHAVIOR_TRAY
    current_service_id: Optional[str] = None
    window_geometry: str = ""
    window_state: str = ""
    tray_hint_shown: bool = False

    def normalized(self) -> "AppSettings":
        data = asdict(self)
        data["font_size"] = _clamp_int(
            data["font_size"], MIN_FONT_SIZE, MAX_FONT_SIZE, DEFAULT_FONT_SIZE
        )
        data["context_lines"] = _clamp_int(
            data["context_lines"], MIN_CONTEXT_LINES, MAX_CONTEXT_LINES, DEFAULT_CONTEXT_LINES
        )
        if data["close_behavior"] not in (CLOSE_BEHAVIOR_TRAY, CLOSE_BEHAVIOR_EXIT):
            data["close_behavior"] = CLOSE_BEHAVIOR_TRAY
        return AppSettings(**data)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: Any) -> "AppSettings":
        if not isinstance(data, dict):
            return AppSettings()
        known = {f: data.get(f) for f in AppSettings().__dataclass_fields__}
        settings = AppSettings(**{k: v for k, v in known.items() if v is not None or k in ("current_service_id", "window_geometry", "window_state")})
        return settings.normalized()


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, ivalue))


def load_settings() -> AppSettings:
    try:
        raw = read_json(ui_json_path())
    except (OSError, json.JSONDecodeError):
        return AppSettings()
    return AppSettings.from_dict(raw)


def save_settings(settings: AppSettings) -> None:
    atomic_write_json(ui_json_path(), settings.normalized().to_dict())
