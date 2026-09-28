# Defaults for the local model server on Windows (RTX 4090 + 64 GB): Qwen3.8-Flash-Next and, optionally, Qwen3.8-27B.
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
$API_KEY = ""                           # required key for every client; `setup.ps1 apikey` generates one into config.local.ps1
$ALLOW_FROM = "LocalSubnet"             # who the firewall lets in: LocalSubnet, or comma-separated IPs such as "192.168.1.30"

# Two parallel slots, as in the reference plan. -c is split evenly across slots.
$PARALLEL = 2
$CTX_PER_SLOT = 65536
$KV_UNIFIED = $false                    # $true = the slots share one pool of PARALLEL*CTX_PER_SLOT; any slot may use all of it (Huntun still plans each at CTX_PER_SLOT)
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
$EXTRA_ARGS = @()                       # anything else to pass to llama-server for Flash-Next

# Second model: Qwen3.8-27B (uncensored, Q4_K_M, ~17 GB). It fits entirely in VRAM, so it is much faster than Flash-Next
# (measured here: ~44 tok/s, ~83 total for two sessions, prefill ~2,800 t/s) but less capable. `.\setup.ps1 download 27b` fetches it.
# Once both models are downloaded, the server offers both under the same address and API key and loads whichever a
# request names; the GPU holds one at a time, so switching costs a reload (seconds for this one, a minute or more for
# Flash-Next).
$Q27_HF_REPO = "orcarouter/Qwen3.8-27B-Uncensored-GGUF"
$Q27_MODEL_DIR = "$FLASHNEXT_HOME\models\Qwen3.8-27B-Uncensored"
$Q27_MODEL_SET = "Q4_K_M"               # Q5_K_M (19.5 GB) is closer to Q8 but leaves room for ~64K of context only
$Q27_ALIAS = "qwen3.8-27b-uncensored"
$Q27_PARALLEL = 3                       # dense and all in VRAM: a session costs little speed (2 measured 43.5 tok/s each)
$Q27_CTX_PER_SLOT = 65536
$Q27_KV_UNIFIED = $true                 # the three share 192K (about what VRAM holds next to the weights); any may use all of it, Huntun plans each at 64K
$Q27_KV_TYPE = "q4_0"                   # ~18 KB per token (16 of 64 layers keep one); q8_0 doubles it
$Q27_BATCH = ""                         # empty = llama.cpp's defaults (2048 / 512)
$Q27_UBATCH = ""
$Q27_MTP = $false                       # $true = draft with the model's built-in MTP head (reported +30-40% decode); compare with bench.py
$Q27_MTP_DRAFT = 3
$Q27_EXTRA_ARGS = @()

# Which downloaded models to serve ("auto" = all of them, or e.g. "27b" or "flash-next,27b"), and which one to load
# at startup when there are several.
$SERVE_MODELS = "auto"
$DEFAULT_MODEL = "flash-next"
