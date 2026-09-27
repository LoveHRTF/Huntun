# Qwen3.8-Flash-Next on an RTX 4090 + 64 GB

A local, uncensored Flash-Next server for Huntun seats, built from
[`Navin-Models/Qwen3.8-Flash-Next-Uncensored-AD-4.27-GGUF`](https://huggingface.co/Navin-Models/Qwen3.8-Flash-Next-Uncensored-AD-4.27-GGUF)
and llama.cpp's `llama-server`.

## The plan, scaled to this box

The reference plan runs Flash-Next on an RTX 5090 + 128 GB with the per-layer-embedding (N-gram) table offloaded to disk:
two parallel streams, about 60 tok/s total decode, prefill above 4,000 t/s. This kit keeps the same shape
(two parallel slots, N-gram table read from NVMe on demand) on an RTX 4090, Ryzen 9 5900X and 64 GB DDR4:

| | Plan (5090 + 128 GB) | This box (expected) |
|---|---|---|
| Parallel streams | 2 | 2 (64K context each) |
| Decode, 1 stream | | 15-20 tok/s |
| Decode, 2 streams total | ~60 tok/s | 19-25 tok/s |
| Prefill | >4,000 t/s | 800-1,300 t/s |

Decode is bound by DDR4 bandwidth (the experts that do not fit in 24 GB of VRAM live in RAM) and prefill by PCIe 4.0
(large batches stream those experts to the GPU). `bench.py` measures the real numbers and prints them next to both columns.

How the memory fits: the model is memory-mapped. `--fit` puts attention, the shared expert, the KV cache and as many
experts as fit into VRAM; the rest of the experts sit in RAM; the ~38 GB N-gram table stays on the NVMe drive and
llama.cpp reads only the rows each token needs (`--lazy-mode on`), with the Linux page cache keeping the hot ones.
Free RAM is therefore speed: run the box headless.

## Requirements

- Ubuntu 24.04 (headless recommended), NVIDIA driver installed (`nvidia-smi` works).
- RTX 4090 on PCIe 4.0 x16, 64 GB RAM, 8-16 GB swap.
- About 100 GB free on an **NVMe** drive for the model and the llama.cpp build.

## Setup

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

On the machine running Huntun (the Mac mini):

```bash
export OLLAMA_HOST=http://<4090-box-ip>:8080
export HUNTUN_OLLAMA_MODELS="qwen3.8-flash-next-uncensored@65536"
```

Huntun's local provider speaks the Anthropic Messages API, which `llama-server` serves at `/v1/messages`
(`bench.py` checks exactly this path with a tool call). The context after `@` must match `CTX_PER_SLOT`.
Give the local model at most `PARALLEL` seats: more agents than slots queue, and they evict each other's prompt
cache, so every turn re-reads the whole context. Huntun reads a single `OLLAMA_HOST`, so a second local server
(for example a model on the Mac mini itself) needs a router in front of both until Huntun supports several hosts.

Whether the model thinks is up to the server (Qwen's template thinks by default, capped by `REASONING_BUDGET`);
`HUNTUN_COMPAT_THINKING=on` also asks for it explicitly.

## Security

`llama-server` listens on the LAN without authentication by default. Allow only the Huntun host
(`sudo ufw allow from <mac-mini-ip> to any port 8080 proto tcp`). Huntun always sends the key `ollama`, so setting
`API_KEY=ollama` adds a speed bump but no real protection.

## Troubleshooting

- **Very slow prefill, fine decode**: the N-gram table is being read inefficiently. Check `./setup.sh check` says the
  model is on NVMe and that there is free RAM for the page cache; see llama.cpp issue #28355 for a related regression.
- **Out of memory at load**: lower `CTX_PER_SLOT` or `UBATCH`, or raise `FIT_TARGET_MIB`. Never add `--mlock`.
- **`/v1/messages` check fails**: update llama.cpp (`./setup.sh build`) and make sure the chat template from the GGUF
  is used (`--jinja` is on by default).
- Logs of the service: `journalctl -u flash-next -f`.
