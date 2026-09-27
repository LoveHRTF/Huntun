#!/usr/bin/env bash
# shellcheck disable=SC2015  # ok/warn/bad always succeed, so `A && ok || bad` is a safe if-else
# Sets up Qwen3.8-Flash-Next (uncensored, AD-4.27 GGUF) on an Ubuntu box with an RTX 4090 and 64 GB RAM.
#
#   ./setup.sh check      hardware / OS checks (no changes)
#   ./setup.sh deps       apt build dependencies (+ CUDA toolkit with INSTALL_CUDA=1)       [sudo]
#   ./setup.sh build      clone/update llama.cpp at LLAMA_REF and build llama-server with CUDA
#   ./setup.sh download   download the model set from Hugging Face into MODEL_DIR
#   ./setup.sh tune       vm.swappiness=10 and headless-mode advice                         [sudo]
#   ./setup.sh service    install and start a systemd service running serve.sh               [sudo]
#   ./setup.sh all        check, deps, build, download
#
# Settings: flash-next.env, overridden by flash-next.local.env next to it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
[[ -f "$HERE/flash-next.local.env" ]] && source "$HERE/flash-next.local.env"
# shellcheck source=flash-next.env
source "$HERE/flash-next.env"

VENV="$FLASHNEXT_HOME/venv"
LLAMA_DIR="$FLASHNEXT_HOME/llama.cpp"
export PATH="/usr/local/cuda/bin:$PATH"

ok()   { printf '  [ok]   %s\n' "$*"; }
warn() { printf '  [warn] %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; FAILED=1; }

cmd_check() {
  FAILED=0
  echo "System checks"
  [[ "$(uname -s)" == "Linux" ]] && ok "Linux $(uname -r)" || bad "not Linux: this kit targets Ubuntu 24.04"
  if [[ -r /etc/os-release ]]; then
    # shellcheck source=/dev/null
    . /etc/os-release
    [[ "${ID:-}" == "ubuntu" ]] && ok "$PRETTY_NAME" || warn "$PRETTY_NAME (tested on Ubuntu 24.04; apt steps may differ)"
  fi

  if command -v nvidia-smi >/dev/null; then
    local name vram driver gen_max width used
    IFS=, read -r name vram driver gen_max width used < <(nvidia-smi --query-gpu=name,memory.total,driver_version,pcie.link.gen.max,pcie.link.width.max,memory.used --format=csv,noheader,nounits | head -n1)
    name="$(echo "$name" | xargs)"
    [[ "$name" == *4090* ]] && ok "GPU: $name, ${vram// /} MiB, driver ${driver// /}" || warn "GPU: $name (tuned for an RTX 4090)"
    gen_max="${gen_max// /}"; width="${width// /}"
    if [[ "$gen_max" -ge 4 && "$width" -ge 16 ]]; then ok "PCIe gen $gen_max x$width (prefill streams CPU-side experts over this link)"
    else warn "PCIe gen $gen_max x$width: prefill will be slower than with gen4 x16 (B450/A520 boards are gen3)"; fi
    used="${used// /}"
    if [[ "$used" -gt 400 ]]; then warn "${used} MiB VRAM already in use (desktop session?); headless frees it, or raise FIT_TARGET_MIB"
    else ok "VRAM in use at idle: ${used} MiB"; fi
  else
    bad "nvidia-smi not found: install the NVIDIA driver first (ubuntu-drivers install)"
  fi

  if command -v nvcc >/dev/null; then ok "nvcc $(nvcc --version | grep -o 'release [0-9.]*')"
  else warn "nvcc not found: run INSTALL_CUDA=1 ./setup.sh deps, or install cuda-toolkit-12-8"; fi

  local mem_gb swap_kb swappiness
  mem_gb=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 / 1024 ))
  [[ "$mem_gb" -ge 60 ]] && ok "RAM: ${mem_gb} GB" || bad "RAM: ${mem_gb} GB (needs 64 GB)"
  swap_kb=$(awk '/SwapTotal/ {print $2}' /proc/meminfo)
  [[ "$swap_kb" -gt 0 ]] && ok "swap: $(( swap_kb / 1024 / 1024 )) GB" || warn "no swap: add 8-16 GB so a memory spike does not trigger the OOM killer"
  swappiness=$(cat /proc/sys/vm/swappiness)
  [[ "$swappiness" -le 20 ]] && ok "vm.swappiness=$swappiness" || warn "vm.swappiness=$swappiness (./setup.sh tune sets 10)"
  ok "CPU: $(lscpu | awk -F: '/Model name/ {gsub(/^ +/,"",$2); print $2; exit}'), $(lscpu -p=Core,Socket | grep -v '^#' | sort -u | wc -l) physical cores"

  mkdir -p "$FLASHNEXT_HOME"
  local free_gb src dev rota tran
  free_gb=$(( $(df -Pk "$FLASHNEXT_HOME" | awk 'NR==2 {print $4}') / 1024 / 1024 ))
  [[ "$free_gb" -ge 100 ]] && ok "free disk at $FLASHNEXT_HOME: ${free_gb} GB" || bad "free disk at $FLASHNEXT_HOME: ${free_gb} GB (the model needs ~85 GB + build)"
  src="$(findmnt -no SOURCE --target "$FLASHNEXT_HOME" | head -n1)"
  dev="$(lsblk -no PKNAME "$src" 2>/dev/null | head -n1)"; dev="${dev:-$(basename "$src")}"
  rota="$(lsblk -dno ROTA "/dev/$dev" 2>/dev/null | xargs || true)"
  tran="$(lsblk -dno TRAN "/dev/$dev" 2>/dev/null | xargs || true)"
  if [[ "$rota" == "1" ]]; then bad "$FLASHNEXT_HOME is on a spinning disk ($dev): the N-gram table must be on NVMe"
  elif [[ "$tran" == "nvme" || "$dev" == nvme* ]]; then ok "$FLASHNEXT_HOME is on NVMe ($dev)"
  else warn "$FLASHNEXT_HOME is on $dev (${tran:-unknown transport}); NVMe is recommended for the N-gram table"; fi

  if systemctl is-active --quiet display-manager 2>/dev/null; then
    warn "a desktop session is running: it costs RAM and VRAM; see ./setup.sh tune for headless mode"
  else ok "no display manager running"; fi
  return "$FAILED"
}

cmd_deps() {
  echo "Installing build dependencies"
  sudo apt-get update
  sudo apt-get install -y build-essential cmake git curl ca-certificates pkg-config libssl-dev python3-venv python3-pip
  if ! command -v nvcc >/dev/null; then
    if [[ "$INSTALL_CUDA" == "1" ]]; then
      . /etc/os-release
      local repo="ubuntu${VERSION_ID//./}"
      local tmp; tmp="$(mktemp -d)"
      curl -fsSL -o "$tmp/cuda-keyring.deb" "https://developer.download.nvidia.com/compute/cuda/repos/$repo/x86_64/cuda-keyring_1.1-1_all.deb"
      sudo dpkg -i "$tmp/cuda-keyring.deb"
      sudo apt-get update
      sudo apt-get install -y cuda-toolkit-12-8
      rm -rf "$tmp"
    else
      echo "nvcc is missing. Re-run with INSTALL_CUDA=1 to install cuda-toolkit-12-8 from NVIDIA's apt repo." >&2
      return 1
    fi
  fi
  mkdir -p "$FLASHNEXT_HOME"
  [[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip "huggingface_hub[hf_xet]>=0.34"
  echo "Dependencies ready (nvcc: $(command -v nvcc))"
}

cmd_build() {
  command -v nvcc >/dev/null || { echo "nvcc not found; run ./setup.sh deps first" >&2; return 1; }
  mkdir -p "$FLASHNEXT_HOME"
  if [[ ! -d "$LLAMA_DIR/.git" ]]; then
    git clone https://github.com/ggml-org/llama.cpp.git "$LLAMA_DIR"
  fi
  git -C "$LLAMA_DIR" fetch --tags origin
  if [[ "$LLAMA_REF" == pr/* ]]; then
    git -C "$LLAMA_DIR" fetch origin "pull/${LLAMA_REF#pr/}/head:$LLAMA_REF"
    git -C "$LLAMA_DIR" checkout -f "$LLAMA_REF"
  elif git -C "$LLAMA_DIR" rev-parse --verify -q "origin/$LLAMA_REF" >/dev/null; then
    git -C "$LLAMA_DIR" checkout -f -B "$LLAMA_REF" "origin/$LLAMA_REF"
  else
    git -C "$LLAMA_DIR" checkout -f "$LLAMA_REF"
  fi
  echo "Building llama.cpp $(git -C "$LLAMA_DIR" log -1 --format='%h (%cd)' --date=short) for sm_$CUDA_ARCH"
  cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCH" -DLLAMA_BUILD_TESTS=OFF
  cmake --build "$LLAMA_DIR/build" --config Release -j "$(nproc)" --target llama-server llama-bench
  local help; help="$("$LLAMA_DIR/build/bin/llama-server" --help 2>&1 || true)"
  grep -q -e "--lazy-mode" <<<"$help" || echo "warning: this build has no --lazy-mode (on-disk N-gram table); use a newer LLAMA_REF" >&2
  echo "Built $LLAMA_DIR/build/bin/llama-server"
}

cmd_download() {
  [[ -x "$VENV/bin/python" ]] || { echo "venv missing; run ./setup.sh deps first" >&2; return 1; }
  local extra=()
  [[ -n "$MODEL_SET" ]] && extra+=(--set "$MODEL_SET")
  [[ "$WITH_VISION" == "1" ]] && extra+=(--vision)
  "$VENV/bin/python" "$HERE/fetch_model.py" "$HF_REPO" "$MODEL_DIR" "${extra[@]}"
  echo "Read the model card before first use: $MODEL_DIR/README.md (required llama.cpp version, recommended flags)"
}

cmd_tune() {
  echo "vm.swappiness=10 (keeps weights in RAM instead of swapping them out)"
  echo "vm.swappiness=10" | sudo tee /etc/sysctl.d/99-flash-next.conf >/dev/null
  sudo sysctl -q -p /etc/sysctl.d/99-flash-next.conf
  cat <<'EOF'
Headless mode (recommended: frees ~1 GB VRAM and several GB RAM). The 5900X has no iGPU, so a desktop runs on the 4090.
  sudo systemctl set-default multi-user.target && sudo reboot      # undo: sudo systemctl set-default graphical.target
Firewall the server to the Huntun host only, e.g.:
  sudo ufw allow from <mac-mini-ip> to any port 8080 proto tcp && sudo ufw enable
EOF
}

cmd_service() {
  local unit=/etc/systemd/system/flash-next.service
  sudo tee "$unit" >/dev/null <<EOF
[Unit]
Description=Qwen3.8-Flash-Next llama-server
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$HERE
Environment=HOME=$HOME
ExecStart=$HERE/serve.sh
Restart=on-failure
RestartSec=10
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now flash-next.service
  echo "Installed $unit. Logs: journalctl -u flash-next -f"
}

case "${1:-}" in
  check) cmd_check ;;
  deps) cmd_deps ;;
  build) cmd_build ;;
  download) cmd_download ;;
  tune) cmd_tune ;;
  service) cmd_service ;;
  all) cmd_check || { echo "Fix the [FAIL] items above first (or run the steps one by one)." >&2; exit 1; }
       cmd_deps; cmd_build; cmd_download
       echo; echo "Next: ./serve.sh   (then ./bench.py in another shell; ./setup.sh service to run it at boot)" ;;
  *) sed -n '3,13p' "$0"; exit 1 ;;
esac
