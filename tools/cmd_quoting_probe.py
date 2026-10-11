"""shell 模式命令行引号保真探针（F15）。

对照两种构造在真实 `cmd` 下的行为：

  * 旧构造：``[COMSPEC, "/d", "/s", "/c", command]`` 走 ``subprocess`` 列表拼装 ——
    ``list2cmdline`` 把元素内嵌的 ``"`` 转义成 ``\\"``（cmd 不认），叠加 ``cmd /s``
    「去首引号 + 删最后一个引号」的处理，引号与带空格路径都会被破坏；
  * 现实现：``specta.launch_worker._shell_command_line`` —— 整条命令行以字符串交给
    CreateProcess，且仅当命令以引号开头时多包一层引号。

用法：
    .venv\\Scripts\\python tools/cmd_quoting_probe.py

输出各条命令的「命令行文本对照」与「真实 exit code / 首行输出」，末尾给出合计通过数。
仅 Windows 有效。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from specta.launch_worker import _shell_command_line  # noqa: E402

COMSPEC = os.environ.get("COMSPEC", "cmd.exe")


def _make_helper(work: Path) -> Path:
    """在带空格的目录里放一个回显自身参数的 .cmd，用于验证路径与 argv 保真。"""
    space_dir = work / "with space"
    space_dir.mkdir(parents=True, exist_ok=True)
    helper = space_dir / "argv.cmd"
    helper.write_text("@echo off\r\necho RAWALL[%*]\r\n", encoding="ascii")
    return helper


def _exec(args):
    try:
        proc = subprocess.run(args, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    except Exception as exc:  # noqa: BLE001
        return None, f"<EXC {type(exc).__name__}: {exc}>"
    return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()


def legacy_command_line(command: str) -> str:
    """修复前的构造（保留以复现缺陷，勿在新代码里使用）。"""
    return subprocess.list2cmdline([COMSPEC, "/d", "/s", "/c", command])


def current_command_line(command: str) -> str:
    return _shell_command_line(command, COMSPEC)


def main() -> int:
    cases = []
    with tempfile.TemporaryDirectory(prefix="specta-cmdprobe-") as tmp:
        helper = _make_helper(Path(tmp))
        helper_q = f'"{helper}"'
        cases = [
            ("简单命令", "echo SIMPLE", ["SIMPLE"]),
            ("&& 串联", "echo A=1&&echo B=2", ["A=1", "B=2"]),
            ("set 环境变量&&", "set JVM=-Xmx1024m&&set JVM", ["JVM=-Xmx1024m"]),
            ("值带引号（非行首）", 'echo ARG="-Xmx1024m"', ['ARG="-Xmx1024m"']),
            ("引号路径+引号参数", f'{helper_q} "p q" -Dk="v v"', ['RAWALL["p q" -Dk="v v"]']),
            ("引号路径+单个引号参数", f'{helper_q} "-Xmx1024m"', ['RAWALL["-Xmx1024m"]']),
            ("管道", "echo PIPE | findstr PIPE", ["PIPE"]),
            ("%VAR% 展开", "echo C=%COMSPEC%", [COMSPEC]),
            ("括号块 if", "if not defined NOPE (echo NO-NOPE)", ["NO-NOPE"]),
            ("现场原案（mvn 带引号参数）", 'mvn spring-boot:run "-Dspring-boot.run.jvmArguments=-Xmx1024m"', ["此时不应有"]),
        ]

        print("=" * 96)
        print("命令行文本对照（旧 → 新）")
        for name, command, _ in cases:
            old = legacy_command_line(command)
            new = current_command_line(command)
            flag = "  " if old == new else "* "
            print(f"{flag}[{name}]\n     old: {old}\n     new: {new}")

        print()
        print("=" * 96)
        tally = {"old": 0, "new": 0}
        total = 0
        for name, command, token in cases:
            # 「现场原案」是负向用例：期望**不出现**该 token
            negative = name.startswith("现场原案")
            old_ok = True
            new_ok = True
            for label, line, key in (("old", legacy_command_line(command), "old"),
                                     ("new", current_command_line(command), "new")):
                code, out = _exec(line)
                present = token[0] in out
                # 负向用例：既不得出现解析错误文本，也不得是 cmd 解析失败的 255
                ok = ((not present) and code != 255) if negative else present
                if key == "old":
                    old_ok = ok
                    old_ret = (code, out)
                else:
                    new_ok = ok
                    new_ret = (code, out)
            total += 1
            tally["old"] += 1 if old_ok else 0
            tally["new"] += 1 if new_ok else 0
            print(f"[{'PASS' if new_ok else 'FAIL'}] {name}   期望{'不出现' if negative else '出现'} {token}")
            print(f"      old exit={old_ret[0]:<4} {'OK ' if old_ok else 'BAD'} {old_ret[1].splitlines()[0] if old_ret[1] else ''}")
            print(f"      new exit={new_ret[0]:<4} {'OK ' if new_ok else 'BAD'} {new_ret[1].splitlines()[0] if new_ret[1] else ''}")

    print()
    print("=" * 96)
    print(f"合计  旧 = {tally['old']}/{total}    新 = {tally['new']}/{total}")
    return 0 if tally["new"] == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
