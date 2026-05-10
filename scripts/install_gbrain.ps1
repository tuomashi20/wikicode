# scripts/install_gbrain.ps1
Write-Host "🚀 开始为 WikiCoder 宿主安装 gbrain 大脑引擎..." -ForegroundColor Cyan

# 1. 检查并安装 Bun
if (!(Get-Command bun -ErrorAction SilentlyContinue)) {
    Write-Host "📦 未检测到 Bun 运行时，正在为您自动安装 Bun..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri "https://bun.sh/install.ps1" -OutFile "bun-install.ps1"
    powershell.exe -ExecutionPolicy Bypass -File .\bun-install.ps1
    Remove-Item "bun-install.ps1"
    
    # 刷新环境变量 (当前进程)
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","User") + ";" + [System.Environment]::GetEnvironmentVariable("Path","Machine")
    if (!(Get-Command bun -ErrorAction SilentlyContinue)) {
        $env:Path += ";$HOME\.bun\bin"
    }
}

Write-Host "✅ Bun 运行时就绪: $(bun --version)" -ForegroundColor Green

# 2. 克隆 gbrain 代码库
$gbrain_dir = "$PSScriptRoot\..\gbrain_core"
if (Test-Path $gbrain_dir) {
    Write-Host "📂 gbrain 代码库已存在，拉取最新代码..."
    Set-Location $gbrain_dir
    git pull
} else {
    Write-Host "📥 正在下载 gbrain 源码..."
    Set-Location "$PSScriptRoot\.."
    git clone https://github.com/garrytan/gbrain.git gbrain_core
    Set-Location gbrain_core
}

# 3. 初始化与依赖安装
Write-Host "⚙️ 正在安装 gbrain 依赖..." -ForegroundColor Yellow
bun install
bun link

# 4. 自动注入九天模型配置并初始化
Write-Host "🤖 正在为您自动配置九天 (Jiutian) 大模型参数..." -ForegroundColor Cyan
Set-Location "$PSScriptRoot\.."
uv run python scripts/init_gbrain_jiutian.py

Write-Host "🎉 gbrain 基础环境与九天模型已全部自动配置完成！" -ForegroundColor Green
