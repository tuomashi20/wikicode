# 启动当前 WikiCoder 项目的专属 gbrain WebUI
$GBRAIN_HOME = Join-Path (Get-Location) ".wikicoder\gbrain_home"
$env:GBRAIN_HOME = $GBRAIN_HOME

Write-Host "正在启动 WikiCoder 项目专属记忆管理界面..." -ForegroundColor Cyan
Write-Host "数据库路径: $GBRAIN_HOME" -ForegroundColor Gray

cd gbrain_core
C:\Users\lihq\.bun\bin\bun.exe run src/cli.ts serve --http --port 3131
