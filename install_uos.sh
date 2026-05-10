#!/bin/bash
# WikiCoder UOS/Linux Installer (Industrialized V4.3 - Pure Python)
set -e

echo "=========================================="
echo "🚀 [WikiCoder] 正在启动一键安装程序 (UOS/Linux)..."
echo "=========================================="

# 0. 锁定脚本所在的物理目录
PROJECT_DIR=$(cd "$(dirname "$0")"; pwd)
cd "$PROJECT_DIR"

# 1. 检查并安装 uv (Python 极速管家)
if ! command -v uv &> /dev/null; then
    echo "📦 [WikiCoder] 正在安装 uv 环境引擎..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

# 2. 同步 Python 依赖环境
echo "🐍 [WikiCoder] 正在构建 Python 虚拟环境与依赖同步..."
if ! uv sync; then
    echo "⚠️ [WikiCoder] uv sync 失败，尝试强制修复..."
    uv venv --quiet
fi

# 3. 基础设施初始化 (目录与配置)
echo "📂 [WikiCoder] 正在初始化基础设施目录..."
mkdir -p wiki .wikicoder data logs scratch

# 4. 注册全局快捷命令 (wikicoder)
echo "⚙️ [WikiCoder] 正在注册全局快捷命令..."
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
LAUNCHER="$BIN_DIR/wikicoder"

cat <<EOF > "$LAUNCHER"
#!/bin/bash
# WikiCoder 启动器 (uv run 规范版)
export PATH="\$HOME/.local/bin:\$HOME/.cargo/bin:\$PATH"
uv --project "$PROJECT_DIR" run python "$PROJECT_DIR/src/main.py" "\$@"
EOF

chmod +x "$LAUNCHER"

# 5. 首次知识库编译与同步
echo "🚀 [WikiCoder] 执行首次知识库编译与同步..."
"$LAUNCHER" sync

# 6. 处理 PATH 环境变量 (永久写入)
SHELL_CONFIG="$HOME/.bashrc"
[ -n "$ZSH_VERSION" ] && SHELL_CONFIG="$HOME/.zshrc"

if ! grep -q ".local/bin" "$SHELL_CONFIG"; then
    echo 'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"' >> "$SHELL_CONFIG"
    echo "🔥 [WikiCoder] 已更新 $SHELL_CONFIG，请执行 'source $SHELL_CONFIG' 生效。"
fi

# 7. 注册 Systemd 用户服务 (实现开机自启)
echo "🛡️ [WikiCoder] 正在配置后台服务自启动 (Systemd)..."
SERVICE_DIR="$HOME/.config/systemd/user"
mkdir -p "$SERVICE_DIR"

cat <<EOF > "$SERVICE_DIR/wikicoder.service"
[Unit]
Description=WikiCoder Backend Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$LAUNCHER serve run
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable wikicoder.service
systemctl --user restart wikicoder.service

echo ""
echo "=========================================="
echo "✅ [WikiCoder] 一键安装圆满完成！"
echo "=========================================="
echo "下一步操作指引:"
echo "1. 执行 'source $SHELL_CONFIG' 或重启终端。"
echo "2. 输入 'wikicoder' 即可进入智能对话终端。"
echo "3. 后端服务已在后台启动 (Systemd)。"
echo "=========================================="
