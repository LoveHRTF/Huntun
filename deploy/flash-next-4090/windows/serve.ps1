# Starts llama-server for Qwen3.8-Flash-Next with the settings in config.ps1 (+ config.local.ps1).
# The N-gram (PLE) table stays on the NVMe drive: the model is memory-mapped and llama.cpp reads the rows of
# that table on demand (--lazy-mode on), so only the rest of the weights occupy VRAM and RAM.
#
#   .\serve.ps1                 run in the foreground
#   .\serve.ps1 -DryRun         print the command instead of running it
#   .\serve.ps1 -LogFile x.log  append output to a log (used by the logon task)
param([switch]$DryRun, [string]$LogFile = "")
$ErrorActionPreference = "Stop"

. "$PSScriptRoot\config.ps1"
if (Test-Path "$PSScriptRoot\config.local.ps1") { . "$PSScriptRoot\config.local.ps1" }

$server = if ($env:LLAMA_SERVER) { $env:LLAMA_SERVER } else { "$FLASHNEXT_HOME\llama.cpp\llama-server.exe" }
if (-not (Test-Path $server)) { throw "llama-server not found at $server; run .\setup.ps1 install first" }
$pathFile = Join-Path $MODEL_DIR "model.path"
if (-not (Test-Path $pathFile)) { throw "No model recorded in $pathFile; run .\setup.ps1 download first" }
$model = (Get-Content $pathFile -TotalCount 1).Trim()
if (-not (Test-Path $model)) { throw "Model file missing: $model" }

$threads = $THREADS
if ($threads -eq "auto") {
    $threads = (Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Measure-Object -Property NumberOfCores -Sum).Sum
    if (-not $threads) { $threads = [Environment]::ProcessorCount }
}

# Windows PowerShell 5.1 turns redirected stderr of native programs into errors; llama-server logs to stderr.
$ErrorActionPreference = "Continue"
$help = (& $server --help 2>&1 | Out-String)
function Has([string]$flag) { return $help.Contains($flag) }

$a = @(
    "-m", $model,
    "--alias", $ALIAS,
    "--host", $LISTEN, "--port", "$PORT",
    "-np", "$PARALLEL", "-c", "$($PARALLEL * $CTX_PER_SLOT)",
    "-fa", "on", "-ctk", $KV_TYPE, "-ctv", $KV_TYPE,
    "-t", "$threads", "-tb", "$threads",
    "-b", "$BATCH", "-ub", "$UBATCH",
    "--jinja",
    "--metrics"
)
# Memory-map the weights and read the huge per-layer-embedding (N-gram) table from disk on demand.
# Never add mlock: it would pin the whole mapping, table included, and 64 GB cannot hold it.
if (Has "--load-mode") { $a += @("--load-mode", "mmap") }
if (Has "--lazy-mode") { $a += @("--lazy-mode", "on") }
else { Write-Warning "this llama-server has no --lazy-mode; the N-gram table may be loaded into RAM. Use a newer LLAMA_TAG." }
if (Has "--fit ") { $a += @("--fit", "on") }
if (Has "--fit-target") { $a += @("--fit-target", "$FIT_TARGET_MIB") }
if (Has "--cache-ram") { $a += @("--cache-ram", "$CACHE_RAM_MIB") }
if (Has "--reasoning-budget") { $a += @("--reasoning-budget", "$REASONING_BUDGET") }
$projFile = Join-Path $MODEL_DIR "mmproj.path"
if ($WITH_VISION -and (Test-Path $projFile)) { $a += @("--mmproj", (Get-Content $projFile -TotalCount 1).Trim()) }
if ($API_KEY) { $a += @("--api-key", $API_KEY) }
$a += $SAMPLING_ARGS
$a += $EXTRA_ARGS

$shown = ($a | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join " "
if ($DryRun) { Write-Output "$server $shown"; exit 0 }
Write-Output "Starting: $server $shown"
if ($LogFile) {
    & $server @a *>> $LogFile
} else {
    & $server @a
}
exit $LASTEXITCODE
