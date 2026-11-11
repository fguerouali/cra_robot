<#
  Deploys a checkout of the repo to the server and restarts the app.
  Called by .github/workflows/deploy.yml on every push, and once by setup-server.ps1.
  Runtime files (Chrome session, history, PDFs, config) live in $Root\data and are never touched.
#>
param(
    [string]$Source = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$Root = "C:\cra_robot",
    [int]$Port = 8765,
    [string]$TaskName = "CRA Robot",
    [int]$WaitForRunMinutes = 30
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$app = "$Root\app"
$state = "http://127.0.0.1:$Port/api/state"

function Get-AppState {
    try { return Invoke-RestMethod $state -TimeoutSec 5 } catch { return $null }
}

# Do not cut an export in the middle.
$deadline = (Get-Date).AddMinutes($WaitForRunMinutes)
while (($s = Get-AppState) -and ($s.running -or $s.session.checking) -and (Get-Date) -lt $deadline) {
    Write-Host "An export or sign-in is in progress; waiting before restarting"
    Start-Sleep -Seconds 20
}

Write-Host "Stopping the app"
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$appPattern = [regex]::Escape("$app\server.py")
$profilePattern = [regex]::Escape("$Root\data\chrome-profile")
Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'chrome.exe' OR Name = 'chromedriver.exe'" |
    Where-Object { $_.Name -eq "chromedriver.exe" -or $_.CommandLine -match $appPattern -or $_.CommandLine -match $profilePattern } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

Write-Host "Copying $Source to $app"
robocopy $Source $app /MIR /NFL /NDL /NJH /NP `
    /XD .git .github .agents .venv __pycache__ chrome-profile downloads logs docker `
    /XF .env runs.jsonl runs.jsonl.bak session.json | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with code $LASTEXITCODE" }
$global:LASTEXITCODE = 0

if (-not (Test-Path "$Root\venv\Scripts\python.exe")) {
    Write-Host "Creating the virtualenv"
    & "C:\Program Files\Python312\python.exe" -m venv "$Root\venv"
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}
& "$Root\venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r "$app\requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# Seed the resource list once; after that the UI owns $Root\data\config.json.
New-Item -ItemType Directory -Force "$Root\data" | Out-Null
if (-not (Test-Path "$Root\data\config.json")) { Copy-Item "$app\config.json" "$Root\data\config.json" }

Write-Host "Starting the app"
Start-ScheduledTask -TaskName $TaskName
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    if (Get-AppState) { Write-Host "App is up on port $Port"; exit 0 }
}

$owner = (Get-ScheduledTask -TaskName $TaskName).Principal.UserId
$loggedOn = (quser 2>$null) -match [regex]::Escape(($owner -split '\\')[-1])
if (-not $loggedOn) {
    throw "Code deployed, but the app could not start: $owner has no open session on the server. " +
          "Connect over RDP once (then disconnect, do not sign out) and the app starts at logon."
}
throw "The app did not answer on $state. See $Root\server.log"
