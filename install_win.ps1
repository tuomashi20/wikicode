# WikiCoder Windows 安装脚本 (PowerShell 版)
$OutputEncoding = [System.Text.Encoding]::UTF8
$ConfirmPreference = "None"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "[WikiCoder] 正在启动一键安装程序..." -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# 1. 检查并安装 uv
if (!(Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[WikiCoder] 正在为您安装微型 Python 引擎 uv..." -ForegroundColor Yellow
    try {
        Invoke-RestMethod -Uri https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Write-Error "安装 uv 失败: $_"
    }
    
    # 将 uv 路径加入当前会话
    $env:Path += ";$HOME\.cargo\bin;$HOME\.local\bin;$env:APPDATA\uv\bin"
}

if (!(Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "无法安装或找到 uv，请尝试手动安装: https://astral.sh/uv"
    Read-Host "按回车键退出"
    exit
}

# 2. 配置虚拟环境与依赖
Write-Host "[WikiCoder] 正在同步 Python 环境与依赖 (请稍候)..." -ForegroundColor Cyan
& uv sync

if ($LASTEXITCODE -ne 0) {
    Write-Host "[WikiCoder] 警告: uv sync 执行失败，尝试使用 uv pip 安装..." -ForegroundColor Yellow
    & uv venv --quiet
    if (Test-Path "requirements.txt") {
        & uv pip install -r requirements.txt --quiet
    }
}

# 3. 创建全局启动脚本
Write-Host "[WikiCoder] 正在注册全局快捷命令..." -ForegroundColor Cyan
$LauncherDir = "$HOME\.wikicoder\bin"
if (!(Test-Path $LauncherDir)) { New-Item -ItemType Directory -Path $LauncherDir | Out-Null }

$ProjectDir = Get-Location
$BatContent = @"
@echo off
pushd "$ProjectDir"
uv run python src\main.py %*
popd
"@

Set-Content -Path "$LauncherDir\wikicoder.bat" -Value $BatContent -Encoding Ascii

# 4. 将启动器目录加入用户 PATH
Write-Host "[WikiCoder] 正在同步系统路径 (永久生效)..." -ForegroundColor Cyan
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$LauncherDir*") {
    $NewPath = "$UserPath;$LauncherDir"
    [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
    Write-Host "[WikiCoder] 环境变量已更新，重启终端后生效。" -ForegroundColor Green
}

Write-Host "`n==========================================" -ForegroundColor Green
Write-Host "[WikiCoder] ✨ 安装成功！" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host "提示："
Write-Host "1. 请 [关闭并重新打开] 您的终端窗口。"
Write-Host "2. 在任意位置输入 'wikicoder' 即可进入交互界面。"
Write-Host "3. 输入 'wikicoder serve' 启动 Obsidian 后端服务。"
Write-Host "=========================================="
Read-Host "安装完成，按回车键退出"
