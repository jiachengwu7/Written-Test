# Mini Code Agent

一个在指定工作目录中读代码、改文件、执行命令的终端助手。Agent 循环使用 Python 编写，模型通过标准库 HTTP 客户端接入，工具用 Pydantic 生成参数 schema 并校验输入。可以使用离线 Mock 验收。

## 五分钟运行

安装 Python 3.10+ 和 uv，在仓库根目录执行以下命令。首次同步需要下载依赖，之后的 Mock 运行和测试都可离线完成。

```sh
uv sync --frozen
uv run --frozen mini-code-agent --provider mock "列出工作目录的内容"
uv run --frozen python -m unittest discover -v
```

内置 Mock 固定演示列目录和读取结果。要接入真实模型，编辑项目根目录的 `.env`。从新检出的仓库启动时，可以先从模板复制一份：

```powershell
if (!(Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
notepad .env
```

模板预填了两种按量付费接口的地址。使用 OpenAI 兼容接口时设置 `MINI_AGENT_PROVIDER=openai`，填写 `OPENAI_MODEL` 和 `OPENAI_API_KEY`。使用 Anthropic 兼容接口时改为 `MINI_AGENT_PROVIDER=anthropic`，填写 `ANTHROPIC_MODEL` 和 `ANTHROPIC_API_KEY`。模型名和密钥均来自相应平台。将文件保存为 UTF-8，然后在项目根目录启动：

```powershell
uv run --frozen mini-code-agent
```

末尾附任务文本会执行单轮然后退出。省略任务文本会进入连续对话，输入 `/exit` 退出。`/model anthropic <模型名>` 会在当前会话使用配置中 Anthropic 的地址和密钥。通过 `uv run --frozen mini-code-agent --resume <会话ID>` 可在原工作目录继续。修改配置文件后重新启动即可生效。

| 设置 | 用途 |
| --- | --- |
| `MINI_AGENT_WORKSPACE` | 在 `.env` 中设置新会话的工作目录，例如 `MINI_AGENT_WORKSPACE=D:/agent_work/c-practice`。启动时自动创建缺失的目录及其父目录。相对路径以该 `.env` 所在目录为起点，留空则使用启动目录 |
| `--workspace` | 为新会话临时覆盖工作目录。`--resume` 沿用原会话目录，显式传入的目录须与原目录一致 |
| `--env-file <路径>` | 读取指定的 dotenv 文件，也支持 `--config` 别名。默认读取启动目录的 `.env`，指定 `--workspace` 仅改变工具工作目录 |
| `--provider`、`--model`、`--base-url` | 临时覆盖对应选项。真实密钥放在 Git 忽略的 `.env` 中，仓库中的 `.env.example` 用作无密钥模板 |
| `--max-steps`、`--context-window` | 每轮模型调用上限默认 30，上下文预算默认 32768 个估算 token |
| `--yes` | 跳过写文件、编辑、Shell 命令的逐项确认 |
| `--mock-script <路径>` | 指定 UTF-8 JSON 数组，每次模型调用消耗一个元素。数组耗尽时报告错误 |
| `MINI_AGENT_HOME` | 会话与日志目录，默认用户主目录下的 `.mini-code-agent` |

模型设置取自命令行和 `.env`。启动目录缺少 `.env` 时才读取系统里的模型环境变量，均未指定协议时使用离线 Mock。显式指定的文件缺失或语法有误时会报告错误。程序支持引号、注释和值中的等号，按字面值使用密钥。文件工具会排除当前加载的配置文件。

例如将下列内容保存为 `demo.json`，运行 `uv run --frozen mini-code-agent --provider mock --mock-script demo.json "创建说明文件"`，输入 `y` 可看到文件落盘。输入 `n` 会得到拒绝结果，循环继续消费下一条脚本响应。

```json
[
  {"calls":[{"id":"w1","name":"write_file","arguments":{"path":"hello.txt","content":"你好！\n"}}]},
  {"content":"本次离线演示结束。"}
]
```

