# 配置

PatchFox 使用用户级全局配置，不要求每个代码项目复制 `.env` 或保存 API Key。

首次运行可以直接启动 `patchfox` / `patchfox-tui`，也可以显式执行：

```powershell
patchfox config init
patchfox config show
```

## 全局文件

| 文件 | 内容 |
| --- | --- |
| `~/.patchfox/config.toml` | Provider、协议、Base URL、默认模型；不保存密钥。 |
| `~/.patchfox/auth.json` | 各 Provider 的 API Key。 |
| `~/.patchfox/projects.json` | 每个项目最近选择的 Provider 和模型。 |

示例 `config.toml`：

```toml
version = 1
default_provider = "deepseek"

[providers.deepseek]
protocol = "anthropic"
base_url = "https://api.deepseek.com/anthropic"
model = "deepseek-v4-pro"

[providers.openai]
protocol = "openai"
base_url = "https://api.openai.com/v1"
model = "gpt-5.4"
```

Provider 名称用于选择配置；真正决定请求格式的是 `protocol`，目前支持
`openai` 和 `anthropic`。例如 DeepSeek 的 Anthropic-compatible endpoint 应配置
`protocol = "anthropic"`。

## 按项目选择

进入任意代码目录后直接运行：

```powershell
cd F:\project\demo
patchfox-tui
```

在 TUI 或 REPL 中切换：

```text
/provider openai
/model gpt-5.4
```

切换立即作用于下一条消息，并写入全局 `projects.json`；目标项目不会因此新增配置文件。

已有 `.patchfox.toml` 时，项目只能提供安全的默认选择：

```toml
provider = "deepseek"
model = "deepseek-v4-pro"

[shell]
backend = "auto"
```

项目中的 `api_key`、`base_url`、`protocol` 和 `[providers.*]` 会被忽略并产生警告，
避免不可信仓库把全局 API Key 重定向到其他服务器。

## 优先级

Provider 和模型：

```text
CLI 参数 > 当前 TUI 会话 > projects.json > 项目安全选择 > 全局默认
```

API Key：

```text
--api-key > 当前进程环境变量 > ~/.patchfox/auth.json
```

协议和 Base URL 只来自显式 CLI/进程覆盖、显式 `--config` 或全局配置，不接受项目覆盖。

## 环境变量与自动化

环境变量仍适合 CI 和临时覆盖，但项目 `.env` 不会被 PatchFox 自动加载：

| 变量 | 用途 |
| --- | --- |
| `PATCHFOX_PROVIDER` | 本次进程使用的 Provider。 |
| `PATCHFOX_API_KEY` / `PATCHFOX_BASE_URL` / `PATCHFOX_MODEL` | 通用覆盖。 |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` | Anthropic。 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | OpenAI。 |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | DeepSeek。 |
| `PATCHFOX_HOME` | 测试或便携安装时覆盖全局配置目录。 |

非交互任务缺少配置时会在创建 Agent 前退出并返回状态码 `2`。

## 旧配置迁移

首次交互启动时，PatchFox 会检测旧的 `~/.config/patchfox/config.toml`、项目 `.env`
和旧项目 `.patchfox.toml`。迁移前会列出来源、Provider、模型以及“是否存在 Key”，但绝不
显示 Key 内容；只有明确确认后才写入 `~/.patchfox`。

## 常用 CLI 参数

```powershell
patchfox --provider deepseek --model deepseek-v4-pro
patchfox --api-key sk-... --base-url https://...
patchfox --max-steps 50 --max-new-tokens 4096
patchfox --approval ask
patchfox --sandbox best_effort
patchfox --shell auto
patchfox --cwd F:\project\demo
patchfox --resume latest
```

运行 `patchfox --help` 查看完整参数。
