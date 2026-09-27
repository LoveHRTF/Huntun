# Defaults for the Qwen3.8-Flash-Next server on Windows (RTX 4090 + 64 GB).
# Override any of these in config.local.ps1 next to this file (git-ignored), e.g.  $PARALLEL = 1

# Where llama.cpp, the Python venv and the model live. Put this on the NVMe drive: the N-gram table is read from it at run time.
$FLASHNEXT_HOME = "$env:USERPROFILE\flash-next"

# llama.cpp prebuilt release: "latest" or a tag such as "b10665" (pin one if a newer build misbehaves on Windows, see README).
$LLAMA_TAG = "latest"
$LLAMA_CUDA = "12.4"                    # 12.4 (driver >= 551) or 13.4 (driver >= 580) build of the official Windows binaries

# Model
$HF_REPO = "Navin-Models/Qwen3.8-Flash-Next-Uncensored-AD-4.27-GGUF"
$MODEL_DIR = "$FLASHNEXT_HOME\models\Qwen3.8-Flash-Next-Uncensored-AD-4.27"
$MODEL_SET = ""                         # substring picking one GGUF set when the repo holds several
$WITH_VISION = $false                   # also fetch and load the mmproj vision projector (Huntun does not need it)

# Server
$ALIAS = "qwen3.8-flash-next-uncensored"
$LISTEN = "0.0.0.0"                     # listen on the LAN so the Mac mini can reach it
$PORT = 8080
$API_KEY = ""                           # empty = no auth. Huntun's local provider always sends the key "ollama".
$HUNTUN_HOST_IP = ""                    # the Mac mini's IP; `setup.ps1 tune` limits the firewall rule to it

# Two parallel slots, as in the reference plan. -c is split evenly across slots.
$PARALLEL = 2
$CTX_PER_SLOT = 65536
$KV_UNIFIED = $false                    # $true = the slots share one pool of PARALLEL*CTX_PER_SLOT; any slot may use all of it
$KV_TYPE = "q8_0"

$THREADS = "auto"                       # auto = physical cores (12 on a Ryzen 9 5900X)
# Prompt batch size. "auto" keeps context x batch at 64K x 4096: this model's sparse-attention scorer reserves a
# context x batch float table in VRAM (1 GiB at 64K x 4096, 4 GiB at 256K x 4096), and every GiB it takes pushes
# experts into RAM and slows decode (256K with 4096 measured 5 tok/s instead of 17). Smaller batches cost some prefill.
$BATCH = "auto"
$UBATCH = "auto"
$FIT_TARGET_MIB = 1536                  # VRAM left free by --fit: the Windows desktop runs on the 4090 (the 5900X has no iGPU)
$CACHE_RAM_MIB = 4096
$REASONING_BUDGET = 8192

$SAMPLING_ARGS = @("--temp", "1.0", "--top-p", "0.95", "--top-k", "20", "--min-p", "0.0")
$EXTRA_ARGS = @()
