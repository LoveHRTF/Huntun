# Starts llama-server with the settings in config.ps1 (+ config.local.ps1).
# One downloaded model: served on its own, as always. Qwen3.8-Flash-Next and Qwen3.8-27B both downloaded: llama-server
# runs as a router under the same address and API key, lists both, and loads the one each request names (one at a time).
# Flash-Next's N-gram (PLE) table stays on the NVMe drive: the model is memory-mapped and llama.cpp reads the rows of
# that table on demand (--lazy-mode on), so only the rest of the weights occupy VRAM and RAM.
#
#   .\serve.ps1                 run in the foreground
#   .\serve.ps1 -DryRun         print the command (and the router's model presets) instead of running it
#   .\serve.ps1 -LogFile x.log  append output to a log (used by the logon task)
param([switch]$DryRun, [string]$LogFile = "")
$ErrorActionPreference = "Stop"

. "$PSScriptRoot\config.ps1"
if (Test-Path "$PSScriptRoot\config.local.ps1") { . "$PSScriptRoot\config.local.ps1" }

$server = if ($env:LLAMA_SERVER) { $env:LLAMA_SERVER } else { "$FLASHNEXT_HOME\llama.cpp\llama-server.exe" }
if (-not (Test-Path $server)) { throw "llama-server not found at $server; run .\setup.ps1 install first" }

$threads = $THREADS
if ($threads -eq "auto") {
    $threads = (Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Measure-Object -Property NumberOfCores -Sum).Sum
    if (-not $threads) { $threads = [Environment]::ProcessorCount }
}

# Windows PowerShell 5.1 turns redirected stderr of native programs into errors; llama-server logs to stderr.
$ErrorActionPreference = "Continue"
$help = (& $server --help 2>&1 | Out-String)
function Has([string]$flag) { return $help.Contains($flag) }

function Get-ModelFile([string]$dir) {
    # the GGUF that download recorded in a model folder, or ""
    $pathFile = Join-Path $dir "model.path"
    if (-not (Test-Path $pathFile)) { return "" }
    $m = (Get-Content $pathFile -TotalCount 1).Trim()
    if (Test-Path $m) { return $m }
    Write-Warning "model file missing: $m"
    return ""
}

function Get-FlashNextArgs([string]$model) {
    # everything but the address, key and name
    # The context one sequence can span: the whole pool when it is shared, one slot otherwise.
    $seqCtx = if ($KV_UNIFIED) { $PARALLEL * $CTX_PER_SLOT } else { $CTX_PER_SLOT }
    $ubatch = $UBATCH
    if ($ubatch -eq "auto") {   # largest power of two in [512, 4096] with seqCtx * batch <= 64K * 4096
        $ubatch = 4096
        while ($ubatch -gt 512 -and [long]$seqCtx * $ubatch -gt 65536L * 4096) { $ubatch = $ubatch / 2 }
    }
    $batch = if ($BATCH -eq "auto") { $ubatch } else { $BATCH }
    $a = @(
        "-m", $model,
        "-np", "$PARALLEL", "-c", "$($PARALLEL * $CTX_PER_SLOT)",
        "-fa", "on", "-ctk", $KV_TYPE, "-ctv", $KV_TYPE,
        "-t", "$threads", "-tb", "$threads",
        "-b", "$batch", "-ub", "$ubatch",
        "--jinja",
        "--metrics"
    )
    # Memory-map the weights and read the huge per-layer-embedding (N-gram) table from disk on demand.
    # Never add mlock: it would pin the whole mapping, table included, and 64 GB cannot hold it.
    if ($KV_UNIFIED) { $a += "--kv-unified" }
    if (Has "--load-mode") { $a += @("--load-mode", "mmap") }
    if (Has "--lazy-mode") { $a += @("--lazy-mode", "on") }
    else { Write-Warning "this llama-server has no --lazy-mode; the N-gram table may be loaded into RAM. Use a newer LLAMA_TAG." }
    if (Has "--fit ") { $a += @("--fit", "on") }
    if (Has "--fit-target") { $a += @("--fit-target", "$FIT_TARGET_MIB") }
    if (Has "--cache-ram") { $a += @("--cache-ram", "$CACHE_RAM_MIB") }
    if (Has "--reasoning-budget") { $a += @("--reasoning-budget", "$REASONING_BUDGET") }
    $projFile = Join-Path $MODEL_DIR "mmproj.path"
    if ($WITH_VISION -and (Test-Path $projFile)) { $a += @("--mmproj", (Get-Content $projFile -TotalCount 1).Trim()) }
    $a += $SAMPLING_ARGS
    $a += $EXTRA_ARGS
    return $a
}

function Get-Q27Args([string]$model) {
    # Qwen3.8-27B: all of it on the GPU, so no N-gram or batch tricks
    $a = @(
        "-m", $model,
        "-np", "$Q27_PARALLEL", "-c", "$($Q27_PARALLEL * $Q27_CTX_PER_SLOT)",
        "-fa", "on", "-ctk", $Q27_KV_TYPE, "-ctv", $Q27_KV_TYPE,
        "-t", "$threads", "-tb", "$threads",
        "--jinja",
        "--metrics"
    )
    if ($Q27_BATCH) { $a += @("-b", "$Q27_BATCH") }
    if ($Q27_UBATCH) { $a += @("-ub", "$Q27_UBATCH") }
    if ($Q27_KV_UNIFIED) { $a += "--kv-unified" }
    if (Has "--fit ") { $a += @("--fit", "on") }
    if (Has "--fit-target") { $a += @("--fit-target", "$FIT_TARGET_MIB") }
    if (Has "--cache-ram") { $a += @("--cache-ram", "$CACHE_RAM_MIB") }
    if (Has "--reasoning-budget") { $a += @("--reasoning-budget", "$REASONING_BUDGET") }
    if ($Q27_MTP) {
        if (Has "draft-mtp") { $a += @("--spec-type", "draft-mtp", "--spec-draft-n-max", "$Q27_MTP_DRAFT") }
        else { Write-Warning "this llama-server has no draft-mtp speculative decoding; Q27_MTP ignored" }
    }
    $a += $SAMPLING_ARGS
    $a += $Q27_EXTRA_ARGS
    return $a
}

function ConvertTo-PresetLines([string[]]$list) {
    # command-line arguments -> preset lines: "--flag value" becomes "flag = value", a lone "--flag" "flag = true"
    $lines = @()
    $i = 0
    while ($i -lt $list.Count) {
        $k = $list[$i].TrimStart("-")
        if ($i + 1 -lt $list.Count -and $list[$i + 1] -notmatch '^--?[A-Za-z]') { $lines += "$k = $($list[$i + 1])"; $i += 2 }
        else { $lines += "$k = true"; $i += 1 }
    }
    return $lines
}

# The models to serve: those downloaded, narrowed by SERVE_MODELS.
$files = [ordered]@{ "flash-next" = (Get-ModelFile $MODEL_DIR); "27b" = (Get-ModelFile $Q27_MODEL_DIR) }
$names = @{ "flash-next" = $ALIAS; "27b" = $Q27_ALIAS }
$wanted = @($SERVE_MODELS -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$serve = @($files.Keys | Where-Object { $files[$_] -and ($SERVE_MODELS -eq "auto" -or $wanted -contains $_) })
if ($serve.Count -eq 0) { throw "No model to serve: run .\setup.ps1 download (Flash-Next) and/or .\setup.ps1 download 27b (SERVE_MODELS=$SERVE_MODELS)" }
function Get-ModelArgs([string]$key) { if ($key -eq "27b") { Get-Q27Args $files[$key] } else { Get-FlashNextArgs $files[$key] } }

$preset = ""
if ($serve.Count -eq 1) {
    $a = @("--alias", $names[$serve[0]], "--host", $LISTEN, "--port", "$PORT") + @(Get-ModelArgs $serve[0])
} else {
    if (-not (Has "--models-preset")) { throw "this llama-server cannot serve several models (no --models-preset); use a newer LLAMA_TAG or set SERVE_MODELS" }
    # Router mode: the router answers on LISTEN:PORT with the API key and starts one llama-server per model on a
    # loopback port when a request names it. --models-max 1: the 4090 holds one of these models at a time, so loading
    # one first stops the other (once its requests are done). The section name is the model name clients send.
    $preset = Join-Path $FLASHNEXT_HOME "models.ini"
    $default = if ($serve -contains $DEFAULT_MODEL) { $DEFAULT_MODEL } else { $serve[0] }
    $lines = @("; written by serve.ps1 from config.ps1; edit that file (or config.local.ps1), not this one", "version = 1")
    foreach ($m in $serve) {
        $margs = @(Get-ModelArgs $m)
        $lines += ""
        $lines += "[$($names[$m])]"
        $lines += ConvertTo-PresetLines $margs
        if ($m -eq $default) { $lines += "load-on-startup = true" }
    }
    # UTF-8 without a byte-order mark: llama-server's preset parser does not skip one
    [IO.File]::WriteAllText($preset, (($lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding $false))
    $a = @("--models-preset", $preset, "--models-max", "1", "--host", $LISTEN, "--port", "$PORT")
}
if ($API_KEY) { $a += @("--api-key", $API_KEY) }

$shown = ($a | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join " "
if ($DryRun) {
    Write-Output "$server $shown"
    if ($preset) { Write-Output ""; Write-Output "${preset}:"; Get-Content $preset }
    exit 0
}
if ($preset) { Write-Output "Serving $(($serve | ForEach-Object { $names[$_] }) -join ', ') (one loaded at a time); presets in $preset" }
Write-Output "Starting: $server $shown"
if ($LogFile) {
    & $server @a *>> $LogFile
} else {
    & $server @a
}
exit $LASTEXITCODE
