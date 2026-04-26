#!/bin/bash
set -e

echo "🚀 [WikiCoder] 正在启动一键安装程序 (UOS/Linux)..."

# 1. 检查并安装 uv
if ! command -v uv &> /dev/null; then
    echo "📦 [WikiCoder] 正在安装高性能引擎 uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    source $HOME/.cargo/env
fi

# 2. 创建环境并安装依赖
echo "🐍 [WikiCoder] 正在配置 Python 环境..."
uv venv --quiet
uv pip install -r requirements.txt --quiet

# 3. 创建全局启动脚本
echo "⚙️ [WikiCoder] 正在注册全局快捷命令..."
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"

PROJECT_DIR=$(pwd)
LAUNCHER="$BIN_DIR/wikicoder"

cat <<EOF > "$LAUNCHER"
#!/bin/bash
# WikiCoder 启动器
cd "$PROJECT_DIR"
./.venv/bin/python src/main.py "\$@"
EOF

chmod +x "$LAUNCHER"

# 4. 处理 PATH 环境变量
path_updated=false
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    # 导出到当前脚本进程，以便后续验证
    export PATH="$BIN_DIR:$PATH"
    
    # 永久写入配置文件
    SHELL_CONFIG="$HOME/.bashrc"
    [ -n "$ZSH_VERSION" ] && SHELL_CONFIG="$HOME/.zshrc"
    
    if [ -f "$SHELL_CONFIG" ]; then
        if ! grep -q ".local/bin" "$SHELL_CONFIG"; then
            echo "" >> "$SHELL_CONFIG"
            echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_CONFIG"
            path_updated=true
        fi
    fi
fi

echo ""
echo "=========================================="
echo "✅ [WikiCoder] 安装成功！"
echo "=========================================="

# 5. 立即尝试验证运行
echo "🔍 正在进行自检..."
if "$LAUNCHER" --version &> /dev/null; then
    echo "✨ 启动器验证通过！"
else
    echo "⚠️ 启动器自检异常，请检查 .venv 是否创建成功。"
fi

echo "=========================================="
if [ "$path_updated" = true ]; then
    echo "🔥 [重要] 请执行以下命令使配置立即生效（或者重启终端）："
    echo ""
    echo "    source $SHELL_CONFIG"
    echo ""
else
    echo "🎉 命令已就绪，您可以直接输入 'wikicoder' 启动。"
fi
echo "=========================================="
