# Sets up Qwen3.8-Flash-Next (uncensored, AD-4.27 GGUF) on Windows with an RTX 4090 and 64 GB RAM.
#
#   .\setup.ps1 check      hardware / OS checks (no changes)
#   .\setup.ps1 install    official llama.cpp CUDA build (prebuilt) + Python venv with huggingface_hub
#   .\setup.ps1 login      store a Hugging Face token (the model repo is gated)
#   .\setup.ps1 download   download the model set from Hugging Face into MODEL_DIR
#   .\setup.ps1 apikey     create an API key for the server (apikey new: replace it)
#   .\setup.ps1 firewall   let the local network reach the server (ALLOW_FROM)                  [admin]
#   .\setup.ps1 tune       firewall, no sleep, Defender exclusion                                [admin]
#   .\setup.ps1 task       start the server at your logon (logs to server.log)
#   .\setup.ps1 service    start the server at boot, without anyone signing in                   [admin]
#   .\setup.ps1 restart    restart the background server after changing settings                 [admin]
#   .\setup.ps1 connect    print the chat address and the settings Huntun needs
#   .\setup.ps1 all        check, install, download
#
# Settings: config.ps1, overridden by config.local.ps1 next to it.
param([Parameter(Position = 0)][string]$Command = "", [Parameter(Position = 1)][string]$Arg = "")
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is many times faster without the progress bar

. "$PSScriptRoot\config.ps1"
if (Test-Path "$PSScriptRoot\config.local.ps1") { . "$PSScriptRoot\config.local.ps1" }

$Kit = Split-Path -Parent $PSScriptRoot    # the shared Python scripts (fetch_model.py, bench.py)
$Venv = "$FLASHNEXT_HOME\venv"
$LlamaDir = "$FLASHNEXT_HOME\llama.cpp"
$script:Failed = $false

function Ok([string]$m) { Write-Host "  [ok]   $m" }
function Warn([string]$m) { Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Bad([string]$m) { Write-Host "  [FAIL] $m" -ForegroundColor Red; $script:Failed = $true }
function IsAdmin { ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }

function Find-Python {
    # Returns @{ Exe; Args } for a Python 3.10+ interpreter, or $null. "python" may be the Microsoft Store stub; the version probe rejects it.
    foreach ($c in @(@{ Exe = "py"; Args = @("-3") }, @{ Exe = "python"; Args = @() })) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        $v = & $c.Exe @($c.Args) -c "import sys; print(sys.version_info >= (3, 10))" 2>$null
        if ($v -eq "True") { return $c }
    }
    return $null
}

function Invoke-Check {
    $script:Failed = $false
    Write-Host "System checks"
    $os = Get-CimInstance Win32_OperatingSystem
    Ok "$($os.Caption) build $($os.BuildNumber)"

    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($smi) {
        $q = (& nvidia-smi --query-gpu=name,memory.total,driver_version,pcie.link.gen.max,pcie.link.width.max,memory.used --format=csv,noheader,nounits | Select-Object -First 1).Split(",") | ForEach-Object { $_.Trim() }
        if ($q[0] -like "*4090*") { Ok "GPU: $($q[0]), $($q[1]) MiB, driver $($q[2])" } else { Warn "GPU: $($q[0]) (tuned for an RTX 4090)" }
        $need = if ($LLAMA_CUDA -like "13*") { 580 } else { 551 }
        if ([double]$q[2] -lt $need) { Bad "driver $($q[2]) is too old for the CUDA $LLAMA_CUDA build (needs $need+); update the NVIDIA driver" }
        if ([int]$q[3] -ge 4 -and [int]$q[4] -ge 16) { Ok "PCIe gen $($q[3]) x$($q[4]) (prefill streams CPU-side experts over this link)" }
        else { Warn "PCIe gen $($q[3]) x$($q[4]): prefill will be slower than with gen4 x16 (B450/A520 boards are gen3)" }
        Ok "VRAM in use now: $($q[5]) MiB (the desktop runs on this GPU; FIT_TARGET_MIB=$FIT_TARGET_MIB is kept free)"
    } else {
        Bad "nvidia-smi not found: install the NVIDIA driver"
    }

    $ramGb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
    if ($ramGb -ge 60) { Ok "RAM: $ramGb GB" } else { Bad "RAM: $ramGb GB (needs 64 GB)" }
    $pf = Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue
    $pfGb = if ($pf) { [math]::Round(($pf | Measure-Object AllocatedBaseSize -Sum).Sum / 1024) } else { 0 }
    if ($pfGb -ge 16) { Ok "page file: $pfGb GB" }
    else { Warn "page file: $pfGb GB; set it to system managed or 16+ GB (System > About > Advanced system settings > Performance) so a memory spike does not kill the server" }
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    Ok "CPU: $($cpu.Name.Trim()), $($cpu.NumberOfCores) cores"

    New-Item -ItemType Directory -Force -Path $FLASHNEXT_HOME | Out-Null
    Ok "install location: $FLASHNEXT_HOME (change it with `$FLASHNEXT_HOME in config.local.ps1, not in the shell)"
    $letter = (Resolve-Path $FLASHNEXT_HOME).Path.Substring(0, 1)
    $vol = Get-Volume -DriveLetter $letter
    $freeGb = [math]::Round($vol.SizeRemaining / 1GB)
    if ($freeGb -ge 100) { Ok "free on ${letter}: $freeGb GB" } else { Bad "free on ${letter}: $freeGb GB (the model needs ~85 GB)" }
    $diskNumber = (Get-Partition -DriveLetter $letter).DiskNumber
    $disk = Get-PhysicalDisk | Where-Object { $_.DeviceId -eq "$diskNumber" } | Select-Object -First 1
    if ($disk.MediaType -eq "HDD") { Bad "${letter}: is a hard disk: the N-gram table must be on NVMe" }
    elseif ($disk.BusType -eq "NVMe") { Ok "${letter}: is NVMe ($($disk.FriendlyName))" }
    else { Warn "${letter}: is $($disk.BusType) $($disk.MediaType) ($($disk.FriendlyName)); NVMe is recommended" }

    try {
        $ex = (Get-MpPreference).ExclusionPath
        if ($ex -and ($ex | Where-Object { $FLASHNEXT_HOME -like "$_*" })) { Ok "Defender excludes $FLASHNEXT_HOME" }
        else { Warn "Defender scans $FLASHNEXT_HOME on every read of the model; .\setup.ps1 tune adds an exclusion" }
    } catch { Warn "could not read Defender settings (run as admin to check)" }

    Warn "set NVIDIA Control Panel > Manage 3D settings > CUDA - Sysmem Fallback Policy = Prefer No Sysmem Fallback (cannot be checked from here)"
    $py = Find-Python
    if ($py) { Ok "Python: $($py.Exe) $($py.Args -join ' ')" } else { Warn "Python 3.10+ not found: winget install Python.Python.3.12" }
    if ($script:Failed) { return 1 } else { return 0 }
}

function Invoke-Install {
    New-Item -ItemType Directory -Force -Path $FLASHNEXT_HOME | Out-Null
    # llama.cpp publishes every build (bNNNNN) as a pre-release, so /releases/latest points at an old release without
    # binaries. "latest" therefore means the newest build that ships the Windows CUDA zip and its runtime DLLs.
    $api = "https://api.github.com/repos/ggml-org/llama.cpp/releases"
    $headers = @{ "User-Agent" = "huntun-flash-next" }
    $releases = if ($LLAMA_TAG -eq "latest") { Invoke-RestMethod "${api}?per_page=30" -Headers $headers } else { @(Invoke-RestMethod "$api/tags/$LLAMA_TAG" -Headers $headers) }
    $rel = $null; $bin = $null; $rt = $null
    foreach ($r in $releases) {
        $bin = $r.assets | Where-Object { $_.name -like "llama-*-bin-win-cuda-$LLAMA_CUDA-x64.zip" } | Select-Object -First 1
        $rt = $r.assets | Where-Object { $_.name -eq "cudart-llama-bin-win-cuda-$LLAMA_CUDA-x64.zip" } | Select-Object -First 1
        if ($bin -and $rt) { $rel = $r; break }
    }
    if (-not $rel) { throw "no llama.cpp release ($LLAMA_TAG) has a Windows CUDA $LLAMA_CUDA x64 build; set LLAMA_TAG or LLAMA_CUDA in config.local.ps1" }
    Write-Host "llama.cpp $($rel.tag_name): $($bin.name) + $($rt.name)"

    $tmp = Join-Path ([IO.Path]::GetTempPath()) "flash-next-$([guid]::NewGuid())"
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        foreach ($asset in @($bin, $rt)) {
            $zip = Join-Path $tmp $asset.name
            Invoke-WebRequest $asset.browser_download_url -OutFile $zip
            Expand-Archive $zip -DestinationPath (Join-Path $tmp "x") -Force
        }
        $exe = Get-ChildItem (Join-Path $tmp "x") -Recurse -Filter llama-server.exe | Select-Object -First 1
        if (-not $exe) { throw "llama-server.exe not found in $($bin.name)" }
        if (Test-Path $LlamaDir) { Remove-Item $LlamaDir -Recurse -Force }
        New-Item -ItemType Directory -Path $LlamaDir | Out-Null
        Copy-Item "$($exe.DirectoryName)\*" $LlamaDir -Recurse -Force
        Get-ChildItem (Join-Path $tmp "x") -Recurse -Filter *.dll | Where-Object { $_.DirectoryName -ne $exe.DirectoryName } | Copy-Item -Destination $LlamaDir -Force
        Set-Content "$LlamaDir\VERSION.txt" $rel.tag_name
    } finally {
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
    $ErrorActionPreference = "Continue"
    $help = (& "$LlamaDir\llama-server.exe" --help 2>&1 | Out-String)
    $ErrorActionPreference = "Stop"
    if (-not $help.Contains("--lazy-mode")) { Write-Warning "this build has no --lazy-mode (on-disk N-gram table); use a newer LLAMA_TAG" }
    Write-Host "Installed $LlamaDir\llama-server.exe ($($rel.tag_name))"

    $py = Find-Python
    if (-not $py) { throw "Python 3.10+ not found: winget install Python.Python.3.12, then run install again" }
    if (-not (Test-Path "$Venv\Scripts\python.exe")) { & $py.Exe @($py.Args) -m venv $Venv }
    & "$Venv\Scripts\python.exe" -m pip install -q --upgrade pip "huggingface_hub[hf_xet]>=0.34"
    Write-Host "Python venv ready at $Venv"
}

function Invoke-Download {
    if (-not (Test-Path "$Venv\Scripts\python.exe")) { throw "venv missing; run .\setup.ps1 install first" }
    $extra = @()
    if ($MODEL_SET) { $extra += @("--set", $MODEL_SET) }
    if ($WITH_VISION) { $extra += "--vision" }
    & "$Venv\Scripts\python.exe" "$Kit\fetch_model.py" $HF_REPO $MODEL_DIR @extra
    if ($LASTEXITCODE -ne 0) { throw "download failed ($LASTEXITCODE)" }
    Write-Host "Read the model card before first use: https://huggingface.co/$HF_REPO (required llama.cpp version, recommended flags)"
}

function Invoke-Login {
    if (-not (Test-Path "$Venv\Scripts\hf.exe")) { throw "venv missing; run .\setup.ps1 install first" }
    Write-Host "Sign in through the browser page it opens, or paste a Read token from https://huggingface.co/settings/tokens."
    & "$Venv\Scripts\hf.exe" auth login
    & "$Venv\Scripts\hf.exe" auth whoami
}

function Get-LanAddresses {
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -ne "127.0.0.1" -and $_.IPAddress -notlike "169.254.*" -and $_.PrefixOrigin -ne "WellKnown" } |
        Select-Object -ExpandProperty IPAddress
}

function Invoke-ApiKey {
    $local = "$PSScriptRoot\config.local.ps1"
    if ($API_KEY -and $Arg -ne "new") {
        Write-Host "The server already has an API key: $API_KEY   (.\setup.ps1 apikey new replaces it)"
        return
    }
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $key = [Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_").TrimEnd("=")
    $lines = @()
    if (Test-Path $local) { $lines = @(Get-Content $local | Where-Object { $_ -notmatch '^\s*\$API_KEY\s*=' }) }
    $lines += "`$API_KEY = `"$key`""
    Set-Content $local $lines
    Write-Host "API key written to config.local.ps1: $key"
    Write-Host "Restart the server to apply it. Every client needs it: the chat page asks for it under Settings > API Key,"
    Write-Host "and Huntun needs it too (.\setup.ps1 connect prints what to enter under Model providers)."
}

function Invoke-Firewall {
    if (-not (IsAdmin)) { throw "run this from an elevated PowerShell (Run as administrator)" }
    $prog = "$LlamaDir\llama-server.exe"
    # answering "Block" to Windows' first-run prompt leaves a rule that beats any Allow rule
    $blocking = Get-NetFirewallApplicationFilter -Program $prog -ErrorAction SilentlyContinue | Get-NetFirewallRule |
        Where-Object { $_.Direction -eq "Inbound" -and $_.Action -eq "Block" }
    foreach ($r in $blocking) { Remove-NetFirewallRule -Name $r.Name; Write-Host "Removed the blocking rule '$($r.DisplayName)'" }
    Get-NetFirewallRule -DisplayName "flash-next llama-server" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    $remote = @($ALLOW_FROM -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    New-NetFirewallRule -DisplayName "flash-next llama-server" -Direction Inbound -Protocol TCP -LocalPort $PORT -Profile Any `
        -RemoteAddress $remote -Program $prog -Action Allow | Out-Null
    Write-Host "Firewall: TCP $PORT open to $($remote -join ', ') for llama-server.exe"
    if ($LISTEN -eq "127.0.0.1") { Write-Warning "LISTEN is 127.0.0.1, so the server only answers this PC; remove that line from config.local.ps1" }
    if (-not $API_KEY) { Write-Warning "the server has no API key: anyone on the network can use it. Run .\setup.ps1 apikey" }
}

function Invoke-Tune {
    if (-not (IsAdmin)) { throw "run this from an elevated PowerShell (Run as administrator)" }
    powercfg /change standby-timeout-ac 0 | Out-Null
    powercfg /change hibernate-timeout-ac 0 | Out-Null
    Write-Host "Sleep and hibernate disabled on AC power (a sleeping box drops the server)"
    Add-MpPreference -ExclusionPath $FLASHNEXT_HOME
    Write-Host "Defender real-time scanning excludes $FLASHNEXT_HOME"
    Invoke-Firewall
    Write-Host ""
    Write-Host "One manual step: NVIDIA Control Panel > Manage 3D settings > CUDA - Sysmem Fallback Policy = Prefer No Sysmem Fallback."
    Write-Host "Otherwise the driver silently spills VRAM into system RAM when it runs short, and generation slows to a crawl."
}

function Register-ServerTask($trigger, [hashtable]$principal) {
    $log = Join-Path $FLASHNEXT_HOME "server.log"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSScriptRoot\serve.ps1`" -LogFile `"$log`""
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Stop-ScheduledTask -TaskName "flash-next" -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName "flash-next" -Action $action -Trigger $trigger -Settings $settings @principal -Force | Out-Null
    Start-ScheduledTask -TaskName "flash-next"
    return $log
}

function Invoke-Task {
    $log = Register-ServerTask (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME") @{}
    Write-Host "Scheduled task 'flash-next' starts the server when you sign in; log: $log"
    Write-Host "It runs only while you are signed in; .\setup.ps1 service starts it at boot instead."
    Write-Host "Restart after changing settings: .\setup.ps1 restart; remove: Unregister-ScheduledTask flash-next -Confirm:`$false"
}

function Invoke-Service {
    if (-not (IsAdmin)) { throw "run this from an elevated PowerShell (Run as administrator)" }
    $cred = Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" `
        -Message "Your Windows password: Task Scheduler stores it to start the server at boot without anyone signing in"
    if (-not $cred) { throw "cancelled" }
    $log = Register-ServerTask (New-ScheduledTaskTrigger -AtStartup) @{
        User = $cred.UserName; Password = $cred.GetNetworkCredential().Password; RunLevel = "Highest"
    }
    Write-Host "Scheduled task 'flash-next' now starts the server at boot, signed in or not; log: $log"
    Write-Host "Check the log once after a reboot: it must list the RTX 4090 as a CUDA device. If it does not, use .\setup.ps1 task."
    Write-Host "Restart after changing settings: .\setup.ps1 restart; remove: Unregister-ScheduledTask flash-next -Confirm:`$false"
}

function Invoke-Restart {
    if (-not (Get-ScheduledTask -TaskName "flash-next" -ErrorAction SilentlyContinue)) {
        throw "no background server yet: .\setup.ps1 service (at boot) or .\setup.ps1 task (at sign-in) sets it up"
    }
    Stop-ScheduledTask -TaskName "flash-next" -ErrorAction SilentlyContinue
    # stopping the task can leave llama-server.exe running, and the new one then cannot take the port
    Get-Process llama-server -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName "flash-next"
    $log = Join-Path $FLASHNEXT_HOME "server.log"
    Write-Host "Restarted. The model takes a few minutes to load; follow it with:"
    Write-Host "  Get-Content `"$log`" -Tail 30 -Wait"
}

function Invoke-Connect {
    $ips = @(Get-LanAddresses)
    if (-not $ips) { $ips = @("<this-pc-ip>") }
    $ctx = if ($KV_UNIFIED) { $PARALLEL * $CTX_PER_SLOT } else { $CTX_PER_SLOT }
    Write-Host "Chat in a browser on the local network:"
    foreach ($ip in $ips) { Write-Host "  http://${ip}:$PORT" }
    if ($API_KEY) { Write-Host "  (enter the API key under Settings > API Key: $API_KEY)" }
    Write-Host ""
    Write-Host "Huntun: add a server under Model providers (the gear button in its header) with these, or export them before starting it:"
    Write-Host "  export HUNTUN_LLAMACPP_URL=http://$($ips[0]):$PORT"
    if ($API_KEY) { Write-Host "  export HUNTUN_LLAMACPP_KEY=$API_KEY" }
    Write-Host "  export HUNTUN_LLAMACPP_NOTE=`"Qwen3.8-Flash-Next uncensored on an RTX 4090: ~17 tok/s for one session, ~20 total for two, prefill ~500 t/s; good for implementation and tasks cloud models refuse`""
    Write-Host ""
    Write-Host "Huntun reads the model name, $([math]::Round($ctx / 1024))K context and $PARALLEL parallel sessions from the server itself."
    if ($LISTEN -eq "127.0.0.1") { Write-Warning "LISTEN is 127.0.0.1: other machines cannot connect until you remove that line from config.local.ps1" }
}

switch ($Command) {
    "check" { exit (Invoke-Check) }
    "install" { Invoke-Install }
    "login" { Invoke-Login }
    "download" { Invoke-Download }
    "apikey" { Invoke-ApiKey }
    "firewall" { Invoke-Firewall }
    "tune" { Invoke-Tune }
    "task" { Invoke-Task }
    "service" { Invoke-Service }
    "restart" { Invoke-Restart }
    "connect" { Invoke-Connect }
    "all" {
        if ((Invoke-Check) -ne 0) { Write-Host "Fix the [FAIL] items above first (or run the steps one by one)."; exit 1 }
        Invoke-Install; Invoke-Download
        Write-Host ""; Write-Host "Next: .\serve.ps1   (then python ..\bench.py in another window; .\setup.ps1 task to start it at logon)"
    }
    default { Get-Content $PSCommandPath -TotalCount 16 | Select-Object -Skip 1 | ForEach-Object { $_ -replace '^# ?', '' }; exit 1 }
}
