#!/usr/bin/env bash
# Starts llama-server for Qwen3.8-Flash-Next with the settings in flash-next.env (+ flash-next.local.env).
# The N-gram (PLE) table stays on the NVMe drive: the model is memory-mapped and llama.cpp reads the rows of
# that table on demand (--lazy-mode on), so only the rest of the weights occupy VRAM and RAM.
#
#   ./serve.sh            run in the foreground
#   DRY_RUN=1 ./serve.sh  print the command instead of running it
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
[[ -f "$HERE/flash-next.local.env" ]] && source "$HERE/flash-next.local.env"
# shellcheck source=flash-next.env
source "$HERE/flash-next.env"

SERVER="${LLAMA_SERVER:-$FLASHNEXT_HOME/llama.cpp/build/bin/llama-server}"
if [[ ! -x "$SERVER" ]]; then
  echo "llama-server not found at $SERVER; run ./setup.sh build first" >&2
  exit 1
fi
if [[ ! -f "$MODEL_DIR/model.path" ]]; then
  echo "No model recorded in $MODEL_DIR/model.path; run ./setup.sh download first" >&2
  exit 1
fi
MODEL="$(head -n1 "$MODEL_DIR/model.path")"
[[ -f "$MODEL" ]] || { echo "Model file missing: $MODEL" >&2; exit 1; }

if [[ "$THREADS" == "auto" ]]; then
  THREADS="$(lscpu -p=Core,Socket 2>/dev/null | grep -v '^#' | sort -u | wc -l)"
  [[ "$THREADS" -gt 0 ]] || THREADS="$(nproc)"
fi

HELP="$("$SERVER" --help 2>&1 || true)"
has() { grep -q -e "$1" <<<"$HELP"; }

args=(
  -m "$MODEL"
  --alias "$ALIAS"
  --host "$HOST" --port "$PORT"
  -np "$PARALLEL" -c "$((PARALLEL * CTX_PER_SLOT))"
  -fa on -ctk "$KV_TYPE" -ctv "$KV_TYPE"
  -t "$THREADS" -tb "$THREADS"
  -b "$BATCH" -ub "$UBATCH"
  --jinja
  --metrics
)
# Memory-map the weights and read the huge per-layer-embedding (N-gram) table from disk on demand.
# Never add mlock here: it would pin the whole mapping, table included, and 64 GB cannot hold it.
has "--load-mode" && args+=(--load-mode mmap)
if has "--lazy-mode"; then
  args+=(--lazy-mode on)
else
  echo "warning: this llama-server has no --lazy-mode; the N-gram table may be loaded into RAM. Rebuild from a newer llama.cpp." >&2
fi
has "--fit " && args+=(--fit on)
has "--fit-target" && args+=(--fit-target "$FIT_TARGET_MIB")
has "--cache-ram" && args+=(--cache-ram "$CACHE_RAM_MIB")
has "--reasoning-budget" && args+=(--reasoning-budget "$REASONING_BUDGET")
if [[ -f "$MODEL_DIR/mmproj.path" && "$WITH_VISION" == "1" ]]; then
  args+=(--mmproj "$(head -n1 "$MODEL_DIR/mmproj.path")")
fi
[[ -n "$API_KEY" ]] && args+=(--api-key "$API_KEY")
# shellcheck disable=SC2206
args+=($SAMPLING_ARGS $EXTRA_ARGS)

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  printf '%q ' "$SERVER" "${args[@]}"; echo
  exit 0
fi
echo "Starting: $SERVER ${args[*]}"
exec "$SERVER" "${args[@]}"
