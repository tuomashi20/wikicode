import os
import sys
import yaml
import subprocess
from pathlib import Path

def main():
    print("🚀 开始为 gbrain 注入九天 (Jiutian) 模型配置...")
    config_path = Path("D:/project/wikicode/.wikicoder/config.yaml")
    
    if not config_path.exists():
        print("❌ 找不到 WikiCoder 配置文件。")
        sys.exit(1)
        
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    llm_cfg = data.get("llm", {})
    
    api_key = llm_cfg.get("api_key")
    model = llm_cfg.get("model", "jiutian-lan-comv3")
    base_url = llm_cfg.get("base_url") or "https://jiutian.10086.cn/largemodel/moma/api/v3"
    
    if not api_key:
        print("❌ 配置文件中未找到 Jiutian API Key。")
        sys.exit(1)
        
    print(f"✅ 已提取九天 API Key: {api_key[:15]}...")
    print(f"✅ Base URL: {base_url}")
    print(f"✅ 模型: {model}")
    
    # 构造兼容 OpenAI 协议的环境变量
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = api_key
    env["OPENAI_BASE_URL"] = base_url
    env["OPENAI_MODEL_NAME"] = model
    # 如果 gbrain 强制要求 Anthropic，我们可以设一个假的防止报错，优先走 OpenAI 兼容层
    env["ANTHROPIC_API_KEY"] = "sk-ant-dummy"
    
    gbrain_dir = Path("D:/project/wikicode/gbrain_core")
    
    print("\n⚙️ 正在执行 gbrain init (免交互模式)...")
    try:
        # 使用 echo 自动回车跳过可能的交互提示，或依赖环境变量
        subprocess.run(
            "bun run gbrain init", 
            cwd=str(gbrain_dir), 
            env=env, 
            shell=True,
            check=True
        )
        print("🎉 gbrain 初始化成功！")
        
        # 将环境变量保存到一个专门的 .env 文件，供后续 MCP Server 启动时使用
        env_file = gbrain_dir / ".env"
        env_file.write_text(f"""
OPENAI_API_KEY={api_key}
OPENAI_BASE_URL={base_url}
OPENAI_MODEL_NAME={model}
ANTHROPIC_API_KEY=sk-ant-dummy
""", encoding="utf-8")
        print(f"✅ 已将九天模型参数固化到 {env_file}")

    except Exception as e:
        print(f"⚠️ gbrain init 遇到问题 (可能需要手动干预): {e}")

if __name__ == "__main__":
    main()
