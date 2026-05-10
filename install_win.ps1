# WikiCoder Windows Installer (Industrialized V4.3 - Pure Python)
$OutputEncoding = [System.Text.Encoding]::UTF8
$ConfirmPreference = "None"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "   __      ___ _    _  _____  ____  _____  " -ForegroundColor Cyan
Write-Host "   \ \    / (_) |  (_)/ ____|/ __ \|  __ \ " -ForegroundColor Cyan
Write-Host "    \ \  / / _| | ___| |    | |  | | |  | |" -ForegroundColor Cyan
Write-Host "     \ \/ / | | |/ / | |    | |  | | |  | |" -ForegroundColor Cyan
Write-Host "      \  /  | |   <| | |____| |__| | |__| |" -ForegroundColor Cyan
Write-Host "       \/   |_|_|\_\_|\_____|\____/|_____/ " -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "[WikiCoder] 启动工业级安装引擎 (纯净版)..." -ForegroundColor Cyan

# 0. 强制切换到脚本所在目录
Set-Location -Path $PSScriptRoot

# 1. 检查并安装 uv (Python 极速管家)
if (!(Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[WikiCoder] 正在安装 uv 环境引擎..." -ForegroundColor Yellow
    try {
        Invoke-RestMethod -Uri https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Write-Error "uv 安装失败: $_"
    }
    $env:Path += ";$HOME\.cargo\bin;$HOME\.local\bin;$env:APPDATA\uv\bin"
}

# 2. 同步 Python 依赖环境
Write-Host "[WikiCoder] 正在构建 Python 虚拟环境与依赖同步..." -ForegroundColor Cyan
& uv sync
if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] uv sync 失败，尝试强制修复..." -ForegroundColor Yellow
    & uv venv --quiet
}

# 3. 基础设施初始化 (目录与配置)
Write-Host "[WikiCoder] 正在初始化基础设施目录..." -ForegroundColor Cyan
$Dirs = @("wiki", ".wikicoder", "data", "logs", "scratch")
foreach ($d in $Dirs) {
    if (!(Test-Path $d)) { New-Item -ItemType Directory -Path $d | Out-Null }
}

# 4. 注册全局命令 (wikicoder)
Write-Host "[WikiCoder] 正在注册全局命令行指令..." -ForegroundColor Cyan
$LauncherDir = "$HOME\.wikicoder\bin"
if (!(Test-Path $LauncherDir)) { New-Item -ItemType Directory -Path $LauncherDir | Out-Null }

$ProjectDir = $PSScriptRoot
$BatContent = "@echo off`r`nuv --project ""$ProjectDir"" run python ""$ProjectDir\src\main.py"" %*"
Set-Content -Path "$LauncherDir\wikicoder.bat" -Value $BatContent -Encoding Ascii

# 更新 PATH (User 级别)
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$LauncherDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$UserPath;$LauncherDir", "User")
    Write-Host "[WikiCoder] 用户 PATH 已更新。" -ForegroundColor Green
}

# 5. 首次知识库编译
Write-Host "[WikiCoder] 执行首次知识库编译与同步..." -ForegroundColor Cyan
& "$LauncherDir\wikicoder.bat" sync

# 6. 注册后台服务 (任务计划程序)
Write-Host "[WikiCoder] 正在注册后台常驻服务 (Task Scheduler)..." -ForegroundColor Cyan
$TaskName = "WikiCoderServer"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$TaskAction = New-ScheduledTaskAction -Execute "$LauncherDir\wikicoder.bat" -Argument "serve start"
$TaskTrigger = New-ScheduledTaskTrigger -AtLogon
$TaskPrincipal = New-ScheduledTaskPrincipal -UserId "$([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)" -LogonType Interactive

try {
    Register-ScheduledTask -Action $TaskAction -Trigger $TaskTrigger -Principal $TaskPrincipal -TaskName $TaskName -Description "WikiCoder 后端服务自动启动" -ErrorAction Stop | Out-Null
    Write-Host "[WikiCoder] 后台服务注册成功，将在下次登录时自动启动。" -ForegroundColor Green
    # 立即启动一次
    & "$LauncherDir\wikicoder.bat" serve start
} catch {
    Write-Host "[!] 权限不足：未能注册开机启动，请尝试以管理员身份运行此脚本。" -ForegroundColor Yellow
}

Write-Host "`n==========================================" -ForegroundColor Green
Write-Host " [WikiCoder] 一键安装圆满完成！" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host "下一步操作指引:"
Write-Host "1. 请重启当前的终端/PowerShell 以使 PATH 生效。"
Write-Host "2. 输入 'wikicoder' 即可进入智能对话终端。"
Write-Host "3. 后端服务已在 http://127.0.0.1:8000 启动。"
Write-Host "=========================================="
Read-Host "安装结束，按回车退出。"
