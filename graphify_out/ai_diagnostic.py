import json
import re
import sys
import yaml
import os
from pathlib import Path

sys.path.append(r"d:/project/wikicode")
from src.core.llm_client import LLMClient

def test_ai_extraction():
    config_path = Path("d:/project/wikicode/.wikicoder/config.yaml")
    with open(config_path, 'r', encoding='utf-8') as f:
        config_raw = yaml.safe_load(f)
    
    class MockConfig:
        def __init__(self, d): self.__dict__.update(d)
    
    llm_config = MockConfig(config_raw['llm'])
    llm = LLMClient(llm_config)
    
    raw_dir = Path(r"D:/lihq_obsi/lihq_obsi/LLM_wiki/raw")
    files = list(raw_dir.glob("*.md"))
    if not files:
        print("No files found in raw directory")
        return
    
    sample_file = files[0]
    print(f"Testing with file: {sample_file.name}")
    content = sample_file.read_text(encoding='utf-8', errors='ignore')[:1500]
    
    print("--- SENDING TO AI ---")
    sys_prompt = "你是一个精准的业务逻辑建模专家。请分析文档内容，提取业务规则原子，返回JSON格式。"
    user_prompt = f"请提取以下文档的业务逻辑原子(务必返回JSON数组，每个对象含label, content, properties属性): {content}"
    
    try:
        response = llm.generate(sys_prompt, user_prompt)
        print("--- AI RAW RESPONSE ---")
        print(response)
    except Exception as e:
        print(f"--- FATAL ERROR: {str(e)} ---")

if __name__ == "__main__":
    test_ai_extraction()
