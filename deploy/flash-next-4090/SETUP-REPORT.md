# Setting up Qwen3.8-Flash-Next (uncensored) on the RTX 4090 box: process, architecture, results

[中文版](SETUP-REPORT.zh-CN.md)

> **How this relates to Huntun.** Huntun runs a team of AI agents; each seat can be given a different model. This
> document covers the local model that serves some of those seats: an uncensored Qwen3.8-Flash-Next running on a
> Windows PC with an RTX 4090. It runs as a `llama-server` (llama.cpp) on the local network, and Huntun on the Mac mini
> reaches it as a **llama.cpp server** model provider (**⚙ Model providers** in Huntun's header). The master stays on a
> cloud model (Opus 5.5). One or two agents use this free, uncensored local model, which saves tokens and handles
> tasks that cloud models refuse. The setup kit lives in this repository, in [`deploy/flash-next-4090/`](README.md);
> this report records how it was set up the first time, what was measured, and how to reproduce it.

## At a glance

| | |
|---|---|
| Hardware | RTX 4090 (24 GB), Ryzen 9 5900X (12 cores), 64 GB DDR4, Samsung 970 PRO NVMe, PCIe 4.0 x16, Windows 11 Pro |
| Model | [`Navin-Models/Qwen3.8-Flash-Next-Uncensored-AD-4.27-GGUF`](https://huggingface.co/Navin-Models/Qwen3.8-Flash-Next-Uncensored-AD-4.27-GGUF), set `-mainline` (33 files, 88 GiB), served as `qwen3.8-flash-next-uncensored` |
| Server | llama.cpp `llama-server` (official Windows CUDA 12.4 build), port 8080, API key, firewall limited to the local subnet, starts as the scheduled task `flash-next` |
| Context | 2 parallel sessions sharing one 256K pool (either session can grow to the model's 262,144-token maximum) |
| Measured speed | With the RAM at DDR4-3200: decode 22 tok/s for one session, 26 tok/s total for two; does not slow down with context length. Prefill ~430–480 t/s with the shared 256K pool (~500–630 with larger batches). At first the RAM ran at 2133 MT/s: 17 and 20 tok/s |
| Against the reference plan | Plan (RTX 5090 + 128 GB): ~60 tok/s total for two streams, prefill >4,000 t/s. This box reaches about a third of the decode and a sixth of the prefill |
| Clients | Browser chat and Chatbox on the LAN (`http://10.0.0.72:8080`), and Huntun on the Mac mini |

## 1. Starting point

**The reference plan** (quoted at the start of our discussion) ran Qwen3.8-Flash-Next in one of two ways:
- An RTX 5090 with 128 GB RAM, with the per-layer-embedding (PLE, the "N-gram table") offloaded to disk: two parallel streams, about 60 tok/s decode in total, prefill above 4,000 t/s.
- Two A100 80 GB: up to 10 streams.

**The question** was whether a box already at hand could do the same: an RTX 4090, a Ryzen 9 5900X and 64 GB DDR4. The
answer was **yes, as a scaled-down version of the first plan**. It is fine for one or two agents, but it can't reach
60 tok/s or 4,000 t/s:
- **Decode is limited by DDR4 bandwidth.** The experts that don't fit in 24 GB of VRAM live in RAM and are read for
  every token. Dual-channel DDR4-3200 gives about 51 GB/s, roughly half of DDR5.
- **Prefill is limited by PCIe 4.0 and the disk.** The CPU-side experts are copied to the GPU for each prompt batch,
  and on 64 GB the ~38 GB N-gram table has to stay on the NVMe drive and be read per prompt token.
- **The model fits.** It has a 125B body plus a 51B N-gram lookup table, with about 6B active parameters per token.
  The AD-4.27 quantization needs about 54.5 GB of "fast" memory (VRAM plus RAM), and the table stays on disk.

**Requirements from the discussion:**
- **Uncensored model.** An abliterated/uncensored build is the default. Navin-Models' AD-4.27 GGUF copies AtomicChat's
  quantization for 64 GB machines.
- **Two sessions.** Two parallel agents, as in the plan.
- **Model first, then Huntun.** First get the model running on its own; connect Huntun later.
- **Windows.** The box runs Windows. Headless Linux would be somewhat faster; WSL2 is a poor fit because it gets half
  the RAM by default and reads Windows drives slowly.

**Where it fits in the overall plan:**
- **Mac mini:** hosts Huntun. The master runs on Opus 5.5.
- **Local uncensored models:** one or more take a few seats, to save tokens and to handle work that cloud models refuse.
- **This 4090 box:** serves Flash-Next for up to two of those seats.

## 2. Architecture

```
        Mac mini                                        Windows PC (RTX 4090)
 ┌──────────────────────────┐   LAN, HTTP + API key   ┌──────────────────────────────────────────────┐
 │ Huntun (web app :4747)   │ ──────────────────────▶ │ llama-server :8080  (scheduled task          │
 │  master: Opus 5.5 (cloud)│   /v1/messages          │  "flash-next", log: flash-next\server.log)   │
 │  seats on "llama.cpp     │   (Anthropic API)       │  2 slots, shared 256K KV pool, --jinja       │
 │  server" models          │   /v1/models, /props    │                                              │
 └──────────────────────────┘   (discovery)           │  GPU 24 GB: attention, shared expert, KV     │
 Browser / Chatbox on the LAN ─────────────────────▶  │   cache, as many experts as fit (--fit)      │
   (built-in chat page, or OpenAI-compatible          │  RAM 64 GB: the remaining experts (mmap)     │
    /v1/chat/completions)                             │  NVMe: ~38 GB N-gram table, read on demand   │
                                                      │   (--lazy-mode on; OS page cache keeps hot   │
                                                      │   rows)                                      │
                                                      │  Firewall: port 8080 from LocalSubnet only   │
                                                      └──────────────────────────────────────────────┘
```

**How the memory is used.** The model is memory-mapped (`--load-mode mmap`).
- **GPU:** `--fit on` puts attention, the shared expert, the KV cache and as many routed experts as fit into VRAM. It
  leaves `FIT_TARGET_MIB` = 1536 MiB free, because the Windows desktop also runs on this GPU (the 5900X has no
  integrated graphics).
- **RAM:** the remaining experts sit here.
- **NVMe:** the N-gram table stays on the drive; llama.cpp reads only the rows each token needs (`--lazy-mode on`).
- **Free RAM is speed.** It becomes page cache for the table.
- **Context cache is cheap.** Only 12 of the model's 48 layers (the QSA sparse-attention layers; the other 36 are Gated
  DeltaNet) keep a per-token cache, about 13 KB per token at q8_0.

**How the server is started** (the kit builds this command; `serve.ps1` prints it as the `Starting:` line):

| Flag | Why |
|---|---|
| `--alias qwen3.8-flash-next-uncensored` | The model name clients and Huntun send |
| `--host 0.0.0.0 --port 8080` | Reachable from the LAN (the firewall limits who) |
| `-np 2 -c 262144 --kv-unified` | Two sessions sharing one 256K context pool |
| `-fa on -ctk q8_0 -ctv q8_0` | Flash attention, 8-bit KV cache |
| `-t 12 -tb 12` | One thread per physical core |
| `-b 1024 -ub 1024` | Batch picked automatically (`BATCH`/`UBATCH` = `auto`), see section 4 |
| `--load-mode mmap --lazy-mode on` | Memory-mapped model; the N-gram table is read from disk on demand |
| `--fit on --fit-target 1536` | Fill VRAM automatically, leave 1.5 GB for the desktop |
| `--cache-ram 4096` | 4 GB RAM cache for idle sessions' prompts |
| `--reasoning-budget 8192` | Caps thinking tokens per turn |
| `--jinja` | Chat template from the GGUF; needed for tool calls |
| `--api-key …` | Every client must send the key |
| `--temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0` | Qwen's recommended sampling |

**What each endpoint is used for:**

| Endpoint | Used by |
|---|---|
| `/v1/messages` (Anthropic-compatible) | Huntun's agents; it drives the tool-use loop |
| `/v1/chat/completions` (OpenAI-compatible) | Chatbox and other chat apps |
| `/` | The built-in browser chat page |
| `/v1/models`, `/props` | Huntun: model name, context and the number of slots |
| `/health` | Connectivity checks |

## 3. How the setup went

This is the order in which things happened, with every problem hit and how it was fixed. Each fix went into the kit, so
following section 5 today avoids them.

| # | Step | What happened | Fix / outcome |
|---|---|---|---|
| 1 | `setup.ps1 check` | Everything OK: Windows 11 Pro, RTX 4090 with driver 616.92, PCIe 4.0 x16, 64 GB RAM, 12 cores, C: is an NVMe drive with 299 GB free. Two warnings: Defender scans the model folder, and the NVIDIA sysmem fallback setting can't be checked from a script. | Handled later with `tune` and the NVIDIA Control Panel setting |
| 2 | Paths set in the shell | `$FLASHNEXT_HOME = "D:\flash-next"` was typed into PowerShell, which has no effect on the scripts. Everything went to the default `%USERPROFILE%\flash-next` on C:. C: is NVMe, so no harm done. | Settings belong in `windows\config.local.ps1` |
| 3 | `setup.ps1 install` | Failed: *"release v0.5.0 has no Windows CUDA 12.4 x64 build"*. GitHub's "latest" release was an old tag without Windows binaries. | The kit now picks the newest release (pre-releases included) that has `llama-<tag>-bin-win-cuda-12.4-x64.zip` and the `cudart` zip |
| 4 | `setup.ps1 download` | *"Could not pick one set automatically"*: the repo holds two sets, `-main` (34 files, 90.6 GiB, experimental attached MTP) and `-mainline` (33 files, 88.0 GiB). | The kit prefers `mainline` and exact suffixes (`MODEL_SET` still overrides) |
| 5 | PowerShell pitfalls | Two commands pasted on one line (`.\serve.ps1Set-Content …`, `.\setup.ps1 download$py = …`). Commands run from `Documents\Dev` instead of the kit's `windows` folder (*"not a git repository"*, *"setup.ps1 is not recognized"*). | Run one command per line, from `deploy\flash-next-4090\windows` |
| 6 | Download, gated repo | *401 Unauthorized*, then after `hf auth login` *403 "you are not in the authorized list"*. | Request access on the model page while signed in to Hugging Face, and wait for approval. Log in with `.\setup.ps1 login`. The kit now explains both errors |
| 7 | Download, time-outs | The 88 GiB of weights arrived, but the small `README.md` kept timing out at the end (*"Max retries exceeded"*). | The kit retries with longer time-outs and treats the model card and other extras as optional |
| 8 | First start | `serve.ps1` loaded the model; the chat on port 8080 answered. | Working |
| 9 | Benchmark 1 | Decode 16.4 tok/s (one session) and 20.6 total (two): as estimated. Prefill 629 t/s, below the 800–1,300 estimate. | NVIDIA *Prefer No Sysmem Fallback*, then measure again |
| 10 | Benchmark 2 | Prefill 632 t/s, decode 16.9 / 20.2, and 19.4 at 32K depth. Prefill is simply what this box does. | Accepted; the estimate in the kit was corrected |
| 11 | Pinned memory (`--load-mode none`) | Crashed at load: *"CUDA error: shared object initialization failed"*. Most likely Windows lets the GPU pin only about half of RAM. | Reverted, and documented as not working on Windows |
| 12 | Batch 8192 instead of 4096 | Prefill +5% (676 t/s), decode −17% (14.1 / 17.0). Bigger compute buffers push experts out of VRAM. | Reverted to 4096 |
| 13 | One 256K session, batch 4096 | Decode collapsed to 5.1 tok/s. The sparse-attention scorer (the QSA indexer) reserves a *context × batch* float table in VRAM: 4 GiB at 256K × 4,096, versus 1 GiB at 64K × 4,096. That pushed many experts into RAM. | The kit now sizes the batch automatically to keep context × batch at 64K × 4,096: 2,048 at 128K, 1,024 at 256K |
| 14 | Two sessions × 128K, batch 2048 | Decode 16.8 / 19.7, still 16.7 tok/s at 117K depth. Prefill ~500–525 t/s (−17%). A 120K prompt took 4 minutes. | Long context works without slowing decode |
| 15 | Final context choice | Two sessions sharing a 256K pool (`--kv-unified`, batch 1,024). Either agent can grow to the model's maximum, as long as the sessions working at the same moment fit in 256K together. | Running. Not benchmarked separately (see section 4) |
| 16 | Local network | `setup.ps1 apikey`, `firewall`/`tune` (as administrator, port 8080 from LocalSubnet), `service` (starts at boot as the scheduled task `flash-next`), and `restart` after every settings change. | Server at `http://10.0.0.72:8080` |
| 17 | Clients | Browser chat and Chatbox (OpenAI API Compatible). Huntun got a "llama.cpp server" provider, first configured on the Projects page. It is now **⚙ Model providers**, which can hold several servers. | Connected |
| 18 | Images | The model reads images; the kit loads the vision projector (`mmproj`, ~0.9 GB of VRAM) only with `WITH_VISION`. | Left off (Huntun does not need it) |

## 4. Results

All figures come from the kit's `bench.py` on this box, in Windows with the desktop running. Decode is in tok/s;
"deep" means one request with that much context already in place. Found afterwards: the RAM was running at
2133 MT/s, DDR4's default, because XMP/DOCP was off in the BIOS. Every row but the last was measured like that; the last
is with the RAM raised to 3200 MT/s with the board's automatic timings (XMP/DOCP itself did not boot with these four
mixed dual-rank modules).

| Configuration | Prefill 4K / 16K / 32K (t/s) | Decode, 1 session | Decode, 2 sessions total | Decode deep |
|---|---|---|---|---|
| 2 × 64K, batch 4096, first run | 394 / 592 / 629 | 16.4 | 20.6 | 14.9 at 32K |
| 2 × 64K, batch 4096, after the sysmem fallback change | 403 / 632 / 622 | 16.9 | 20.2 | 19.4 at 32K |
| 2 × 64K, batch 8192 (rejected) | 405 / 666 / 676 | 14.1 | 17.0 | 17.6 at 32K |
| 1 × 256K, batch 4096 (before the auto batch fix) | 358 / 491 / 547 | 5.1 | 5.4 | 4.4 at 32K |
| 2 × 128K, batch 2048 (auto) | 484 / 523 / 526 | 16.8 | 19.7 | 18.7 at 32K |
| 2 × 128K, batch 2048, 120K prompt | 536 at 4K, 497 at 120K (241.7 s) | 16.0 | 20.3 | 16.7 at 117K |
| `--load-mode none` (pinned RAM) | did not start on Windows | | | |
| **2 sessions sharing 256K, batch 1024, RAM at DDR4-3200 (in use now)** | 481 / 431 / 455 | 22.0 (23.2 per request) | 26.1 | 22.9 at 32K |

**Against the plan:**

| | Plan (RTX 5090 + 128 GB) | This box |
|---|---|---|
| Parallel sessions | 2 | 2 (sharing 256K) |
| Decode, 2 sessions total | ~60 tok/s | ~26 tok/s |
| Decode, 1 session | – | ~22 tok/s |
| Prefill | >4,000 t/s | ~430–630 t/s (by batch size) |
| Slows down with context length | "no" | Confirmed flat up to 117K |

**What it means in use:**
- **Responsive enough.** About 22 tok/s is comfortable for one agent or one chat.
- **Two sessions share the speed.** With two at once, each gets about 13 tok/s (26 in total).
- **Only the first long prompt is slow.** llama.cpp keeps a conversation's processed prompt, so later turns only read
  the new text: a 5,000-token tool result takes about 8 s. A 20,000-token first prompt takes 30–45 s; a full 256K
  context read from scratch takes about 10 minutes.
- **More agents than slots queue.** Huntun tells the master that the server runs 2 sessions at once, so it staffs no
  more than two seats on it.

**What would make it faster:**
- **RAM at its rated speed (XMP/DOCP).** Decode reads most of the experts from RAM every token, so it is almost entirely
  bound by RAM bandwidth. 2133 → 3200 MT/s (+50% bandwidth) measured +30% decode: 22.0 tok/s instead of ~17, 26.1 total
  for two instead of ~20. Prefill did not gain. The 27B, which sits in VRAM, is unaffected. Done on this box (section 4).
- **128 GB RAM.** The N-gram table could then stay in memory. A 4090 with the table in RAM was reported at ~1,360 t/s
  prefill, about double. This is why the reference plan specifies 128 GB. Decode would barely change, since it is
  limited by DDR4.
- **Headless Linux.** The desktop would no longer take VRAM and RAM, and the pinned-memory option might work there.
  The gain is uncertain.
- **Nothing left to tune in the software settings.** Larger batches and pinned memory were both tried (section 3).

**Still open:**
- **Tool-call check.** The `/v1/messages` tool-call check (`bench.py` without `--skip-messages`) was never reported
  back in our conversation. Run it once before relying on the model in Huntun (step 9).

## 5. Reproducing it (Windows)

All commands run in **Windows PowerShell**, one per line, from the kit's `windows` folder unless stated otherwise. Steps
marked **[admin]** need a PowerShell opened *as administrator*.

### 0. Before you start

- **System:** Windows 10/11 and an NVIDIA driver recent enough for CUDA 12.4 (`nvidia-smi` works).
- **Disk:** about 100 GB free on an **NVMe** drive.
- **Memory:** turn on XMP/DOCP in the BIOS so the RAM runs at its rated speed (Task Manager → Performance → Memory
  shows it; 2133 MT/s means it is off). A page file of 8–16 GB is recommended; the box ran fine with 4 GB.
- **Tools:** git, and Python 3.10 or newer (`winget install Python.Python.3.12`).
- **Hugging Face access:** the model repo is gated.
  - Sign in at huggingface.co, open
    [the model page](https://huggingface.co/Navin-Models/Qwen3.8-Flash-Next-Uncensored-AD-4.27-GGUF), and request
    access.
  - Wait until it is granted. Before that, downloads fail with 403.
- **NVIDIA setting:** *NVIDIA Control Panel → Manage 3D settings → Global Settings → CUDA - Sysmem Fallback Policy →
  Prefer No Sysmem Fallback → Apply*. The same option is in the NVIDIA App under Graphics → Global settings. Without
  it, a GPU that runs short of memory silently spills into system RAM and gets very slow.

### 1. Get the kit

```powershell
git clone https://github.com/LoveHRTF/Huntun.git
```
```powershell
cd Huntun\deploy\flash-next-4090\windows
```
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### 2. Choose your settings (in a file, not in the shell)

Settings go in `config.local.ps1` next to `config.ps1`. Variables typed into the shell are ignored. To reproduce the
setup in use here (two sessions sharing a 256K pool):

```powershell
Set-Content config.local.ps1 '$CTX_PER_SLOT = 131072', '$KV_UNIFIED = $true'
```

If `%USERPROFILE%` is not on an NVMe drive, add the location of the model and llama.cpp:

```powershell
Add-Content config.local.ps1 '$FLASHNEXT_HOME = "D:\flash-next"'
```

Other context options:

| Lines in `config.local.ps1` | Context |
|---|---|
| (none) | 2 sessions × 64K |
| `$CTX_PER_SLOT = 131072` | 2 sessions × 128K |
| `$CTX_PER_SLOT = 131072` and `$KV_UNIFIED = $true` | 2 sessions sharing 256K |
| `$PARALLEL = 1` and `$CTX_PER_SLOT = 262144` | 1 session × 256K |

`Set-Content` replaces the whole file. After step 7 has added the API key, use `Add-Content` for further changes, or
edit the file in a text editor.

### 3. Check the machine (changes nothing)

```powershell
.\setup.ps1 check
```

Fix anything marked `[FAIL]`. The two `[warn]` lines (Defender, sysmem fallback) are handled in steps 0 and 7.

### 4. Install llama.cpp and the Python tools

```powershell
.\setup.ps1 install
```

This downloads the newest official llama.cpp build with Windows CUDA 12.4 binaries into
`%USERPROFILE%\flash-next\llama.cpp`, and creates a Python venv with `huggingface_hub`. To pin a build, add
`$LLAMA_TAG = "b10665"` (or another tag) to `config.local.ps1` and run the step again.

### 5. Log in to Hugging Face and download the model

```powershell
.\setup.ps1 login
```
```powershell
.\setup.ps1 download
```

- **Login:** it opens a device-code login in the browser.
- **Download:** about 88 GiB into `%USERPROFILE%\flash-next\models\Qwen3.8-Flash-Next-Uncensored-AD-4.27` (about 45
  minutes at ~35 MB/s).
- **Expected output:** it lists the two sets and says `Selected: …-mainline (33 file(s))`.
- **If it stops partway,** run it again; finished files are kept.
- **If only the model card fails** at the end, the model is still complete.

### 6. First start and benchmark

Start the server in the foreground:

```powershell
.\serve.ps1
```

The first load takes a few minutes. The `Starting:` line shows the full command. Then, in a **second** PowerShell
window:

```powershell
& "$env:USERPROFILE\flash-next\venv\Scripts\python.exe" ..\bench.py --skip-messages
```

- **Run it twice.** The first run after a start reads the N-gram table from disk cold.
- **Expected on this box** (RAM at DDR4-3200): decode about 22 tok/s (1 session) and 26 tok/s (2 sessions), prefill
  about 430–630 t/s depending on the batch size.
- **Long context:** add `--sizes 4096,120000` to measure speed with 120K of context in place (the prompt alone takes
  about 4 minutes).

Stop the foreground server with Ctrl+C before step 8.

### 7. Open it to the local network

Create an API key (written into `config.local.ps1`):

```powershell
.\setup.ps1 apikey
```

Then **[admin]**:

```powershell
.\setup.ps1 tune
```

`tune` does three things:
- Adds a firewall rule for port 8080 from the local subnet only (to allow single addresses instead, set `$ALLOW_FROM`).
- Removes any "Block" rule Windows created for `llama-server`.
- Turns off sleep and adds a Defender exclusion for the model folder.

### 8. Run it in the background

**[admin]**:

```powershell
.\setup.ps1 service
```

- **What it does:** registers the scheduled task `flash-next`, which starts the server at boot without anyone signing
  in. It asks for your Windows password.
- **If it misbehaves:** if `%USERPROFILE%\flash-next\server.log` does not list the RTX 4090 as a CUDA device, use
  `.\setup.ps1 task` instead, which starts the server when you sign in.
- **After any settings change** (**[admin]**):

```powershell
.\setup.ps1 restart
```

- **Follow the log:**

```powershell
Get-Content "$env:USERPROFILE\flash-next\server.log" -Tail 30 -Wait
```

### 9. Connect clients

Print the address and the settings to hand out:

```powershell
.\setup.ps1 connect
```

It prints `http://<LAN address>:8080` (here `http://10.0.0.72:8080`) and the key.

- **Check reachability:** on another device, `http://<address>:8080/health` should show `{"status":"ok"}`.
- **Browser:** open the address and enter the key under *Settings → API Key*.
- **Chatbox:**
  1. *Settings → Model Provider → Add Custom Provider*.
  2. API Mode: **OpenAI API Compatible**.
  3. API Host: `http://<address>:8080`. Type the `http://` yourself; without it Chatbox assumes https.
  4. API Path: leave empty.
  5. API Key: the key.
  6. Model: click **Fetch** to get `qwen3.8-flash-next-uncensored`.
- **Huntun** (Mac mini, current main):
  1. Open **⚙ Model providers** in the header, then **+ Add a server**.
  2. Type: *llama.cpp server*. Name: e.g. "4090 box". Address: `http://<address>:8080`. API key: the key.
  3. Optional note for the master, e.g. *"Qwen3.8-Flash-Next uncensored on an RTX 4090: ~22 tok/s for one session, ~26
     total for two, prefill ~450 t/s; good for implementation and tasks cloud models refuse"*.
  4. **Test** shows the model, the context and "2 sessions at once"; **Save** makes it available to every seat, the
     master included.
  5. Alternatively, export `HUNTUN_LLAMACPP_URL`, `HUNTUN_LLAMACPP_KEY` and `HUNTUN_LLAMACPP_NOTE` before starting
     Huntun.
- **Before relying on it in Huntun,** check that tool calls work through `/v1/messages`. From the `windows` folder on
  the 4090:

```powershell
& "$env:USERPROFILE\flash-next\venv\Scripts\python.exe" ..\bench.py --api-key <your key>
```

Section 1 of its output must show a `tool_use: get_time(...)` line.

### 10. Optional and maintenance

- **Images:**
  1. `Add-Content config.local.ps1 '$WITH_VISION = $true'`, then `.\setup.ps1 download` (adds the `mmproj` projector).
  2. `.\setup.ps1 restart` **[admin]**.
  3. In Chatbox, turn on *Capabilities → Vision* for the model.
  4. Cost: about 0.9 GB of VRAM (a few percent of decode), and each image adds 1,000–4,000 prompt tokens.
- **New API key:**
  1. `.\setup.ps1 apikey new`, then `.\setup.ps1 restart` **[admin]**.
  2. Update Chatbox and Huntun.
  3. Keep the key out of chats and commits. The firewall limits exposure to the local network, but it is still a
     secret.
- **Update llama.cpp:** run `.\setup.ps1 install` again, then `.\setup.ps1 restart`. If prefill becomes very slow after
  an update, pin an older `$LLAMA_TAG` (see llama.cpp issue #28355).
- **Never forward port 8080 to the internet.**

### 11. Optional: a second, faster model (Qwen3.8-27B)

Added after the setup above. Qwen3.8-27B (uncensored, Q4_K_M, ~17 GB) fits entirely in VRAM, so it is much faster, at a
lower level of capability (Artificial Analysis index 34 vs 40). Measured on this box with `bench.py` (no MTP):

| | Qwen3.8-27B | Flash-Next | Ratio |
|---|---|---|---|
| Decode, 1 session | 43.9 tok/s | 22.0 tok/s | ~2× |
| Decode, 2 sessions total | 83.3 tok/s (43.5 each) | 26.1 tok/s | ~3.2× |
| Decode, 3 sessions total (the kit's default, sharing 192K) | 108.7 tok/s (41.0 each) | | |
| Decode at 32K / 128K depth | 43.0 / 33.1 tok/s | 22.9 / 16.7 tok/s (128K at DDR4-2133) | |
| Prefill, 4K / 16K / 32K | 2,763 / 2,804 / 2,632 t/s | 481 / 431 / 455 t/s | ~6× |

The `/v1/messages` tool check passes (the model thinks, then calls the tool). A 30,000-token prompt takes ~11 s instead
of 50–65 s; a 131K prompt ~69 s. A 256K pool with Q4_K_M did not fit: everything ran ~2.3× slower (layers moved to the
CPU), so the kit keeps 192K; the smaller IQ4_XS would leave room for 256K.

1. `.\setup.ps1 download 27b` (~17 GB from `orcarouter/Qwen3.8-27B-Uncensored-GGUF`).
2. `.\setup.ps1 restart` **[admin]**. With both models downloaded, the server starts as a llama.cpp **router**: same
   address, port, API key and firewall rule; `/v1/models` lists `qwen3.8-flash-next-uncensored` and
   `qwen3.8-27b-uncensored`, and each request is served by the model it names.
3. Measure it: `bench.py --api-key <your key> --model qwen3.8-27b-uncensored`.
4. In Huntun, **⚙ Model providers** shows both models under the 4090 entry, each with its own context and sessions;
   any seat can use either. Chatbox and the chat page pick the model by name.

The 4090 holds one of them at a time. A request for the other waits until the loaded one finishes its requests, then
the router swaps them (seconds for the 27B, a minute or more for Flash-Next), so keep a team's seats on this machine on
one model. `$DEFAULT_MODEL = "27b"` in `config.local.ps1` makes the 27B the one loaded at startup; the kit README
("A second model") lists the other settings.

### Linux variant

The same kit runs on Ubuntu 24.04. It builds llama.cpp for sm_89 instead of downloading binaries; settings go in
`flash-next.local.env`. Run from `deploy/flash-next-4090`:

```bash
./setup.sh check
INSTALL_CUDA=1 ./setup.sh deps
./setup.sh build
./setup.sh login
./setup.sh download
./setup.sh apikey
./setup.sh tune
./serve.sh                        # foreground; ./bench.py in another shell
./setup.sh service                # systemd service at boot
./setup.sh connect
```

## 6. Troubleshooting (all seen during this setup)

| Symptom | Cause | What to do |
|---|---|---|
| `release vX has no Windows CUDA 12.4 x64 build` | GitHub's "latest" release has no Windows binaries | `git pull` (the kit now finds the right release), or pin `$LLAMA_TAG` |
| `Could not pick one set automatically` | Several GGUF sets in the repo | `git pull`, or set `$MODEL_SET = "mainline"` |
| `401 Unauthorized` / `GatedRepoError` | Not logged in to Hugging Face | `.\setup.ps1 login` |
| `403 … not in the authorized list` | Access not granted yet | Request access on the model page and wait for approval |
| `The read operation timed out` on `README.md` | Hugging Face was slow on the small files | Run the download again; the weights are already there |
| `The term '.\serve.ps1Set-Content' is not recognized` | Two commands pasted on one line | One command per line |
| `setup.ps1 is not recognized`, `not a git repository` | Wrong folder | `cd …\Huntun\deploy\flash-next-4090\windows` |
| Settings seem ignored | Set in the shell instead of in the file | Put them in `config.local.ps1` |
| Everything very slow, GPU memory "full" | Sysmem fallback, or the desktop using VRAM | *Prefer No Sysmem Fallback*; close games and browsers; raise `$FIT_TARGET_MIB` |
| Decode ~5 tok/s at long context | Batch too large for the context (the scorer table) | Keep `$BATCH`/`$UBATCH` at `auto` |
| `CUDA error: shared object initialization failed` | `--load-mode none` (pinned RAM) on Windows | Remove it from `$EXTRA_ARGS` |
| Another device can't connect | Firewall or listen address | `/health` from that device; `.\setup.ps1 firewall` **[admin]**; the `Starting:` line must show `--host 0.0.0.0` |
| `401 Invalid API Key` in a client | Key mismatch | Copy the key again from `config.local.ps1` |
| Second session only answers after the first finishes | `$PARALLEL = 1` | Use two slots (sharing 256K if needed) |

## 7. Files in the kit

| File | Purpose |
|---|---|
| [`README.md`](README.md) | Reference for the kit: memory layout, context options, Windows and Linux setup, security |
| `windows/config.ps1` | Windows defaults; override in `windows/config.local.ps1` (git-ignored) |
| `windows/setup.ps1` | `check`, `install`, `login`, `download` (`download 27b` for the second model), `apikey`, `firewall`, `tune`, `task`, `service`, `restart`, `connect`, `all` |
| `windows/serve.ps1` | Builds the `llama-server` command from the settings and starts it; with both models downloaded, writes the router's model presets and starts it as a router |
| `flash-next.env`, `setup.sh`, `serve.sh` | The same for Linux |
| `fetch_model.py` | Lists the GGUF sets, picks one, downloads with retries, explains gated-repo errors |
| `bench.py` | Tool-call check, prefill at chosen sizes, decode at 1 and 2 sessions and at depth, compared against the plan (`--model` picks the model on a router) |
