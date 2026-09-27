#!/usr/bin/env bash
# Starts llama-server with the settings in flash-next.env (+ flash-next.local.env).
# One downloaded model: served on its own, as always. Qwen3.8-Flash-Next and Qwen3.8-27B both downloaded: llama-server
# runs as a router under the same address and API key, lists both, and loads the one each request names (one at a time).
# Flash-Next's N-gram (PLE) table stays on the NVMe drive: the model is memory-mapped and llama.cpp reads the rows of
# that table on demand (--lazy-mode on), so only the rest of the weights occupy VRAM and RAM.
#
#   ./serve.sh            run in the foreground
#   DRY_RUN=1 ./serve.sh  print the command (and the router's model presets) instead of running it
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

if [[ "$THREADS" == "auto" ]]; then
  THREADS="$(lscpu -p=Core,Socket 2>/dev/null | grep -v '^#' | sort -u | wc -l)"
  [[ "$THREADS" -gt 0 ]] || THREADS="$(nproc)"
fi

HELP="$("$SERVER" --help 2>&1 || true)"
has() { grep -q -e "$1" <<<"$HELP"; }

model_file() {  # the GGUF that download recorded in a model folder, or nothing
  [[ -f "$1/model.path" ]] || return 0
  local m; m="$(head -n1 "$1/model.path")"
  if [[ -f "$m" ]]; then echo "$m"; else echo "warning: model file missing: $m" >&2; fi
}

# shellcheck disable=SC2153  # BATCH and UBATCH come from flash-next.env
flash_next_args() {  # everything but the address, key and name
  local seq_ctx=$CTX_PER_SLOT batch=$BATCH ubatch=$UBATCH
  # The context one sequence can span: the whole pool when it is shared, one slot otherwise.
  [[ "$KV_UNIFIED" == "1" ]] && seq_ctx=$((PARALLEL * CTX_PER_SLOT))
  if [[ "$ubatch" == "auto" ]]; then  # largest power of two in [512, 4096] with seq_ctx * batch <= 64K * 4096
    ubatch=4096
    while (( ubatch > 512 && seq_ctx * ubatch > 65536 * 4096 )); do ubatch=$((ubatch / 2)); done
  fi
  [[ "$batch" == "auto" ]] && batch="$ubatch"
  ARGS=(
    -m "$1"
    -np "$PARALLEL" -c "$((PARALLEL * CTX_PER_SLOT))"
    -fa on -ctk "$KV_TYPE" -ctv "$KV_TYPE"
    -t "$THREADS" -tb "$THREADS"
    -b "$batch" -ub "$ubatch"
    --jinja
    --metrics
  )
  # Memory-map the weights and read the huge per-layer-embedding (N-gram) table from disk on demand.
  # Never add mlock here: it would pin the whole mapping, table included, and 64 GB cannot hold it.
  [[ "$KV_UNIFIED" == "1" ]] && ARGS+=(--kv-unified)
  has "--load-mode" && ARGS+=(--load-mode mmap)
  if has "--lazy-mode"; then
    ARGS+=(--lazy-mode on)
  else
    echo "warning: this llama-server has no --lazy-mode; the N-gram table may be loaded into RAM. Rebuild from a newer llama.cpp." >&2
  fi
  has "--fit " && ARGS+=(--fit on)
  has "--fit-target" && ARGS+=(--fit-target "$FIT_TARGET_MIB")
  has "--cache-ram" && ARGS+=(--cache-ram "$CACHE_RAM_MIB")
  has "--reasoning-budget" && ARGS+=(--reasoning-budget "$REASONING_BUDGET")
  if [[ -f "$MODEL_DIR/mmproj.path" && "$WITH_VISION" == "1" ]]; then
    ARGS+=(--mmproj "$(head -n1 "$MODEL_DIR/mmproj.path")")
  fi
  # shellcheck disable=SC2206
  ARGS+=($SAMPLING_ARGS $EXTRA_ARGS)
}

q27_args() {  # Qwen3.8-27B: all of it on the GPU, so no N-gram or batch tricks
  ARGS=(
    -m "$1"
    -np "$Q27_PARALLEL" -c "$((Q27_PARALLEL * Q27_CTX_PER_SLOT))"
    -fa on -ctk "$Q27_KV_TYPE" -ctv "$Q27_KV_TYPE"
    -t "$THREADS" -tb "$THREADS"
    --jinja
    --metrics
  )
  [[ -n "$Q27_BATCH" ]] && ARGS+=(-b "$Q27_BATCH")
  [[ -n "$Q27_UBATCH" ]] && ARGS+=(-ub "$Q27_UBATCH")
  [[ "$Q27_KV_UNIFIED" == "1" ]] && ARGS+=(--kv-unified)
  has "--fit " && ARGS+=(--fit on)
  has "--fit-target" && ARGS+=(--fit-target "$FIT_TARGET_MIB")
  has "--cache-ram" && ARGS+=(--cache-ram "$CACHE_RAM_MIB")
  has "--reasoning-budget" && ARGS+=(--reasoning-budget "$REASONING_BUDGET")
  if [[ "$Q27_MTP" == "1" ]]; then
    if has "draft-mtp"; then ARGS+=(--spec-type draft-mtp --spec-draft-n-max "$Q27_MTP_DRAFT")
    else echo "warning: this llama-server has no draft-mtp speculative decoding; Q27_MTP ignored" >&2; fi
  fi
  # shellcheck disable=SC2206
  ARGS+=($SAMPLING_ARGS $Q27_EXTRA_ARGS)
}

to_ini() {  # command-line arguments -> preset lines: "--flag value" becomes "flag = value", a lone "--flag" "flag = true"
  local -a a=("$@")
  local i=0 n=$# k
  while (( i < n )); do
    k="${a[i]#-}"; k="${k#-}"
    if (( i + 1 < n )) && [[ ! "${a[i + 1]}" =~ ^--?[A-Za-z] ]]; then
      printf '%s = %s\n' "$k" "${a[i + 1]}"; i=$((i + 2))
    else
      printf '%s = true\n' "$k"; i=$((i + 1))
    fi
  done
}

# The models to serve: those downloaded, narrowed by SERVE_MODELS.
declare -A FILE=() NAME=()
FILE[flash-next]="$(model_file "$MODEL_DIR")"; NAME[flash-next]="$ALIAS"
FILE[27b]="$(model_file "$Q27_MODEL_DIR")"; NAME[27b]="$Q27_ALIAS"
SERVE=()
for m in flash-next 27b; do
  [[ -n "${FILE[$m]}" ]] || continue
  [[ "$SERVE_MODELS" == "auto" || ",${SERVE_MODELS// /}," == *",$m,"* ]] && SERVE+=("$m")
done
if (( ${#SERVE[@]} == 0 )); then
  echo "No model to serve: run ./setup.sh download (Flash-Next) and/or ./setup.sh download 27b (SERVE_MODELS=$SERVE_MODELS)" >&2
  exit 1
fi
model_args() { if [[ "$1" == "27b" ]]; then q27_args "${FILE[$1]}"; else flash_next_args "${FILE[$1]}"; fi; }

PRESET=""
if (( ${#SERVE[@]} == 1 )); then
  model_args "${SERVE[0]}"
  args=(--alias "${NAME[${SERVE[0]}]}" --host "$HOST" --port "$PORT" "${ARGS[@]}")
else
  if ! has "--models-preset"; then
    echo "this llama-server cannot serve several models (no --models-preset); rebuild from a newer llama.cpp or set SERVE_MODELS" >&2
    exit 1
  fi
  # Router mode: the router answers on HOST:PORT with the API key and starts one llama-server per model on a
  # loopback port when a request names it. --models-max 1: the 4090 holds one of these models at a time, so loading
  # one first stops the other (once its requests are done). The section name is the model name clients send.
  PRESET="$FLASHNEXT_HOME/models.ini"
  default="$DEFAULT_MODEL"
  [[ " ${SERVE[*]} " == *" $default "* ]] || default="${SERVE[0]}"
  {
    echo "; written by serve.sh from flash-next.env; edit that file (or flash-next.local.env), not this one"
    echo "version = 1"
    for m in "${SERVE[@]}"; do
      model_args "$m"
      echo
      echo "[${NAME[$m]}]"
      to_ini "${ARGS[@]}"
      [[ "$m" == "$default" ]] && echo "load-on-startup = true"
    done
  } > "$PRESET.tmp"
  mv "$PRESET.tmp" "$PRESET"
  args=(--models-preset "$PRESET" --models-max 1 --host "$HOST" --port "$PORT")
fi
[[ -n "$API_KEY" ]] && args+=(--api-key "$API_KEY")

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  printf '%q ' "$SERVER" "${args[@]}"; echo
  if [[ -n "$PRESET" ]]; then echo; echo "$PRESET:"; cat "$PRESET"; fi
  exit 0
fi
if [[ -n "$PRESET" ]]; then
  echo "Serving ${SERVE[*]} as $(for m in "${SERVE[@]}"; do printf '%s ' "${NAME[$m]}"; done)(one loaded at a time); presets in $PRESET"
fi
echo "Starting: $SERVER ${args[*]}"
exec "$SERVER" "${args[@]}"
