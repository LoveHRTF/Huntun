# 馄饨 · Huntun

[English](README.md) · 简体中文

**运行在你自己机器上、对你负责的自主软件团队。**

馄饨：许多小小的部分，包在一起，端上来就是一道完整的菜。

<img width="1589" height="1104" alt="Screenshot 2026-09-23 at 9 45 30 PM" src="https://github.com/user-attachments/assets/344e9ba0-8c0f-4826-9a29-0218abd640e0" />

Huntun 将一段书面目标转化为一支配置完整的工程团队。主管智能体负责组建合适的角色、分派任务、审核产出，并对交付结果全程负责。团队完成设计、开发、测试、文档与提交，所有成果进入同一个 git 仓库；成员在共享讨论板上协作，你可以随时阅读与参与，同时实时办公室视图展示每一位成员的工作状态。

Huntun 直接使用你现有的 AI 订阅。无需 API key，无需引入新的供应商关系，除你今天已在进行的模型调用之外，没有任何数据离开你的机器。

## 你将获得什么

**一份需求，一支完整团队。** 描述你要的结果。主管会提出项目所需的角色、人数，以及每个席位的模型与推理强度，在成本与难度之间取得平衡，并给出附带费用估算的方案。未经你批准，不会启动任何工作。

**可问责的领导。** 主管对交付与团队负责。它与你确认目标和「完成的定义」，维护实时交付状态，上报阻塞事项，确保每位成员只在自身职责范围内工作，并将人员调整提交给你审批。它负责管理，从不亲自编写代码。当「完成的定义」全部达成并有证据支持时，由它向你交付最终报告。

**透明的协作。** 成员在讨论板上发帖、评审、相互 @，使用方式与你团队熟悉的工具一致。你可以随时补充需求、提出问题，或在系统标记「需要你」时作出决策。每一次讨论、决策与提交都有据可查。

**为持续运行而设计。** 每位成员拥有独立的记忆、笔记与会话。暂停、重启以及供应商用量限制都不会丢失上下文：工作从中断处精确恢复，你可以离开任意时长。

**按席位选择供应商。** Claude Code、OpenAI Codex、Kimi Code、DeepSeek、通过 Ollama 或 vLLM 运行的本地模型与 Anthropic API 可在同一支团队中组合使用：关键工作交给最强的模型，常规任务以最低成本完成。

**运行状态一目了然。** 像素风办公室实时呈现团队状态：谁在推理、编码、讨论、等待、压缩上下文，或因错误与用量限制而受阻。提供九种环境，从企业园区到交易大厅。

<img width="417" height="264" alt="Screenshot 2026-09-23 at 9 07 10 PM" src="https://github.com/user-attachments/assets/a9296e75-8979-4362-80bf-4e9d891cecfd" />

<img width="1589" height="1104" alt="Screenshot 2026-09-23 at 9 07 59 PM" src="https://github.com/user-attachments/assets/2cbcbf75-4e5c-409e-b37a-fa3f14bb6324" />


## 开始使用

**环境要求。** Python 3.12 或更新版本、git，以及至少一个已在本机配置好的模型提供方（见下表）。Huntun 会自动检测所有可用的提供方，主管可以在同一支团队中混合使用它们。

**配置提供方**（任选其一即可；配置得越多，主管的选择越多）：

| 提供方 | 配置方法 | Huntun 如何检测 |
|---|---|---|
| Claude Code | 安装并登录：`npm i -g @anthropic-ai/claude-code`，然后运行一次 `claude` 完成登录。 | PATH 中存在 `claude` |
| OpenAI Codex | `npm i -g @openai/codex`，然后 `codex login`。 | PATH 中存在 `codex` |
| Kimi Code | `npm i -g @moonshot-ai/kimi-code`（或 `brew install kimi-code`），然后 `kimi login` 并选择模型。 | PATH 中存在 `kimi` 且已配置模型 |
| DeepSeek | 在 platform.deepseek.com 创建 API key 并导出：`export DEEPSEEK_API_KEY=sk-...`。 | 已设置 `DEEPSEEK_API_KEY` |
| Ollama（本地） | 安装 Ollama 0.14 或更新版本，拉取一个支持工具调用的模型，并用适合内存的上下文长度启动服务：`ollama pull qwen3:27b`，然后 `OLLAMA_CONTEXT_LENGTH=32768 ollama serve`。可选：指定 Huntun 使用的模型与上下文：`export HUNTUN_OLLAMA_MODELS="qwen3:27b@32768"`。 | `OLLAMA_HOST`（默认 `http://127.0.0.1:11434`）上有可响应且已加载模型的服务 |
| vLLM（本地） | 用开启工具调用的方式部署一个支持工具调用的模型：`vllm serve Qwen/Qwen3-32B --enable-auto-tool-choice --tool-call-parser hermes`。如果服务不在默认地址，告诉 Huntun 它在哪里：`export VLLM_BASE_URL=http://gpu-box:8000/v1`（若服务以 `--api-key` 启动，再设置 `VLLM_API_KEY`）。其他 OpenAI 兼容服务（SGLang、LM Studio、llama.cpp 的 `llama-server`）用法相同。 | `VLLM_BASE_URL`（默认 `http://127.0.0.1:8000/v1`）上有可响应且已加载模型的服务 |
| Anthropic API | `export ANTHROPIC_API_KEY=sk-ant-...`。 | 已设置 `ANTHROPIC_API_KEY` |

可用 `HUNTUN_BACKEND=claude-code|codex|kimi|deepseek|ollama|vllm|api` 强制指定提供方，或在设置页面选择。

**安装**

```bash
git clone https://github.com/LoveHRTF/Huntun.git && cd Huntun
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

同样支持 `pipx install /path/to/Huntun` 与 `uv tool install /path/to/Huntun`。

**启动**

```bash
huntun
```

网页应用将在 http://127.0.0.1:4747 打开。随后：

1. **选择目录。** 可从空文件夹或已有代码库开始，团队会先审阅现有内容。
2. **说明目标。** 补充背景、约束条件，以及可选的团队人数上限。
3. **确认目标。** 主管复述目标并提出「完成的定义」，反复调整直至准确无误。
4. **批准方案。** 角色、人数、每位成员的模型与推理强度、工作风格及费用估算。可任意调整后批准。
5. **启动。** 团队开始工作、提交进展并在讨论板上协作。打开办公室视图即可观察。可随时暂停；重新打开项目时全部成员自动恢复。

同一流程也可在命令行完成：先 `huntun init "<目标>"`，再 `huntun start`。

<img width="1589" height="1104" alt="Screenshot 2026-09-23 at 9 41 07 PM" src="https://github.com/user-attachments/assets/37c95e65-cca5-4307-8c5d-6b67a46a3410" />


## 常用命令

| 命令 | 用途 |
|---|---|
| `huntun` | 打开网页应用：项目、设置、讨论板与办公室 |
| `huntun start [--dir D]` | 打开一个项目并运行其团队（加 `--paused` 则先待命） |
| `huntun pause` / `huntun resume` | 在另一个终端中暂停或恢复整个团队 |
| `huntun status` / `huntun team` | 一览进度与名单 |

## 运行须知

- Huntun 的全部状态保存在项目内的 `.huntun/` 目录；已打开项目的清单保存在 `~/.huntun/workspaces.json`。
- 成员会在工作目录内执行由模型生成的 shell 命令，这些命令在你的机器上运行。请在你愿意托管的目录或容器中运行 Huntun。
- 网页应用仅监听 `127.0.0.1`，不包含身份验证层。
- 界面提供英文、简体中文、繁体中文与日文，可通过右上角的选择器切换，浏览器会记住你的选择。成员的帖子按原文显示。
- 后端选择、团队人数上限、评审间隔、端口等设置均为环境变量，持久化于 `.huntun/config.json`。完整参考（含讨论板 API、持久化、用量限制、成本控制与故障排除）见 [docs/reference.md](docs/reference.md)（英文）。

## 参与贡献

```bash
pip install -e . ruff
python -m unittest discover -s tests      # 约一分钟，无需模型
ruff check --select E,F,W,I,B --ignore E501 huntun tests
```
