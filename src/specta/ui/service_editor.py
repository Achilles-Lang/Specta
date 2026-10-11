"""新增 / 编辑服务对话框。

宽度 640px，字段纵向排列；高级区折叠（环境变量、编码、就绪观察条件、持久化说明）。
两种启动方式互斥：命令行（cmd /d /s /c）与 程序 + 参数（不经 shell）。

保存前做完整校验：ID 规则、名称长度、cwd 必须为存在的绝对目录、端口范围与去重、
ready_when.port 必须包含在 ports 中。校验失败在对话框内就地提示，不弹独立错误框。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..domain.errors import SpectaError
from ..domain.models import (
    ID_PATTERN,
    LAUNCH_MODE_EXEC,
    LAUNCH_MODE_SHELL,
    MAX_ARGS,
    MAX_ENV_ENTRIES,
    MAX_NAME_LEN,
    MAX_PORTS_PER_SERVICE,
    SUPPORTED_ENCODINGS,
    LaunchSpec,
    ReadyWhen,
    ServiceSpec,
    slugify,
)

MODE_SHELL_LABEL = "命令行（cmd /d /s /c）"
MODE_EXEC_LABEL = "程序与参数（不经 shell）"


class ServiceEditorDialog(QDialog):
    def __init__(
        self,
        *,
        existing: Optional[ServiceSpec] = None,
        suggested_name: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._existing = existing
        self._spec: Optional[ServiceSpec] = None
        self._result_spec: Optional[ServiceSpec] = None
        self.setWindowTitle("编辑服务" if existing else "新增服务")
        self.setMinimumWidth(640)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setSpacing(8)

        self._name_edit = QLineEdit(existing.name if existing else suggested_name)
        self._name_edit.setPlaceholderText("例如：Shop API")
        form.addRow(self._required("名称"), self._name_edit)

        self._id_edit = QLineEdit(existing.service_id if existing else "")
        self._id_edit.setPlaceholderText("自动由名称生成，可修改；保存后不可改")
        if existing:
            self._id_edit.setReadOnly(True)
            self._id_edit.setToolTip("服务 ID 保存后不可修改；如需变更请另建服务")
        form.addRow(self._required("服务 ID"), self._id_edit)

        cwd_row = QHBoxLayout()
        cwd_row.setSpacing(6)
        self._cwd_edit = QLineEdit(existing.cwd if existing else "")
        self._cwd_edit.setPlaceholderText("必须是存在的绝对目录")
        cwd_row.addWidget(self._cwd_edit, 1)
        browse = QPushButton("选择…")
        browse.clicked.connect(self._pick_directory)
        cwd_row.addWidget(browse)
        cwd_wrap = QWidget()
        cwd_wrap.setLayout(cwd_row)
        form.addRow(self._required("工作目录"), cwd_wrap)

        self._mode_combo = QComboBox()
        self._mode_combo.addItem(MODE_SHELL_LABEL, LAUNCH_MODE_SHELL)
        self._mode_combo.addItem(MODE_EXEC_LABEL, LAUNCH_MODE_EXEC)
        if existing and existing.launch.mode == LAUNCH_MODE_EXEC:
            self._mode_combo.setCurrentIndex(1)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("启动方式", self._mode_combo)

        self._command_edit = QLineEdit()
        self._command_edit.setPlaceholderText("例如：npm run dev  或  mvn spring-boot:run")
        self._command_label = QLabel("命令")
        form.addRow(self._command_label, self._command_edit)

        exec_row = QHBoxLayout()
        exec_row.setSpacing(6)
        self._program_edit = QLineEdit()
        self._program_edit.setPlaceholderText("例如：D:\\Python\\python.exe")
        exec_row.addWidget(self._program_edit, 1)
        prog_browse = QPushButton("选择…")
        prog_browse.clicked.connect(self._pick_program)
        exec_row.addWidget(prog_browse)
        exec_wrap = QWidget()
        exec_wrap.setLayout(exec_row)
        self._program_label = QLabel("可执行文件")
        form.addRow(self._program_label, exec_wrap)

        self._args_edit = QLineEdit()
        self._args_edit.setPlaceholderText("参数按空格分隔，含空格的项用双引号包裹")
        self._args_label = QLabel("参数")
        form.addRow(self._args_label, self._args_edit)

        self._ports_edit = QLineEdit()
        self._ports_edit.setPlaceholderText("例如：8080, 5173（最多 16 个，可留空）")
        if existing and existing.ports:
            self._ports_edit.setText(", ".join(str(p) for p in existing.ports))
        form.addRow("端口", self._ports_edit)

        root.addLayout(form)

        # -- 高级区 ---------------------------------------------------------
        self._advanced_btn = QPushButton("▸ 高级设置（环境变量、编码、就绪条件、持久化）")
        self._advanced_btn.setObjectName("FlatBtn")
        self._advanced_btn.clicked.connect(self._toggle_advanced)
        root.addWidget(self._advanced_btn)

        self._advanced = QWidget()
        adv_layout = QVBoxLayout(self._advanced)
        adv_layout.setContentsMargins(0, 4, 0, 0)
        adv_layout.setSpacing(8)

        adv_layout.addWidget(self._hint("环境变量（键值表；值将保存在本机配置文件，不加密）"))
        self._env_table = QTableWidget(0, 2)
        self._env_table.setHorizontalHeaderLabels(["变量名", "值"])
        self._env_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._env_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._env_table.setMinimumHeight(110)
        adv_layout.addWidget(self._env_table)
        env_btns = QHBoxLayout()
        add_env = QPushButton("添加变量")
        add_env.clicked.connect(lambda: self._env_table.insertRow(self._env_table.rowCount()))
        del_env = QPushButton("删除选中")
        del_env.clicked.connect(self._remove_env_row)
        env_btns.addWidget(add_env)
        env_btns.addWidget(del_env)
        env_btns.addStretch(1)
        adv_layout.addLayout(env_btns)

        adv_form = QFormLayout()
        adv_form.setSpacing(8)
        self._encoding_combo = QComboBox()
        for enc in SUPPORTED_ENCODINGS:
            self._encoding_combo.addItem("UTF-8" if enc == "utf-8" else "GB18030", enc)
        if existing:
            index = self._encoding_combo.findData(existing.encoding)
            if index >= 0:
                self._encoding_combo.setCurrentIndex(index)
        adv_form.addRow("编码", self._encoding_combo)

        self._ready_port_spin = QSpinBox()
        self._ready_port_spin.setRange(0, 65535)
        self._ready_port_spin.setSpecialValueText("（不检查端口）")
        self._ready_regex_edit = QLineEdit()
        self._ready_regex_edit.setPlaceholderText("例如：Started .*Application in")
        if existing and existing.ready_when:
            if existing.ready_when.port:
                self._ready_port_spin.setValue(existing.ready_when.port)
            if existing.ready_when.log_regex:
                self._ready_regex_edit.setText(existing.ready_when.log_regex)
        adv_form.addRow("就绪观察端口", self._ready_port_spin)
        adv_form.addRow("就绪观察正则", self._ready_regex_edit)
        adv_layout.addLayout(adv_form)
        adv_layout.addWidget(
            self._hint("就绪条件命中只作为“事实”呈现，是否就绪仍由 Agent 判断。")
        )

        self._ephemeral_check = QCheckBox("临时服务：不写入 services.json（本次会话内保留）")
        if existing and existing.ephemeral:
            self._ephemeral_check.setChecked(True)
        adv_layout.addWidget(self._ephemeral_check)

        self._advanced.hide()
        root.addWidget(self._advanced)

        # -- 错误与按钮 ------------------------------------------------------
        self._error_label = QLabel("")
        self._error_label.setObjectName("InlineError")
        self._error_label.setWordWrap(True)
        self._error_label.hide()
        root.addWidget(self._error_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._name_edit.textChanged.connect(self._maybe_autofill_id)
        self._id_edit_autofilled = not existing
        self._on_mode_changed()

        if existing:
            if existing.launch.mode == LAUNCH_MODE_SHELL:
                self._command_edit.setText(existing.launch.command or "")
            else:
                self._program_edit.setText(existing.launch.program or "")
                self._args_edit.setText(" ".join(_quote_arg(a) for a in existing.launch.args))
            for key, value in existing.env.items():
                row = self._env_table.rowCount()
                self._env_table.insertRow(row)
                self._env_table.setItem(row, 0, QTableWidgetItem(key))
                self._env_table.setItem(row, 1, QTableWidgetItem(value))

    # ------------------------------------------------------------------
    def _required(self, text: str) -> QWidget:
        wrap = QWidget()
        layout = QHBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        mark = QLabel("*")
        mark.setObjectName("RequiredMark")
        layout.addWidget(mark)
        layout.addWidget(QLabel(text))
        return wrap

    def _hint(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Hint")
        label.setWordWrap(True)
        return label

    # ------------------------------------------------------------------
    def _maybe_autofill_id(self, text: str) -> None:
        if not self._id_edit_autofilled or self._existing:
            return
        if self._id_edit.text().strip() and self._id_edit.hasFocus():
            self._id_edit_autofilled = False
            return
        self._id_edit.setText(slugify(text, fallback=""))

    def _on_mode_changed(self) -> None:
        is_shell = self._mode_combo.currentData() == LAUNCH_MODE_SHELL
        self._command_label.setVisible(is_shell)
        self._command_edit.setVisible(is_shell)
        self._program_label.setVisible(not is_shell)
        self._program_edit.setVisible(not is_shell)
        self._args_label.setVisible(not is_shell)
        self._args_edit.setVisible(not is_shell)
        self._program_edit.parentWidget().setVisible(not is_shell)

    def _toggle_advanced(self) -> None:
        open_now = not self._advanced.isVisible()
        self._advanced.setVisible(open_now)
        self._advanced_btn.setText(
            "▾ 高级设置（环境变量、编码、就绪条件、持久化）"
            if open_now
            else "▸ 高级设置（环境变量、编码、就绪条件、持久化）"
        )

    def _pick_directory(self) -> None:
        start = self._cwd_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "选择工作目录", start)
        if chosen:
            self._cwd_edit.setText(chosen)

    def _pick_program(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "选择可执行文件", self._program_edit.text().strip() or str(Path.home()),
            "可执行文件 (*.exe *.cmd *.bat);;所有文件 (*)"
        )
        if chosen:
            self._program_edit.setText(chosen)

    def _remove_env_row(self) -> None:
        rows = sorted({index.row() for index in self._env_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self._env_table.removeRow(row)

    # ------------------------------------------------------------------
    def _on_accept(self) -> None:
        try:
            self._result_spec = self.build_spec()
        except SpectaError as exc:
            self._show_error(exc.message)
            return
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self.accept()

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()

    # ------------------------------------------------------------------
    def build_spec(self) -> ServiceSpec:
        name = self._name_edit.text().strip()
        if not name:
            raise ValueError("请填写服务名称")
        if len(name) > MAX_NAME_LEN:
            raise ValueError(f"名称最多 {MAX_NAME_LEN} 个字符")

        service_id = self._id_edit.text().strip()
        if not service_id:
            service_id = slugify(name, fallback="")
        if not service_id or not ID_PATTERN.match(service_id):
            raise ValueError("服务 ID 必须匹配 [a-zA-Z0-9][a-zA-Z0-9._-]{0,63}")

        cwd = self._cwd_edit.text().strip()
        if not cwd:
            raise ValueError("请填写工作目录")
        cwd_path = Path(cwd)
        if not cwd_path.is_absolute():
            raise ValueError("工作目录必须是绝对路径")
        if not cwd_path.is_dir():
            raise ValueError(f"工作目录不存在或不是目录：{cwd}")

        mode = self._mode_combo.currentData()
        if mode == LAUNCH_MODE_SHELL:
            command = self._command_edit.text().strip()
            if not command:
                raise ValueError("请填写启动命令")
            launch = LaunchSpec(mode=LAUNCH_MODE_SHELL, command=command)
        else:
            program = self._program_edit.text().strip()
            if not program:
                raise ValueError("请填写可执行文件路径")
            args = _split_args(self._args_edit.text())
            if len(args) > MAX_ARGS:
                raise ValueError(f"参数最多 {MAX_ARGS} 项")
            launch = LaunchSpec(mode=LAUNCH_MODE_EXEC, program=program, args=tuple(args))

        ports = _parse_ports(self._ports_edit.text())

        env: Dict[str, str] = {}
        for row in range(self._env_table.rowCount()):
            key_item = self._env_table.item(row, 0)
            value_item = self._env_table.item(row, 1)
            key = key_item.text().strip() if key_item else ""
            if not key:
                continue
            if len(env) >= MAX_ENV_ENTRIES:
                raise ValueError(f"环境变量最多 {MAX_ENV_ENTRIES} 项")
            env[key] = value_item.text() if value_item else ""

        encoding = self._encoding_combo.currentData() or "utf-8"

        ready_port = self._ready_port_spin.value() or None
        ready_regex = self._ready_regex_edit.text().strip() or None
        if ready_port is not None and ready_port not in ports:
            raise ValueError("就绪观察端口必须包含在端口列表中")
        ready_when = ReadyWhen(port=ready_port, log_regex=ready_regex)
        if ready_when.port is None and ready_when.log_regex is None:
            ready_when = None

        origin = self._existing.origin if self._existing else "user"

        return ServiceSpec(
            service_id=service_id,
            name=name,
            launch=launch,
            cwd=str(cwd_path),
            env=env,
            ports=tuple(ports),
            encoding=encoding,
            ready_when=ready_when,
            origin=origin,
            ephemeral=bool(self._ephemeral_check.isChecked()),
        )

    def result_spec(self) -> Optional[ServiceSpec]:
        return self._result_spec


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _parse_ports(text: str) -> List[int]:
    ports: List[int] = []
    for chunk in text.replace("，", ",").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = int(chunk)
        except ValueError:
            raise ValueError(f"端口不是整数：{chunk}") from None
        if not (1 <= value <= 65535):
            raise ValueError(f"端口超出范围：{value}")
        if value not in ports:
            ports.append(value)
    if len(ports) > MAX_PORTS_PER_SERVICE:
        raise ValueError(f"每服务最多声明 {MAX_PORTS_PER_SERVICE} 个端口")
    return ports


def _split_args(text: str) -> List[str]:
    """按空格拆分，支持双引号包裹含空格的参数。"""
    args: List[str] = []
    current = ""
    in_quote = False
    for ch in text.strip():
        if ch == '"':
            in_quote = not in_quote
            continue
        if ch == " " and not in_quote:
            if current:
                args.append(current)
                current = ""
            continue
        current += ch
    if current:
        args.append(current)
    return args


def _quote_arg(arg: str) -> str:
    if " " in arg or "\t" in arg:
        return f'"{arg}"'
    return arg
