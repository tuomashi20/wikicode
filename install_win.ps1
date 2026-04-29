# WikiCoder Windows Installer (Pure ASCII Version)
$OutputEncoding = [System.Text.Encoding]::UTF8
$ConfirmPreference = "None"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "[WikiCoder] Starting Installer..." -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# 1. Check/Install uv
if (!(Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[WikiCoder] Installing uv engine..." -ForegroundColor Yellow
    try {
        Invoke-RestMethod -Uri https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Write-Error "Failed to install uv: $_"
    }
    
    $env:Path += ";$HOME\.cargo\bin;$HOME\.local\bin;$env:APPDATA\uv\bin"
}

if (!(Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv not found. Please install manually: https://astral.sh/uv"
    Read-Host "Press Enter to exit"
    exit
}

# 2. Sync Environment
Write-Host "[WikiCoder] Syncing Python environment and deps..." -ForegroundColor Cyan
& uv sync

if ($LASTEXITCODE -ne 0) {
    Write-Host "[WikiCoder] Warning: uv sync failed, trying fallback..." -ForegroundColor Yellow
    & uv venv --quiet
    if (Test-Path "requirements.txt") {
        & uv pip install -r requirements.txt --quiet
    }
}

# 3. Create Launcher
Write-Host "[WikiCoder] Registering global command..." -ForegroundColor Cyan
$LauncherDir = "$HOME\.wikicoder\bin"
if (!(Test-Path $LauncherDir)) { 
    New-Item -ItemType Directory -Path $LauncherDir | Out-Null 
}

$ProjectDir = (Get-Item .).FullName
$BatContent = "@echo off`r`npushd ""$ProjectDir""`r`nuv run python src\main.py %*`r`npopd"

Set-Content -Path "$LauncherDir\wikicoder.bat" -Value $BatContent -Encoding Ascii

# 4. Update PATH
Write-Host "[WikiCoder] Updating User PATH..." -ForegroundColor Cyan
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$LauncherDir*") {
    $NewPath = "$UserPath;$LauncherDir"
    [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
    Write-Host "[WikiCoder] PATH updated." -ForegroundColor Green
}

# 5. Register Auto-start Task
Write-Host "[WikiCoder] Registering auto-start service (Login trigger)..." -ForegroundColor Cyan
$TaskName = "WikiCoderServer"
$TaskAction = New-ScheduledTaskAction -Execute "$LauncherDir\wikicoder.bat" -Argument "serve start"
$TaskTrigger = New-ScheduledTaskTrigger -AtLogon
$TaskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

try {
    Register-ScheduledTask -Action $TaskAction -Trigger $TaskTrigger -Settings $TaskSettings -TaskName $TaskName -Description "WikiCoder Backend Server Auto-start" | Out-Null
    Write-Host "[WikiCoder] Auto-start service registered successfully." -ForegroundColor Green
    
    # Immediately start the service for current session
    Write-Host "[WikiCoder] Starting background service now..." -ForegroundColor Cyan
    & "$LauncherDir\wikicoder.bat" serve start
} catch {
    Write-Host "[WikiCoder] Warning: Failed to register auto-start task. You may need to run as Admin for this step, or skip it." -ForegroundColor Yellow
}

Write-Host "`n==========================================" -ForegroundColor Green
Write-Host "[WikiCoder] Successfully installed!" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host "Next Steps:"
Write-Host "1. Restart your Terminal/PowerShell."
Write-Host "2. Type 'wikicoder' to start."
Write-Host "3. Type 'wikicoder serve' for Obsidian support."
Write-Host "=========================================="
Read-Host "Done! Press Enter to exit."
