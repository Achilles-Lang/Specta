"""以 offscreen 平台渲染真实 UI 并截图（M4 交付物；无需显示器）。

用法：
    .venv\\Scripts\\python tools/capture_screenshots.py

输出到 docs/screenshots/。注意：offscreen 平台不会自动扫描系统字体，脚本会显式注册
Windows 字体，否则中文会渲染成豆腐块。
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
FIX = ROOT / "tests" / "fixtures"
TMP = ROOT / ".specta-shots"
OUT = ROOT / "docs" / "screenshots"

shutil.rmtree(TMP, ignore_errors=True)
(TMP / "Specta").mkdir(parents=True)
OUT.mkdir(parents=True, exist_ok=True)
os.environ["LOCALAPPDATA"] = str(TMP)
os.environ["PYTHONPATH"] = str(SRC)
os.environ.pop("SPECTA_NO_UI", None)
sys.path.insert(0, str(SRC))

SVC = FIX / "logging_service.py"
seed = {
    "schema_version": 1,
    "services": [
        {
            "service_id": "web-dev", "name": "Web Dev Server", "origin": "user",
            "cwd": str(FIX), "ports": [18080], "encoding": "utf-8",
            "launch": {"mode": "exec", "program": sys.executable,
                       "args": ["-u", str(SVC), "--port", "18080", "--loops", "600", "--interval", "0.5"]},
            "ready_when": {"port": 18080, "log_regex": "ready in 123 ms"},
        },
        {
            "service_id": "worker", "name": "Background Worker", "origin": "agent",
            "cwd": str(FIX), "ports": [], "encoding": "utf-8",
            "launch": {"mode": "exec", "program": sys.executable,
                       "args": ["-u", str(SVC), "--rewrite", "--loops", "600", "--interval", "0.5"]},
        },
    ],
}
(TMP / "Specta" / "services.json").write_text(
    json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8"
)

from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from specta.daemon_main import build_runtime  # noqa: E402
from specta.domain.models import ServiceSpec  # noqa: E402
from specta.ui.service_editor import ServiceEditorDialog  # noqa: E402
from specta.ui.settings_dialog import SettingsDialog  # noqa: E402

app = QApplication([])

for candidate in (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\consola.ttf",
):
    if os.path.exists(candidate):
        font_id = QFontDatabase.addApplicationFont(candidate)
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        print(f"  font {os.path.basename(candidate)} -> {families}")

runtime = build_runtime(app, with_ui=True)
window = runtime.window
window.resize(1200, 780)
window.show()


def pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def shot(widget, name: str) -> None:
    pix = widget.grab()
    path = OUT / name
    ok = pix.save(str(path))
    print(f"  saved {name}: {ok} ({pix.width()}x{pix.height()})")


pump(0.6)
for sid in ("web-dev", "worker"):
    try:
        runtime.manager.submit_start(sid, None, source="ui")
    except Exception as exc:  # noqa: BLE001
        print("start error", sid, exc)
pump(6.0)

print("screenshots:")
shot(window, "01-main-window.png")

spec = ServiceSpec.from_dict(seed["services"][0])
editor = ServiceEditorDialog(existing=spec, parent=window)
editor.show()
pump(0.8)
shot(editor, "02-service-editor.png")
editor.close()

settings_dlg = SettingsDialog(runtime.settings, parent=window)
settings_dlg.show()
pump(0.8)
shot(settings_dlg, "03-settings.png")
settings_dlg.close()

try:
    window._open_service("worker")  # type: ignore[attr-defined]
except Exception:
    pass
pump(1.5)
shot(window, "04-second-service.png")

runtime.shutdown()
print("done")
