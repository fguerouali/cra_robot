<#
  One-time setup of the Windows server. Run it in an elevated PowerShell, logged in (RDP) as the
  account that will run the app. Keep that session open (disconnect, do not sign out): the app runs in it.
  Power BI sign-in happens in any browser at http://<server-ip>:<port>/signin.

    powershell -ExecutionPolicy Bypass -File .\deploy\setup-server.ps1 [-RunnerToken <token>]

  The token comes from GitHub: repo Settings > Actions > Runners > New self-hosted runner.
  Without it, the GitHub runner (deploy on every push) is skipped; rerun with the token later.
  It installs Python and Chrome, the app task, the firewall rule, the GitHub runner, then deploys once.
#>
param(
    [string]$RunnerToken = "",
    [string]$RepoUrl = "https://github.com/fguerouali/cra_robot",
    [string]$Root = "C:\cra_robot",
    [int]$Port = 8765,
    [string]$ListenAddress = "0.0.0.0",
    [string]$AllowFrom = "Any",
    [string]$TaskName = "CRA Robot",
    [string]$RunnerDir = "C:\actions-runner",
    [string]$RunnerLabel = "cra-rdp",
    # e.g. http://10.10.211.234:8081. SYSTEM (the runner) and the app's chromedriver download do not
    # see the proxy set in the user's Internet options, so it is passed to them explicitly.
    [string]$Proxy = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Run this script from an elevated PowerShell (Run as administrator)." }

$tmp = Join-Path $env:TEMP "cra-setup"
New-Item -ItemType Directory -Force $tmp, $Root, "$Root\data" | Out-Null

# --- Python 3.12 (pillow<11 has no wheels for 3.13) ---------------------------------------------
$python = "C:\Program Files\Python312\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Installing Python 3.12"
    $exe = Join-Path $tmp "python-3.12.8-amd64.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe" -OutFile $exe
    Start-Process $exe -Wait -ArgumentList "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_launcher=1", "Include_test=0"
    if (-not (Test-Path $python)) { throw "Python install failed." }
}

# --- Google Chrome (Selenium Manager fetches the matching chromedriver) -------------------------
$chrome = @("C:\Program Files\Google\Chrome\Application\chrome.exe",
            "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe") | Where-Object { Test-Path $_ }
if (-not $chrome) {
    Write-Host "Installing Google Chrome"
    $msi = Join-Path $tmp "chrome.msi"
    Invoke-WebRequest "https://dl.google.com/chrome/install/googlechromestandaloneenterprise64.msi" -OutFile $msi
    Start-Process msiexec.exe -Wait -ArgumentList "/i", "`"$msi`"", "/qn", "/norestart"
}

# --- Launcher + scheduled task ------------------------------------------------------------------
# The task runs at logon in the user's own session. SIGNIN_IN_BROWSER shows the sign-in at /signin.
@"
@echo off
set APP_DATA=$Root\data
set APP_HOST=$ListenAddress
set APP_PORT=$Port
set SIGNIN_IN_BROWSER=1
$(if ($Proxy) { "set HTTPS_PROXY=$Proxy`r`nset HTTP_PROXY=$Proxy`r`nset NO_PROXY=localhost,127.0.0.1" })
cd /d "$Root\app"
"$Root\venv\Scripts\python.exe" -u "$Root\app\server.py" >> "$Root\server.log" 2>&1
"@ | Set-Content -Encoding ascii "$Root\start-app.cmd"

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Root\start-app.cmd`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Task '$TaskName' runs as $user at logon"

if ($ListenAddress -ne "127.0.0.1") {
    Get-NetFirewallRule -DisplayName "CRA Robot $Port" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName "CRA Robot $Port" -Direction Inbound -Protocol TCP -LocalPort $Port `
        -RemoteAddress $AllowFrom -Action Allow | Out-Null
}

# --- GitHub Actions self-hosted runner (the server is on a private IP GitHub cannot reach) -----
if (-not $RunnerToken) {
    Write-Host "No -RunnerToken: skipping the GitHub runner (deploy on push stays off)"
} elseif (-not (Test-Path "$RunnerDir\.runner")) {
    Write-Host "Installing the GitHub Actions runner"
    New-Item -ItemType Directory -Force $RunnerDir | Out-Null
    $release = Invoke-RestMethod "https://api.github.com/repos/actions/runner/releases/latest" -Headers @{ "User-Agent" = "cra-setup" }
    $asset = $release.assets | Where-Object { $_.name -like "actions-runner-win-x64-*.zip" } | Select-Object -First 1
    $zip = Join-Path $tmp $asset.name
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip
    Expand-Archive $zip -DestinationPath $RunnerDir -Force
    Push-Location $RunnerDir
    try {
        # SYSTEM can stop/start the app task and write to $Root.
        & .\config.cmd --unattended --url $RepoUrl --token $RunnerToken --name "$env:COMPUTERNAME-cra" `
            --labels $RunnerLabel --work _work --runasservice --windowslogonaccount "NT AUTHORITY\SYSTEM" --replace
        if ($LASTEXITCODE -ne 0) { throw "Runner configuration failed (expired token?)." }
    } finally { Pop-Location }
}
if ($Proxy -and (Test-Path "$RunnerDir\.runner")) {
    # The runner reads .env at start and passes it on to jobs (pip install in deploy.ps1).
    "https_proxy=$Proxy`nhttp_proxy=$Proxy`nno_proxy=localhost,127.0.0.1" | Set-Content -Encoding ascii "$RunnerDir\.env"
    Get-Service "actions.runner.*" | Restart-Service
}

# --- First deploy from this copy of the repo ----------------------------------------------------
& "$PSScriptRoot\deploy.ps1" -Source (Resolve-Path "$PSScriptRoot\..").Path -Root $Root -Port $Port -TaskName $TaskName
Write-Host "Done. App: http://<server-ip>:$Port  (from the server: http://127.0.0.1:$Port)"
