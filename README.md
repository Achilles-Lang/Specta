# Specta

写代码时看见日志输出感觉安心，可是IDE内存占用多，前端后端启动后电脑卡顿。使用codex、trae等工具看不到日志输出感觉烧心，总觉得面对一个黑盒，所以写了一个工具，能够在使用codex等工具coding时，能看到项目的日志输出。

面向 Vibe Coding 的本地开发运行台：**进程托管 + 多路日志看板 + MCP 工具面**。

Specta 让 AI Agent 能够自己把服务跑起来、看日志、判断是否就绪，而不是靠人肉复制粘贴终端输出。
一个常驻 Daemon 负责进程与日志，一个轻量 MCP stdio 桥把能力暴露给 Agent，两者通过本机回环
IPC 通信。

```
        ┌──────────────┐   stdio (MCP)   ┌───────────────────────┐
Agent ─▶│ specta-bridge│ ──────────────▶ │ 官方 MCP SDK (asyncio)│
        └──────┬───────┘                 └───────────┬───────────┘
               │  127.0.0.1:动态端口 + token          │ JSON Lines
               ▼                                      ▼
        ┌──────────────────────────────────────────────────────┐
        │  Daemon (PySide6 / Qt 事件循环)                        │
        │  ServiceManager ─ ManagedService ─ LogPipeline        │
        │        │                 │              │             │
        │        │      Windows Job Object        │  pyte 屏/历史 │
        │        ▼                 ▼              ▼             │
        │  launch_worker ──▶ 目标进程（整组随 Job 回收）          │
        └──────────────────────────────────────────────────────┘
```

---

## 1. 环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11（进程树回收依赖 Job Object；非 Windows 会降级为 `taskkill`/无 Job） |
| Python | ≥ 3.12（实测 3.13.14） |
| 图形 | 桌面会话（Daemon 是 Qt 应用；无界面模式见 `SPECTA_NO_UI`） |

主要依赖：`PySide6-Essentials`、`pyte`、`mcp`、`psutil`、`pywin32`（Windows）、`regex`。

## 2. 安装

```bash
git clone <repo> specta && cd specta
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

安装后提供两个入口脚本：

| 命令 | 说明 |
| --- | --- |
| `specta` | 启动 Daemon（含图形界面） |
| `specta-bridge` | 启动 MCP stdio 桥（由 Agent 客户端拉起） |

也可不安装、直接用模块方式运行：

```bash
set PYTHONPATH=src
python -m specta.daemon_main
python -m specta.bridge_main
```

## 3. 启动与操作

1. 运行 `specta`（或 `python -m specta.daemon_main`）→ 出现主窗口，托盘图标常驻。
2. 在界面中「新建服务」，填写启动方式：
   - **exec 模式**：`program` + `args`（推荐，参数不做 shell 解析）；
   - **shell 模式**：整条 `command`，按 Windows `cmd /d /s /c` 语义执行，**引号原样保留**。
     下列写法均可用：
     ```bat
     mvn spring-boot:run -Dspring-boot.run.jvmArguments="-Xmx1024m"
     "C:\Program Files\Java\jdk-17\bin\java.exe" -jar "D:\app\my app.jar"
     set MAVEN_OPTS=-Xmx1024m&&mvn spring-boot:run
     ```
     > 早期版本这里会**吃掉引号**：`cmd /s` 的引号处理，叠加旧实现走
     > `subprocess` 列表拼装时注入的 `\"` 转义，使 `-Dk="v v"` 变成 `-Dk=\"v v\"`、
     > 带空格路径直接不可执行，现场报 `此时不应有 =-Xmx1024m`（exit 255）。
     > 现已修复：整条命令行以字符串形式交给 CreateProcess，且仅在命令以引号开头时
     > 多包一层引号。
3. 点击启动后：日志实时滚动，顶部显示进程事实（pid / 生命周期 / 端口 / 错误命中数）。
4. 关闭窗口的默认行为是**隐藏到托盘**（服务继续跑）；「退出 Specta」才会停止全部服务。

### 生命周期语义

- `start_service` 成功 **只代表目标进程创建成功**，不代表 HTTP 健康或应用就绪；
- 是否就绪由 Agent 或人根据日志判断，并通过 `report_service_state` 上报；
- 自然退出 → `exited`；被停止 → `stopped`；端口被占 → 启动直接失败并给出占用者 PID。

## 4. 目录与文件

全部状态位于 `%LOCALAPPDATA%\Specta\`：

| 文件 | 说明 |
| --- | --- |
| `daemon.json` | 运行实例端点：`protocol_version` / `instance_id` / `pid` / `port` / `token` / `started_at` |
| `daemon.lock` | 单实例互斥（QLockFile），持有至进程退出 |
| `services.json` | 服务配置（schema 版本化；坏文件不覆盖，进入恢复流程） |
| `ui.json` | 界面偏好（字号、上下文行数、关闭行为、窗口几何） |
| `diagnostics\specta.log` | 诊断日志（轮转 3×1 MiB） |

> `daemon.json` 里带 **token**，是本机 IPC 的凭据；不要提交或外发。

### 环境变量

| 变量 | 作用 |
| --- | --- |
| `SPECTA_NO_UI=1` | 无界面模式：仍启动 IPC 与进程管理（集成测试使用） |
| `SPECTA_ALLOW_NO_JOB=1` | 开发逃生口：Job 创建/加入失败时降级为 `taskkill /T /F` 兜底 |

## 5. 接入 AI Agent（MCP）

把下面片段合并进你的 MCP 客户端配置（界面「设置 → 复制 MCP 配置」可直接拿到等价内容）：

```json
{
  "mcpServers": {
    "specta": {
      "command": "<SPECTA 所用的 python.exe 绝对路径>",
      "args": ["-m", "specta.bridge_main"]
    }
  }
}
```

桥自身**不持有业务状态**：它发现 Daemon（读 `daemon.json`）；Daemon 未运行时会自启一个，
再把工具调用转发过去。**注意**：自启的 Daemon 生命周期**绑定宿主客户端** —— 客户端退出，
Daemon 与其托管服务会被一并结束（设计语义，见 §9 第 7 条）。若需要 Daemon 在客户端退出后
继续存活，请先手动启动 Specta 再连 MCP。

**桥的 stdout 是 MCP 协议通道**，桥不会向 stdout 打印任何内容（日志一律走 stderr）。

### 工具清单（第一版固定 7 个）

| 工具 | 作用 |
| --- | --- |
| `list_services` | 列出服务（ID/名称/来源/cwd/启动方式/端口/状态），不返回环境变量 |
| `get_service_status` | 完整快照：进程事实 + Agent 判断 + 当前/最近操作 + 日志元信息 |
| `start_service` | 启动。新 ID 需完整 spec；已有 ID 可只给 `service_id` |
| `restart_service` | 停整组 → 等端口释放 → 按原配置重启，返回新 `run_id` |
| `stop_service` | 硬终止整组进程并清理 Job（保留配置） |
| `get_recent_logs` | 读日志；`filter_error=true` 返回完整错误块（保留堆栈）；`run_id='all'` 跨运行 |
| `report_service_state` | 上报判断 `ready`/`error`/`unknown`，必须携带当前 `run_id` |

统一返回信封（成功与失败同形，均为**直接信封**，不额外包一层）：

```json
{ "ok": true,  "data": { ... }, "error": null }
{ "ok": false, "data": null, "error": { "code": "PORT_IN_USE", "message": "...", "details": {} } }
```

该信封同时出现在工具结果的正文与结构化内容中。**业务失败时协议标志同步置位**：
`CallToolResult.is_error = true`（成功为 `false`），使宿主/Agent 按协议标志即可识别失败，
无需先解析正文（对应审查报告 F13）。

`start/stop/restart` 会等到操作终止状态才返回；超时返回 `OPERATION_PENDING`，**不要盲目重做**，
改用 `get_service_status` 查询。写操作支持 `request_key` 幂等去重。

### Agent 典型流程

```
list_services → start_service → get_service_status / get_recent_logs
   → 自己判断就绪/异常 → report_service_state → 改代码 → restart_service → 复检
```

## 6. 资源上限

单服务：

| 项 | 上限 |
| --- | --- |
| 历史行数 | 3000 行 |
| 历史内容 | 4 MiB |
| 原始字节（用于窗口缩放重放） | 2 MiB |
| Specta 事件行 | 800 |
| 单行长度 | 64 KiB（超出截断） |
| 终端列数 | 40–400（默认 120） |
| 日志查询行数 | 1–500 |

全局：

| 项 | 上限 |
| --- | --- |
| 注册服务 | 50 |
| 同时运行 | 10 |
| 同服务排队操作 | 8（超出返回 `BUSY`） |
| IPC 连接 | 32 |
| 未鉴权连接存活 | 3 s 后断开 |
| 已完成操作保留 | 15 分钟 / 1000 条 |
| 启动握手超时 | 15 s |
| 停止超时 | 5 s |
| 重启端口释放等待 | 5 s |

日志**只在内存中保留**，Daemon 退出后不恢复。

> **保留预算口径说明**：上表「历史行数 3000」是**输出历史**的上限（`MAX_RETAINED_ROWS`）；活动屏幕
> （≤ 屏幕 80 行）与 Specta 事件（≤ 800）**另行独立限界**，因此单服务快照总行数上限约
> 3000 + 80 + 800。三层均有硬上限、不会无界增长。

## 7. 编码与终端渲染

- 服务输出按 `spec.encoding` 解码（默认 `utf-8`，`replace` 兜底，不解码失败即丢数据）。
- 为正确显示中文/emoji 等**宽字符**，内部按「终端列」与「码点」两套下标换算，避免半宽截断。
- 被管道输出时很多程序只发 `\n`（没有 PTY 的 ONLCR 转换），Specta 会规范化 `LF→CRLF`，
  避免日志「阶梯状」错位。
- 若目标程序支持，会注入 `FORCE_COLOR` / `CLICOLOR_FORCE` / `PY_COLORS` 保留颜色；
  设置 `NO_COLOR` 则不注入。

## 8. 诊断

- 诊断日志：`%LOCALAPPDATA%\Specta\diagnostics\specta.log`（界面「设置 → 打开诊断目录」）。
- `daemon.json` 缺失/过期 → 桥会尝试重连或自启 Daemon。
- 配置损坏：`services.json` 解析失败时**保留原文件**、以空配置启动并提示恢复，不会静默覆盖；
  schema 版本高于当前版本时按只读处理。
- 内部错误会返回 `diagnostic_id`，可到日志中检索同 id 定位。

## 9. 已知限制与注意事项

1. **venv 「重定向器」与 pid 语义（本机实测）**：某些 Python 发行版里 `venv\Scripts\python.exe`
   是一个 redirector，会再派生真正的解释器作为子进程。此时 `subprocess.Popen(...).pid` ≠
   目标进程的 `os.getpid()`。Specta 记录的 `pid` 取自进程自身，`launcher_pid` 为受控启动器 pid；
   写脚本做清理时请按**进程树**处理（或用 `daemon.json` 里的 `pid`），只 `terminate()` 外层
   句柄可能留下真正的服务进程。
2. **Job Object 归属**：若宿主进程本身在禁止 breakaway 的 Job 中，服务可能无法加入新 Job，
   此时会明确返回 `JOB_ASSIGN_FAILED`（可用 `SPECTA_ALLOW_NO_JOB=1` 降级，仅适合开发）。
3. **端口预检是「启动前快照」**：启动与真正 bind 之间仍存在竞态，极端情况下目标程序自身
   bind 失败会体现为进程很快退出（`exited`），请以日志为准。
4. **日志不落盘**：Daemon 退出即丢，需要长期留存请自行重定向到文件。
5. **第一版固定暗色主题**，设置中不提供主题切换。
6. **shell 模式的引号语义（F15，已修复）**：`command` 按 Windows `cmd /d /s /c` 执行，
   引号**原样保留**（含 `"C:\Program Files\..."` 这类带空格路径）。早期版本会破坏引号：
   `cmd /s` 的引号处理叠加旧实现经 `subprocess` 列表拼装注入的 `\"` 转义，使
   `-Dk="v v"` 变成 `-Dk=\"v v\"`，现场报 `此时不应有 =-Xmx1024m`（exit 255）。
   若你在旧版本上被迫写过 `set XXX=...&&` 这类绕行写法，现在可以直接用引号形式。
7. **自启 Daemon 随宿主客户端退出（设计语义，已确认）**：Daemon 由 MCP 桥自动拉起时，处在宿主
   客户端（含官方 MCP SDK 的 stdio 子进程）的 Windows Job 内；宿主退出会关闭该 Job，
   **Daemon 与它托管的服务一并结束**。这是预期行为，不是缺陷 —— `DETACHED_PROCESS` /
   `CREATE_NEW_PROCESS_GROUP` / `CREATE_NO_WINDOW` **不等于**脱离 Job。
   需要 Daemon 在客户端退出后继续存活时（例如多个 Agent 共用一个 Specta），请**先手动启动 Specta
   再连 MCP**；此时关闭任一桥都不影响共享 Daemon。已拉起的 Daemon 存在时，桌面 UI 因单实例守卫
   不会另开窗口、也不会接管它。

## 10. 开发

```
src/specta/
  domain/          纯数据与规则（models / errors / protocol）
  infrastructure/  Job、端口探测、设置、仓库、IPC 服务端与客户端、诊断
  application/     ServiceManager / ManagedService / OperationRegistry / 参数解析
  logs/            LogPipeline（原始字节 + 输出历史 + 事件）、pyte 屏幕、查询、导出
  ui/              主窗口、标签页、服务头、日志视图、编辑与设置对话框（PySide6）
  bridge/          MCP 工具契约与注册
  daemon_main.py   Daemon 入口
  bridge_main.py   MCP stdio 桥入口
  launch_worker.py 受控启动器（延迟创建目标进程，消除 Job 归属竞态）
```

分层约束：`domain` 不依赖上层；`bridge` 不导入 Qt；业务校验只在 Daemon 侧做一份（`application/params.py`），
桥只做参数整理与转发。
