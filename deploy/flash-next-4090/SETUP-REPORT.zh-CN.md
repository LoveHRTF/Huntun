# 在 RTX 4090 主机上部署 Qwen3.8-Flash-Next（去审查版）：流程、架构与结果

[English](SETUP-REPORT.md)

> **与 Huntun 的关系。** Huntun 让一支 AI agent 团队协作开发，每个席位都可以用不同的模型。本文讲的是为其中一部分
> 席位提供服务的本地模型：一台装 RTX 4090 的 Windows 电脑上运行的去审查版 Qwen3.8-Flash-Next。它以 llama.cpp 的
> `llama-server` 形式在局域网里提供服务，Mac mini 上的 Huntun 把它当作 **llama.cpp 服务器** 类型的模型提供方
> （Huntun 顶栏的 **⚙ 模型提供方**）来使用。主管仍然用云端模型（Opus 5.5），一到两个 agent 用这个免费、去审查的
> 本地模型，既节省 token，也能处理云端模型拒绝的任务。部署工具包就在本仓库的
> [`deploy/flash-next-4090/`](README.md)。本文记录第一次部署的过程、实测结果，以及如何复现。

## 概览

| | |
|---|---|
| 硬件 | RTX 4090（24 GB）、Ryzen 9 5900X（12 核）、64 GB DDR4、三星 970 PRO NVMe、PCIe 4.0 x16、Windows 11 专业版 |
| 模型 | [`Navin-Models/Qwen3.8-Flash-Next-Uncensored-AD-4.27-GGUF`](https://huggingface.co/Navin-Models/Qwen3.8-Flash-Next-Uncensored-AD-4.27-GGUF) 的 `-mainline` 版本（33 个文件，88 GiB），服务名 `qwen3.8-flash-next-uncensored` |
| 服务 | llama.cpp 的 `llama-server`（官方 Windows CUDA 12.4 版本），端口 8080，设了 API 密钥，防火墙只放行本地子网，以计划任务 `flash-next` 开机启动 |
| 上下文 | 2 个并行会话共享一个 256K 上下文池（任一会话都能用到模型上限 262,144 token） |
| 实测速度 | 生成（decode）单会话约 17 tok/s，两个会话合计约 20 tok/s；上下文变长不减速（117K 时仍有 16.7 tok/s）。提示词处理（prefill）约 500–630 t/s |
| 对比参考方案 | 参考方案（RTX 5090 + 128 GB）：双路合计约 60 tok/s，prefill 超过 4000 t/s。这台机器的生成速度约为其三分之一，prefill 约为六分之一 |
| 客户端 | 局域网内的浏览器聊天页和 Chatbox（`http://10.0.0.72:8080`），以及 Mac mini 上的 Huntun |

## 1. 起点

**参考方案**（我们讨论开始时引用的那段话）有两种跑法：
- RTX 5090 加 128 GB 内存，把逐层嵌入表（PLE，也就是 "N-gram 表"）放到硬盘上：双并发合计生成约 60 tok/s，prefill
  超过 4000 t/s。
- 双 A100 80G：最多 10 并发。

**问题**是手头这台机器能不能做到：RTX 4090、Ryzen 9 5900X、64 GB DDR4。结论是**能跑，但只是方案一的缩水版**。一到
两个 agent 用没问题，但到不了 60 tok/s 和 4000 t/s：
- **生成受 DDR4 带宽限制。** 显存放不下的专家权重在内存里，每个 token 都要读一遍。双通道 DDR4-3200 约 51 GB/s，
  大约只有 DDR5 的一半。
- **prefill 受 PCIe 4.0 和硬盘限制。** 每批提示词都要把 CPU 侧的专家经 PCIe 搬到显卡。64 GB 内存下约 38 GB 的
  N-gram 表只能留在 NVMe 上，按提示词 token 去读。
- **放得下。** 模型主体 125B，外加 51B 的 N-gram 查找表，每个 token 激活约 6B。AD-4.27 量化约需 54.5 GB "快内存"
  （显存加内存），N-gram 表留在硬盘。

**讨论中确定的要求：**
- **去审查模型。** 默认要 abliterated/去审查版。Navin-Models 的 AD-4.27 GGUF 沿用了 AtomicChat 为 64 GB 机器设计的
  量化方案。
- **双并发。** 和参考方案一样，两个 agent 并行。
- **先跑模型，再接 Huntun。** 先让模型单独跑起来，Huntun 之后再接。
- **Windows。** 这台机器装的是 Windows。不开桌面的 Linux 会快一些；WSL2 不合适，它默认只能用一半内存，读 Windows
  分区也慢。

**在整体规划中的位置：**
- **Mac mini：** 运行 Huntun，主管用 Opus 5.5。
- **本地去审查模型：** 一个或几个，负责部分席位，用来省 token，也处理云端模型拒绝的任务。
- **这台 4090：** 用 Flash-Next 为其中最多两个席位提供服务。

## 2. 架构

```
        Mac mini                                        Windows 电脑（RTX 4090）
 ┌──────────────────────────┐   局域网 HTTP + API 密钥  ┌──────────────────────────────────────────────┐
 │ Huntun（网页应用 :4747）  │ ──────────────────────▶ │ llama-server :8080（计划任务 "flash-next"，   │
 │  主管：Opus 5.5（云端）   │   /v1/messages          │  日志：flash-next\server.log）                │
 │  用 "llama.cpp 服务器"    │   （Anthropic 接口）     │  2 个会话槽，共享 256K KV 池，--jinja         │
 │  模型的席位               │   /v1/models、/props    │                                              │
 └──────────────────────────┘   （发现模型）            │  显存 24 GB：注意力、共享专家、KV 缓存、      │
 局域网内的浏览器 / Chatbox ─────────────────────────▶  │   尽量多的路由专家（--fit）                   │
   （自带聊天页，或 OpenAI 兼容的                      │  内存 64 GB：其余专家（mmap）                 │
    /v1/chat/completions）                            │  NVMe：约 38 GB 的 N-gram 表，按需读取        │
                                                      │   （--lazy-mode on；系统页缓存留住热点行）    │
                                                      │  防火墙：8080 端口只对本地子网开放            │
                                                      └──────────────────────────────────────────────┘
```

**内存怎么分配。** 模型以内存映射方式加载（`--load-mode mmap`）。
- **显存：** `--fit on` 把注意力、共享专家、KV 缓存和尽量多的路由专家放进显存，并留出 `FIT_TARGET_MIB` = 1536 MiB，
  因为 Windows 桌面也跑在这块显卡上（5900X 没有核显）。
- **内存：** 其余专家放在这里。
- **NVMe：** N-gram 表留在硬盘上，llama.cpp 只读取每个 token 用到的那几行（`--lazy-mode on`）。
- **空闲内存就是速度。** 空闲内存会成为这张表的页缓存。
- **上下文缓存很小。** 模型 48 层里只有 12 层（QSA 稀疏注意力层；其余 36 层是 Gated DeltaNet）为每个 token 存缓存，
  q8_0 下约每 token 13 KB。

**服务的启动参数**（由工具包拼出，`serve.ps1` 启动时会在 `Starting:` 行打印完整命令）：

| 参数 | 作用 |
|---|---|
| `--alias qwen3.8-flash-next-uncensored` | 客户端和 Huntun 使用的模型名 |
| `--host 0.0.0.0 --port 8080` | 局域网可访问（由防火墙限制来源） |
| `-np 2 -c 262144 --kv-unified` | 两个会话共享一个 256K 上下文池 |
| `-fa on -ctk q8_0 -ctv q8_0` | Flash attention，8 位 KV 缓存 |
| `-t 12 -tb 12` | 每个物理核一个线程 |
| `-b 1024 -ub 1024` | 批大小自动选择（`BATCH`/`UBATCH` = `auto`），见第 4 节 |
| `--load-mode mmap --lazy-mode on` | 内存映射加载；N-gram 表按需从硬盘读 |
| `--fit on --fit-target 1536` | 自动填满显存，给桌面留 1.5 GB |
| `--cache-ram 4096` | 空闲会话的提示词缓存，占 4 GB 内存 |
| `--reasoning-budget 8192` | 每轮思考 token 上限 |
| `--jinja` | 使用 GGUF 自带的对话模板，工具调用需要它 |
| `--api-key …` | 所有客户端都必须带密钥 |
| `--temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0` | Qwen 推荐的采样参数 |

**各个接口的用途：**

| 接口 | 使用者 |
|---|---|
| `/v1/messages`（Anthropic 兼容） | Huntun 的 agent，用来驱动工具调用循环 |
| `/v1/chat/completions`（OpenAI 兼容） | Chatbox 等聊天应用 |
| `/` | 自带的浏览器聊天页 |
| `/v1/models`、`/props` | Huntun 读取模型名、上下文长度和会话槽数 |
| `/health` | 连通性检查 |

## 3. 部署经过

按时间顺序列出每个遇到的问题和解决办法。所有修正都已经写进工具包，现在按第 5 节操作不会再遇到。

| # | 步骤 | 发生了什么 | 解决 / 结果 |
|---|---|---|---|
| 1 | `setup.ps1 check` | 全部通过：Windows 11 专业版，RTX 4090（驱动 616.92），PCIe 4.0 x16，64 GB 内存，12 核，C 盘是 NVMe，剩余 299 GB。两个警告：Defender 会扫描模型目录；NVIDIA 的 sysmem fallback 设置无法用脚本检查。 | 之后用 `tune` 和 NVIDIA 控制面板处理 |
| 2 | 在命令行里设路径 | 在 PowerShell 里直接输入 `$FLASHNEXT_HOME = "D:\flash-next"`，这对脚本不起作用，文件都装到了默认的 C 盘 `%USERPROFILE%\flash-next`。C 盘本身是 NVMe，所以没有影响。 | 设置要写进 `windows\config.local.ps1` |
| 3 | `setup.ps1 install` | 失败：*"release v0.5.0 has no Windows CUDA 12.4 x64 build"*。GitHub 标记为 "latest" 的是一个没有 Windows 二进制的旧版本。 | 工具包改为选择最新的（包括预发布）且带 `llama-<tag>-bin-win-cuda-12.4-x64.zip` 和 `cudart` 压缩包的版本 |
| 4 | `setup.ps1 download` | *"Could not pick one set automatically"*：仓库里有两套文件，`-main`（34 个文件，90.6 GiB，带实验性的 MTP）和 `-mainline`（33 个文件，88.0 GiB）。 | 工具包优先选 `mainline` 和精确后缀（`MODEL_SET` 仍可指定） |
| 5 | PowerShell 使用问题 | 两条命令粘到了一行（`.\serve.ps1Set-Content …`、`.\setup.ps1 download$py = …`）。在 `Documents\Dev` 而不是工具包的 `windows` 目录下运行（*"not a git repository"*、*"setup.ps1 is not recognized"*）。 | 一行一条命令，在 `deploy\flash-next-4090\windows` 目录下运行 |
| 6 | 下载：受限仓库 | 先是 *401 Unauthorized*，`hf auth login` 之后变成 *403 "not in the authorized list"*。 | 登录 Hugging Face 后在模型页面申请访问，等待批准；用 `.\setup.ps1 login` 登录。工具包现在会解释这两种错误 |
| 7 | 下载：超时 | 88 GiB 权重已下完，但最后那个很小的 `README.md` 一直超时（*"Max retries exceeded"*）。 | 工具包加了重试和更长的超时，模型卡等附加文件失败不影响结果 |
| 8 | 首次启动 | `serve.ps1` 加载成功，8080 端口的聊天能正常回复。 | 可用 |
| 9 | 测速 1 | 生成单会话 16.4 tok/s，双会话合计 20.6，与估计一致；prefill 629 t/s，低于估计的 800–1300。 | 改 NVIDIA 设置 *Prefer No Sysmem Fallback*，重测 |
| 10 | 测速 2 | prefill 632 t/s，生成 16.9 / 20.2，32K 深度时 19.4。这个 prefill 就是这台机器的水平。 | 接受；工具包里的预期值已更正 |
| 11 | 锁页内存（`--load-mode none`） | 加载时崩溃：*"CUDA error: shared object initialization failed"*。很可能是 Windows 只允许 GPU 锁定约一半内存。 | 撤回，并注明 Windows 上不可用 |
| 12 | 批大小 8192（原为 4096） | prefill +5%（676 t/s），生成 −17%（14.1 / 17.0）：更大的计算缓冲区把专家挤出了显存。 | 改回 4096 |
| 13 | 单会话 256K，批大小 4096 | 生成掉到 5.1 tok/s。原因是稀疏注意力的打分器（QSA indexer）在显存里预留一张 "上下文 × 批大小" 的浮点表：256K × 4096 时 4 GiB，64K × 4096 时只有 1 GiB。这把大量专家挤进了内存。 | 工具包改为自动选择批大小，让 "上下文 × 批大小" 保持在 64K × 4096：128K 用 2048，256K 用 1024 |
| 14 | 双会话 × 128K，批大小 2048 | 生成 16.8 / 19.7，117K 深度时仍为 16.7 tok/s；prefill 约 500–525 t/s（−17%）；12 万 token 的提示词读了 4 分钟。 | 长上下文可用，生成不减速 |
| 15 | 最终的上下文方案 | 双会话共享 256K 上下文池（`--kv-unified`，批大小 1024）。任一 agent 都能用到模型上限，只要同一时刻在工作的会话加起来不超过 256K。 | 在用；尚未单独测速（见第 4 节） |
| 16 | 开放给局域网 | `setup.ps1 apikey`，`firewall`/`tune`（管理员，8080 只对本地子网开放），`service`（以计划任务 `flash-next` 开机启动），改设置后用 `restart`。 | 服务地址 `http://10.0.0.72:8080` |
| 17 | 客户端 | 浏览器聊天和 Chatbox（OpenAI API 兼容模式）。Huntun 新增了 "llama.cpp 服务器" 提供方，一开始在项目页配置，现在改在 **⚙ 模型提供方** 里，可以添加多台服务器。 | 已接通 |
| 18 | 图片 | 模型能读图；工具包只在 `WITH_VISION` 开启时才加载视觉投影器（`mmproj`，约 0.9 GB 显存）。 | 保持关闭（Huntun 不需要） |

## 4. 结果

以下数据都来自工具包的 `bench.py`，在这台开着桌面的 Windows 机器上实测。生成速度单位为 tok/s；"深度" 指上下文里
已经有这么多内容时单个请求的生成速度。

| 配置 | prefill 4K / 16K / 32K（t/s） | 生成，单会话 | 生成，双会话合计 | 深度生成 |
|---|---|---|---|---|
| 2 × 64K，批大小 4096，第一次 | 394 / 592 / 629 | 16.4 | 20.6 | 32K 时 14.9 |
| 2 × 64K，批大小 4096，改 sysmem fallback 设置后 | 403 / 632 / 622 | 16.9 | 20.2 | 32K 时 19.4 |
| 2 × 64K，批大小 8192（放弃） | 405 / 666 / 676 | 14.1 | 17.0 | 32K 时 17.6 |
| 1 × 256K，批大小 4096（自动批大小修正前） | 358 / 491 / 547 | 5.1 | 5.4 | 32K 时 4.4 |
| 2 × 128K，批大小 2048（自动） | 484 / 523 / 526 | 16.8 | 19.7 | 32K 时 18.7 |
| 2 × 128K，批大小 2048，12 万 token 提示词 | 4K 时 536，120K 时 497（241.7 秒） | 16.0 | 20.3 | 117K 时 16.7 |
| `--load-mode none`（锁页内存） | Windows 上无法启动 | | | |
| **双会话共享 256K，批大小 1024（当前在用）** | 尚未测 | | | |

**与参考方案对比：**

| | 参考方案（RTX 5090 + 128 GB） | 这台机器 |
|---|---|---|
| 并行会话 | 2 | 2（共享 256K） |
| 生成，双会话合计 | 约 60 tok/s | 约 20 tok/s |
| 生成，单会话 | – | 约 17 tok/s |
| prefill | 4000 t/s 以上 | 约 500–630 t/s |
| 上下文变长是否减速 | "不会" | 已确认到 117K 基本不变 |

**实际使用意味着什么：**
- **速度够用。** 约 17 tok/s，一个 agent 或一个聊天用起来很流畅。
- **两个会话分享速度。** 同时两个会话时，各约 10 tok/s（合计 20）。
- **只有第一轮长提示词慢。** llama.cpp 会保留已处理过的提示词，后续轮次只处理新增内容：5000 token 的工具结果约
  8 秒。2 万 token 的首轮提示约 30 秒；从头读满 256K 需要 7–10 分钟。
- **agent 多于会话槽时要排队。** Huntun 会告诉主管这台服务器同时只跑 2 个会话，所以最多只安排两个席位给它。

**怎样才能更快：**
- **加到 128 GB 内存。** N-gram 表就能放进内存。有人报告 4090 在表放内存时 prefill 约 1360 t/s，大约翻倍，这也是
  参考方案要求 128 GB 的原因。生成速度基本不变，因为瓶颈在 DDR4。
- **不开桌面的 Linux。** 桌面不再占用显存和内存，锁页内存选项也可能可用，但收益不确定。
- **软件设置已经没有可调的了。** 更大的批大小和锁页内存都试过（见第 3 节）。

**尚未完成的：**
- **共享 256K 池的速度。** 还没测；它用更小的批大小（1024），prefill 预计比 2 × 128K 略低。
- **工具调用检查。** 我们的对话里一直没有反馈 `/v1/messages` 工具调用的测试结果（`bench.py` 去掉
  `--skip-messages`）。在 Huntun 里依赖它之前，请先跑一次（第 9 步）。

## 5. 复现步骤（Windows）

所有命令都在 **Windows PowerShell** 里一行一条执行；除非另有说明，都在工具包的 `windows` 目录下运行。标 **[管理员]**
的步骤需要 *以管理员身份* 打开的 PowerShell。

### 0. 准备

- **系统：** Windows 10/11，NVIDIA 驱动要支持 CUDA 12.4（能运行 `nvidia-smi`）。
- **硬盘：** 一块 **NVMe** 硬盘上约 100 GB 空闲空间。
- **内存：** 建议页面文件 8–16 GB；这台机器用 4 GB 也能跑。
- **工具：** git，以及 Python 3.10 或更新版本（`winget install Python.Python.3.12`）。
- **Hugging Face 访问权限：** 模型仓库是受限的。
  - 登录 huggingface.co，打开
    [模型页面](https://huggingface.co/Navin-Models/Qwen3.8-Flash-Next-Uncensored-AD-4.27-GGUF)，申请访问。
  - 等待批准。批准之前下载会报 403。
- **NVIDIA 设置：** *NVIDIA 控制面板 → 管理 3D 设置 → 全局设置 → CUDA - 系统内存回退策略（Sysmem Fallback
  Policy）→ 首选无系统内存回退（Prefer No Sysmem Fallback）→ 应用*。新版 NVIDIA App 里在 "图形 → 全局设置"。不设的
  话，显存不够时驱动会悄悄借用系统内存，速度会变得非常慢。

### 1. 获取工具包

```powershell
git clone https://github.com/LoveHRTF/Huntun.git
```
```powershell
cd Huntun\deploy\flash-next-4090\windows
```
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### 2. 写好设置（写进文件，不要在命令行里设）

设置写在 `config.ps1` 旁边的 `config.local.ps1` 里，直接在命令行输入的变量不起作用。复现现在在用的配置（双会话共享
256K）：

```powershell
Set-Content config.local.ps1 '$CTX_PER_SLOT = 131072', '$KV_UNIFIED = $true'
```

如果 `%USERPROFILE%` 不在 NVMe 盘上，再加上模型和 llama.cpp 的存放位置：

```powershell
Add-Content config.local.ps1 '$FLASHNEXT_HOME = "D:\flash-next"'
```

其他上下文方案：

| `config.local.ps1` 里的内容 | 上下文 |
|---|---|
| （空） | 2 个会话 × 64K |
| `$CTX_PER_SLOT = 131072` | 2 个会话 × 128K |
| `$CTX_PER_SLOT = 131072` 和 `$KV_UNIFIED = $true` | 2 个会话共享 256K |
| `$PARALLEL = 1` 和 `$CTX_PER_SLOT = 262144` | 1 个会话 × 256K |

`Set-Content` 会覆盖整个文件。第 7 步写入 API 密钥之后，再改设置请用 `Add-Content`，或者用文本编辑器修改。

### 3. 检查机器（不做任何修改）

```powershell
.\setup.ps1 check
```

标 `[FAIL]` 的项必须解决。两条 `[warn]`（Defender、sysmem fallback）在第 0 步和第 7 步处理。

### 4. 安装 llama.cpp 和 Python 工具

```powershell
.\setup.ps1 install
```

它会把最新的、带 Windows CUDA 12.4 二进制的官方 llama.cpp 下载到 `%USERPROFILE%\flash-next\llama.cpp`，并建一个装有
`huggingface_hub` 的 Python 虚拟环境。要固定某个版本，就在 `config.local.ps1` 里加 `$LLAMA_TAG = "b10665"`（或其他
版本号）再运行一次。

### 5. 登录 Hugging Face 并下载模型

```powershell
.\setup.ps1 login
```
```powershell
.\setup.ps1 download
```

- **登录：** 会在浏览器里打开设备码登录。
- **下载：** 约 88 GiB，存到 `%USERPROFILE%\flash-next\models\Qwen3.8-Flash-Next-Uncensored-AD-4.27`（约 35 MB/s
  时需要 45 分钟左右）。
- **正常输出：** 列出两套文件，并显示 `Selected: …-mainline (33 file(s))`。
- **中途断了，** 重新运行即可，已下载完的文件会保留。
- **只有最后的模型卡下载失败时，** 模型本身已经完整。

### 6. 首次启动与测速

在前台启动服务：

```powershell
.\serve.ps1
```

第一次加载要几分钟，`Starting:` 那一行会打印完整命令。然后在 **另一个** PowerShell 窗口里：

```powershell
& "$env:USERPROFILE\flash-next\venv\Scripts\python.exe" ..\bench.py --skip-messages
```

- **跑两次。** 启动后第一次测试要从硬盘冷读 N-gram 表。
- **这台机器上的预期：** 生成约 17 tok/s（单会话）和 20 tok/s（双会话），prefill 约 500–630 t/s。
- **长上下文：** 加上 `--sizes 4096,120000` 可以测上下文里已有 12 万 token 时的速度（光读提示词就要约 4 分钟）。

进入第 8 步之前，在前台窗口按 Ctrl+C 停掉服务。

### 7. 开放给局域网

生成 API 密钥（写入 `config.local.ps1`）：

```powershell
.\setup.ps1 apikey
```

然后 **[管理员]**：

```powershell
.\setup.ps1 tune
```

`tune` 做三件事：
- 为 8080 端口加一条只对本地子网开放的防火墙规则（想只放行指定地址，设置 `$ALLOW_FROM`）。
- 删除 Windows 为 `llama-server` 建的 "阻止" 规则（如果有）。
- 关闭睡眠，并把模型目录加入 Defender 排除项。

### 8. 后台运行

**[管理员]**：

```powershell
.\setup.ps1 service
```

- **作用：** 注册计划任务 `flash-next`，开机即启动，不需要登录。它会要求输入 Windows 密码。
- **如果运行不正常：** 如果 `%USERPROFILE%\flash-next\server.log` 里没有列出 RTX 4090 作为 CUDA 设备，改用
  `.\setup.ps1 task`，它在你登录时启动。
- **每次改设置之后**（**[管理员]**）：

```powershell
.\setup.ps1 restart
```

- **查看日志：**

```powershell
Get-Content "$env:USERPROFILE\flash-next\server.log" -Tail 30 -Wait
```

### 9. 连接客户端

打印地址和要发给客户端的设置：

```powershell
.\setup.ps1 connect
```

它会打印 `http://<局域网地址>:8080`（这里是 `http://10.0.0.72:8080`）和密钥。

- **检查连通：** 在另一台设备上打开 `http://<地址>:8080/health`，应显示 `{"status":"ok"}`。
- **浏览器：** 打开该地址，在 *Settings → API Key* 里填密钥。
- **Chatbox：**
  1. *设置 → 模型提供方 → 添加自定义提供方*。
  2. API 模式：**OpenAI API 兼容**。
  3. API 主机：`http://<地址>:8080`。`http://` 要自己写上，否则 Chatbox 会当成 https。
  4. API 路径：留空。
  5. API 密钥：填密钥。
  6. 模型：点 **获取** 得到 `qwen3.8-flash-next-uncensored`。
- **Huntun**（Mac mini，当前 main 分支）：
  1. 点顶栏的 **⚙ 模型提供方**，然后点 **+ 添加服务器**。
  2. 类型：*llama.cpp 服务器*。名称：例如 "4090 主机"。地址：`http://<地址>:8080`。API 密钥：填密钥。
  3. 可选的给主管的说明，例如 *"RTX 4090 上的 Qwen3.8-Flash-Next 去审查版：单会话约 17 tok/s，双会话合计约 20，
     prefill 约 500 t/s；适合实现类任务和云端模型拒绝的任务"*。
  4. **测试** 会显示模型、上下文和 "同时 2 个会话"；**保存** 后所有席位（包括主管）都能选它。
  5. 也可以在启动 Huntun 前导出 `HUNTUN_LLAMACPP_URL`、`HUNTUN_LLAMACPP_KEY` 和 `HUNTUN_LLAMACPP_NOTE`。
- **在 Huntun 里依赖它之前，** 先确认 `/v1/messages` 的工具调用正常。在 4090 的 `windows` 目录下：

```powershell
& "$env:USERPROFILE\flash-next\venv\Scripts\python.exe" ..\bench.py --api-key <你的密钥>
```

输出的第 1 部分必须出现 `tool_use: get_time(...)`。

### 10. 可选项与日常维护

- **图片：**
  1. `Add-Content config.local.ps1 '$WITH_VISION = $true'`，再运行 `.\setup.ps1 download`（会下载 `mmproj` 投影器）。
  2. `.\setup.ps1 restart` **[管理员]**。
  3. 在 Chatbox 里为该模型打开 *能力 → 视觉*。
  4. 代价：约 0.9 GB 显存（生成速度慢几个百分点），每张图增加 1000–4000 个提示词 token。
- **更换 API 密钥：**
  1. `.\setup.ps1 apikey new`，然后 `.\setup.ps1 restart` **[管理员]**。
  2. 更新 Chatbox 和 Huntun 里的密钥。
  3. 不要把密钥贴到聊天或提交里。防火墙只放行局域网，但它仍然是机密。
- **更新 llama.cpp：** 重新运行 `.\setup.ps1 install`，然后 `.\setup.ps1 restart`。如果更新后 prefill 变得极慢，
  固定一个旧的 `$LLAMA_TAG`（参见 llama.cpp issue #28355）。
- **绝不要把 8080 端口映射到公网。**

### Linux 版本

同一个工具包也能在 Ubuntu 24.04 上运行。它会为 sm_89 编译 llama.cpp，而不是下载二进制；设置写在
`flash-next.local.env` 里。在 `deploy/flash-next-4090` 目录下运行：

```bash
./setup.sh check
INSTALL_CUDA=1 ./setup.sh deps
./setup.sh build
./setup.sh login
./setup.sh download
./setup.sh apikey
./setup.sh tune
./serve.sh                        # 前台运行；在另一个终端运行 ./bench.py
./setup.sh service                # 以 systemd 服务开机启动
./setup.sh connect
```

## 6. 故障排查（都是这次部署中实际遇到的）

| 现象 | 原因 | 处理 |
|---|---|---|
| `release vX has no Windows CUDA 12.4 x64 build` | GitHub 的 "latest" 版本没有 Windows 二进制 | `git pull`（工具包现在会找到合适的版本），或固定 `$LLAMA_TAG` |
| `Could not pick one set automatically` | 仓库里有多套 GGUF 文件 | `git pull`，或设置 `$MODEL_SET = "mainline"` |
| `401 Unauthorized` / `GatedRepoError` | 没有登录 Hugging Face | `.\setup.ps1 login` |
| `403 … not in the authorized list` | 访问权限还没批下来 | 在模型页面申请访问并等待批准 |
| 下载 `README.md` 时 `The read operation timed out` | Hugging Face 上的小文件响应慢 | 重新运行下载；权重已经下好了 |
| `The term '.\serve.ps1Set-Content' is not recognized` | 两条命令粘到了一行 | 一行一条命令 |
| `setup.ps1 is not recognized`、`not a git repository` | 目录不对 | `cd …\Huntun\deploy\flash-next-4090\windows` |
| 设置好像没生效 | 在命令行里设的变量，而不是写进文件 | 写进 `config.local.ps1` |
| 一切都很慢、显存 "满了" | 系统内存回退，或桌面占用显存 | 设 *Prefer No Sysmem Fallback*；关掉游戏和浏览器；调大 `$FIT_TARGET_MIB` |
| 长上下文时生成只有约 5 tok/s | 批大小相对上下文太大（打分器的表） | `$BATCH`/`$UBATCH` 保持 `auto` |
| `CUDA error: shared object initialization failed` | Windows 上用了 `--load-mode none`（锁页内存） | 从 `$EXTRA_ARGS` 里去掉 |
| 其他设备连不上 | 防火墙或监听地址 | 在该设备上访问 `/health`；**[管理员]** 运行 `.\setup.ps1 firewall`；`Starting:` 行里必须有 `--host 0.0.0.0` |
| 客户端报 `401 Invalid API Key` | 密钥不一致 | 从 `config.local.ps1` 重新复制密钥 |
| 第二个会话要等第一个说完才有回应 | `$PARALLEL = 1` | 改回两个会话槽（需要长上下文就用共享 256K） |

## 7. 工具包里的文件

| 文件 | 作用 |
|---|---|
| [`README.md`](README.md) | 工具包说明：内存布局、上下文方案、Windows 与 Linux 部署、安全 |
| `windows/config.ps1` | Windows 默认设置；在 `windows/config.local.ps1`（不进 git）里覆盖 |
| `windows/setup.ps1` | `check`、`install`、`login`、`download`、`apikey`、`firewall`、`tune`、`task`、`service`、`restart`、`connect`、`all` |
| `windows/serve.ps1` | 根据设置拼出 `llama-server` 命令并启动 |
| `flash-next.env`、`setup.sh`、`serve.sh` | Linux 下的对应文件 |
| `fetch_model.py` | 列出 GGUF 文件组、自动选择、带重试下载、解释受限仓库错误 |
| `bench.py` | 工具调用检查、指定长度的 prefill、单双会话及深度生成速度，并与参考方案对比 |
