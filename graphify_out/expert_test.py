import json
import re
import os
import sys
import yaml
from pathlib import Path

# 强制路径
BASE_DIR = Path(r"d:/project/wikicode")
sys.path.append(str(BASE_DIR))

from src.core.llm_client import LLMClient

def expert_single_test():
    log_file = BASE_DIR / "graphify_out/sync_debug.log"
    def log(msg):
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"{msg}\n")
        print(msg)

    log("--- STARTING SINGLE THREAD TEST ---")
    
    # 初始化
    try:
        config_path = BASE_DIR / ".wikicoder/config.yaml"
        with open(config_path, 'r', encoding='utf-8') as f:
            config_raw = yaml.safe_load(f)
        
        class MockConfig:
            def __init__(self, d): self.__dict__.update(d)
        
        llm = LLMClient(MockConfig(config_raw['llm']))
        log("LLM Initialized Success.")
    except Exception as e:
        log(f"INIT ERROR: {e}")
        return

    raw_dir = Path(r"D:/lihq_obsi/lihq_obsi/LLM_wiki/raw")
    files = list(raw_dir.glob("*.md"))
    
    for f_path in files[:3]: # 只跑 3 个
        log(f"Processing: {f_path.name}")
        try:
            content = f_path.read_text(encoding='utf-8', errors='ignore')
            sys_prompt = "你是一个业务逻辑建模专家。请从文档中提取原子化的业务规则，必须包含结构化的 properties (如数值、日期、比例)。"
            user_prompt = f"内容: {content[:1500]}\n提取JSON数组:"
            
            response = llm.generate(sys_prompt, user_prompt)
            log(f"AI RESPONSE RECEIVED ({f_path.name})")
            
            # 存储结果
            out_path = BASE_DIR / f"graphify_out/debug_{f_path.name}.json"
            out_path.write_text(response, encoding='utf-8')
            log(f"SAVED TO: {out_path.name}")
        except Exception as e:
            log(f"PROC ERROR ({f_path.name}): {e}")

if __name__ == "__main__":
    expert_single_test()
