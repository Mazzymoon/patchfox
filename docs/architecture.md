# PatchFox 架构

PatchFox 是一个直接在本地代码仓库中运行的轻量 coding agent。默认运行时只有 Python 进程、模型 API 客户端和本地工具，不要求 WSL、Web 服务、独立数据库或常驻调度器。

## 主链路

```text
CLI / REPL / Textual TUI
          │
          ▼
      PatchFox runtime ─── session / memory / run evidence
          │
          ▼
        Engine ─────────── context assembly ── provider adapter
          │
          ▼
  tool lookup → argument validation → permission and safety checks
              → local execution → workspace diff → result recording
          │
          ▼
  file tools / local shell / workers / skills
```

## 模块边界

- `patchfox/cli.py`：解析命令行、项目配置和 provider profile，装配一次本地运行。
- `patchfox/core/runtime.py`：管理会话、任务状态、上下文、恢复和最终报告。
- `patchfox/core/engine.py`：驱动 model/tool 循环，不负责平台专属的命令解析。
- `patchfox/tools/`：文件、搜索、补丁、shell 等本地工具及其参数约束。
- `patchfox/providers/`：OpenAI-compatible、Anthropic-compatible 与 DeepSeek API 适配。
- `patchfox/features/`：memory、skills 和 worker 等可关闭的本地增强功能。
- `.patchfox/`：项目内的 session、run evidence、memory 和 plan 数据。

## Windows 与 shell

`run_shell` 通过一个很小的 `ShellBackend` 接口执行命令，目的是消除 `shell=True` 在不同操作系统上的隐式差异：

- Windows 的 `auto` 依次尝试 Git Bash、PowerShell、CMD，不要求安装 WSL。
- Linux/macOS 默认使用 Bash 或兼容的 POSIX shell。
- `--shell` 和 `[shell].backend` 可以固定项目所需的命令方言。
- 实际选择的 shell 会写入 prompt、session event 和 run report，便于复现失败。

这只是本地进程执行适配，不是插件系统，也不会引入服务发现、热加载或远程运行时。

## 数据与可恢复性

- `.patchfox/sessions/*.json` 保存可恢复的会话快照。
- `.patchfox/sessions/*.events.jsonl` 保存会话事件。
- `.patchfox/runs/*/task_state.json` 保存单次任务状态。
- `.patchfox/runs/*/trace.jsonl` 与 `report.json` 保存调试和验收证据。

所有数据默认留在当前仓库。写入使用原子替换；同一运行内的并发状态写入由 `RunStore` 串行化，避免 Windows 上后台任务与主循环竞争文件。

## 轻量化约束

核心继续遵守以下边界：

- 不增加 Web server、浏览器 UI 或独立数据库；
- 不把默认工具拆成需要额外进程的服务；
- 不要求 Node.js、容器、WSL 或远程 sandbox 才能启动；
- 可选功能必须能关闭，关闭后不影响基本的读、改、测闭环；
- 新抽象必须解决已经存在的跨平台或测试问题，不为假设中的扩展预留复杂框架。

当前重构重点只有三项：统一 PatchFox 品牌与本地状态协议、保证 Windows/Linux/macOS 的命令执行一致性、用回归测试守住文件安全和运行证据。
