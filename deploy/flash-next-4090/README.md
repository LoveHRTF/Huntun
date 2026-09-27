# Qwen3.8-Flash-Next on an RTX 4090 + 64 GB

A local, uncensored Flash-Next server for Huntun seats, built from
[`Navin-Models/Qwen3.8-Flash-Next-Uncensored-AD-4.27-GGUF`](https://huggingface.co/Navin-Models/Qwen3.8-Flash-Next-Uncensored-AD-4.27-GGUF)
and llama.cpp's `llama-server`.

## The plan, scaled to this box

The reference plan runs Flash-Next on an RTX 5090 + 128 GB with the per-layer-embedding (N-gram) table offloaded to disk:
two parallel streams, about 60 tok/s total decode, prefill above 4,000 t/s. This kit keeps the same shape
(two parallel slots, N-gram table read from NVMe on demand) on an RTX 4090, Ryzen 9 5900X and 64 GB DDR4:

| | Plan (5090 + 128 GB) | This box, measured (Windows 11) |
|---|---|---|
| Parallel streams | 2 | 2 (64K context each) |
| Decode, 1 stream | | 16.4-16.9 tok/s |
| Decode, 2 streams total | ~60 tok/s | 20.2-20.6 tok/s |
| Decode at 32K context | | 15-19 tok/s (no slowdown with length) |
| Prefill | >4,000 t/s | ~630 t/s |

Decode is bound by DDR4 bandwidth: the experts that do not fit in 24 GB of VRAM live in RAM. Prefill is bound by
reading the N-gram table from disk for every prompt token: with the table in RAM, a 4090 reaches ~1,360 t/s, and
llama.cpp's own numbers show on-demand reads costing about half of that. 64 GB cannot hold the table; 128 GB can, which
is the main lever for prefill on this box (the plan's 128 GB is there for this reason). `bench.py` measures your numbers
and prints them next to both columns.

### Context length

The model supports 262,144 tokens. The context cache is small (~13 KB per token at q8_0, only 12 of 48 layers keep
one), but llama.cpp's sparse-attention scorer reserves a *context x prompt-batch* float table in VRAM: 1 GiB at
64K x 4,096, 4 GiB at 256K x 4,096. Each GiB it takes moves experts from VRAM to RAM, so one 256K slot with 4,096-token
batches measured **5 tok/s** instead of 17. The kit therefore sizes the batch automatically (`BATCH`/`UBATCH` =
`auto`), keeping context x batch at 64K x 4,096:

| Setting (`config.local.ps1` / `flash-next.local.env`) | Context per session | Batch (auto) |
|---|---|---|
| default | 2 x 64K | 4,096 |
| `CTX_PER_SLOT = 131072` | 2 x 128K | 2,048 |
| `CTX_PER_SLOT = 131072` + `KV_UNIFIED` on | up to 256K each, 256K shared | 1,024 |
| `PARALLEL = 1`, `CTX_PER_SLOT = 262144` | 1 x 256K | 1,024 |

Measured with 2 x 128K (batch 2,048): decode 16.8 tok/s for one session, 19.7 total for two, 18.7 at 32K depth, all
the same as 2 x 64K, and 16.7 tok/s at 117K depth (decode does not slow down with length); prefill ~525 t/s on
16K-32K prompts (-17%), ~500 t/s on a 120K prompt (4 minutes), and faster on short ones (484-536 vs ~400 t/s at 4K). With a shared pool, the sessions generating at the same moment must fit in
the pool together; idle sessions are moved to the RAM prompt cache to make room.

Tried on this box and not worth it:

- `UBATCH`/`BATCH` 8192 instead of 4096: prefill +5%, decode -17% (larger compute buffers push experts out of VRAM).
- `--load-mode none` (pinned RAM for the CPU-side experts): fails on Windows with "CUDA error: shared object
  initialization failed", most likely because Windows lets the GPU map pinned memory only up to about half of RAM.

How the memory fits: the model is memory-mapped. `--fit` puts attention, the shared expert, the KV cache and as many
experts as fit into VRAM; the rest of the experts sit in RAM; the ~38 GB N-gram table stays on the NVMe drive and
llama.cpp reads only the rows each token needs (`--lazy-mode on`), with the OS page cache keeping the hot ones.
Free RAM is therefore speed.

## Requirements

- Windows 10/11 or Ubuntu 24.04 (headless Linux is fastest), NVIDIA driver installed (`nvidia-smi` works).
- RTX 4090 on PCIe 4.0 x16, 64 GB RAM, 8-16 GB swap or page file.
- About 100 GB free on an **NVMe** drive for the model and the llama.cpp build.

## Setup on Windows

`windows/` does the same with the official prebuilt llama.cpp CUDA binaries, so nothing is compiled. In PowerShell:

```powershell
git clone https://github.com/LoveHRTF/Huntun.git; cd Huntun\deploy\flash-next-4090\windows
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   # once, to allow local scripts
winget install Python.Python.3.12                     # if Python 3.10+ is missing
.\setup.ps1 check      # GPU/driver, PCIe, RAM, page file, NVMe, Defender
.\setup.ps1 install    # latest llama.cpp Windows CUDA 12.4 build + Python venv
.\setup.ps1 login      # the model repo is gated: accept its terms on Hugging Face first
.\setup.ps1 download   # ~88 GB into %USERPROFILE%\flash-next\models (put FLASHNEXT_HOME on the NVMe drive)
.\serve.ps1            # foreground; chat at http://localhost:8080
& "$env:USERPROFILE\flash-next\venv\Scripts\python.exe" ..\bench.py   # second window
```

To serve the local network (chat from other devices, Huntun on the Mac mini):

```powershell
.\setup.ps1 apikey     # generates a key into config.local.ps1; every client must send it
.\setup.ps1 tune       # as administrator: firewall rule for the local network, no sleep, Defender exclusion
.\setup.ps1 service    # as administrator: start at boot, no sign-in needed (asks for your Windows password)
.\setup.ps1 connect    # prints the chat address and the lines to paste into Huntun's environment
```

`service` runs the server in the background after every reboot; if its log (`%USERPROFILE%\flash-next\server.log`) does
not list the RTX 4090 as a CUDA device, use `.\setup.ps1 task` instead, which starts it when you sign in. After
changing settings, restart it with `Stop-ScheduledTask flash-next; Start-ScheduledTask flash-next`.

Settings live in `windows\config.ps1`; override them in `windows\config.local.ps1` (git-ignored), e.g.
`$FLASHNEXT_HOME = "D:\flash-next"`, `$CTX_PER_SLOT = 131072`, `$ALLOW_FROM = "192.168.1.30"`.

Windows-specific points:

- **Sysmem fallback**: set NVIDIA Control Panel > Manage 3D settings > CUDA - Sysmem Fallback Policy to
  *Prefer No Sysmem Fallback*. Otherwise, when VRAM runs short, the driver silently moves allocations into system RAM
  and generation drops to a crawl instead of failing.
- **The desktop shares the 4090** (the 5900X has no iGPU), so `FIT_TARGET_MIB` defaults to 1536 MiB of headroom, and
  Windows itself keeps several GB of RAM that the N-gram page cache would otherwise use. Close browsers and games
  while agents run. Expect somewhat lower numbers than on a headless Linux install of the same box.
- **llama.cpp issue #28355**: on Windows, builds after b10665 were reported to load the N-gram table badly, making
  prefill extremely slow. If `bench.py` shows prefill far below the expectation, try another build with
  `$LLAMA_TAG = "b10665"` (or a newer tag) in `config.local.ps1` and `.\setup.ps1 install` again; watch RAM use, since
  older builds may not have `--lazy-mode`.
- **Defender** scans every read of the 85 GB model unless the folder is excluded (`tune` does it).
- **Start at logon** uses a scheduled task, so the server runs only while you are signed in; for unattended reboots
  enable automatic sign-in (Sysinternals Autologon).
- WSL2 is not a good fit: it gets half the RAM by default and reads Windows drives slowly.

## Setup on Linux

```bash
git clone https://github.com/LoveHRTF/Huntun.git && cd Huntun/deploy/flash-next-4090
./setup.sh check                  # hardware/OS report, changes nothing
INSTALL_CUDA=1 ./setup.sh deps    # apt build deps + CUDA toolkit 12.8 if nvcc is missing (sudo)
./setup.sh build                  # llama.cpp master with CUDA for sm_89
./setup.sh download               # ~85 GB; lists the GGUF sets in the repo and picks the main (non-MTP) one
./setup.sh tune                   # vm.swappiness=10, prints headless/firewall commands (sudo)
./serve.sh                        # foreground; Ctrl-C to stop
./bench.py                        # in another shell: tool-call check, prefill, decode at 1 and 2 streams
./setup.sh service                # optional: run serve.sh at boot as a systemd service (sudo)
```

Read `$MODEL_DIR/README.md` (the model card, downloaded next to the weights) before the first run. If it asks for a
specific llama.cpp build, set `LLAMA_REF` (a commit, tag, or `pr/NNNNN`) and run `./setup.sh build` again.
If `download` reports several candidate sets, set `MODEL_SET` to a substring of the one you want.

Settings live in `flash-next.env`; put overrides in `flash-next.local.env` (git-ignored), for example:

```bash
PARALLEL=1            # one agent only: full speed for that one stream
CTX_PER_SLOT=131072   # longer context per slot
REASONING_BUDGET=4096 # shorter thinking, faster turns
FIT_TARGET_MIB=1536   # if a desktop session shares the GPU
```

## Connecting Huntun

Huntun has a provider for llama.cpp servers. On the machine running Huntun (the Mac mini), set what
`setup.ps1 connect` / `setup.sh connect` prints, for example:

```bash
export HUNTUN_LLAMACPP_URL=http://192.168.1.20:8080
export HUNTUN_LLAMACPP_KEY=<the API_KEY>
export HUNTUN_LLAMACPP_NOTE="Qwen3.8-Flash-Next uncensored on an RTX 4090: ~17 tok/s ..."
```

Restart Huntun. "llama.cpp server" then appears among the providers on the setup page, and the model
(`qwen3.8-flash-next-uncensored`) among the choices for each seat, next to Claude, Codex and Ollama models. Huntun reads
the model name, the context of one slot and the number of slots from the server, and tells the master how many
sessions it runs at once so it staffs no more seats than that: more agents than slots queue, and they evict each
other's prompt cache, so every turn re-reads the whole context. The note is shown to the master when it staffs the team.

Run `bench.py` once without `--skip-messages` (add `--api-key ...`): it checks the `/v1/messages` tool call that
Huntun relies on. Whether the model thinks is up to the server (Qwen's template thinks by default, capped by
`REASONING_BUDGET`); `HUNTUN_COMPAT_THINKING=on` also asks for it explicitly.

## Security

`llama-server` answers anyone who can reach its port, and without an API key any web page opened on the network could
use it (it allows all origins). Set a key (`setup.ps1 apikey` / `setup.sh apikey`), and keep the firewall to the local
network (`ALLOW_FROM`, default `LocalSubnet`) or to the machines that need it. The chat page asks for the key under
Settings; Huntun sends `HUNTUN_LLAMACPP_KEY`. Never forward the port to the internet.

## Troubleshooting

- **Very slow prefill, fine decode**: the N-gram table is being read inefficiently. Check `./setup.sh check` says the
  model is on NVMe and that there is free RAM for the page cache; see llama.cpp issue #28355 for a related regression.
- **Out of memory at load**: lower `CTX_PER_SLOT` or `UBATCH`, or raise `FIT_TARGET_MIB`. Never add `--mlock`.
- **`/v1/messages` check fails**: update llama.cpp (`./setup.sh build`) and make sure the chat template from the GGUF
  is used (`--jinja` is on by default).
- Logs: `journalctl -u flash-next -f` (Linux service), `Get-Content $env:USERPROFILE\flash-next\server.log -Wait` (Windows task).
