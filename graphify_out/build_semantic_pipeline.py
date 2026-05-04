import json
import re
import os
import hashlib
import sys
import yaml
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

# 环境准备
BASE_DIR = Path(r"d:/project/wikicode")
sys.path.append(str(BASE_DIR))
from src.core.llm_client import LLMClient

CACHE_DIR = BASE_DIR / "graphify_out/semantic_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

def get_content_hash(content):
    return hashlib.md5(content.encode('utf-8')).hexdigest()

def extract_semantics_expert(content: str, filename: str, llm: LLMClient):
    """专家级语义提取器：深度解析业务规则"""
    c_hash = get_content_hash(content)
    cache_path = CACHE_DIR / f"{c_hash}.json"
    
    if cache_path.exists():
        try: return json.loads(cache_path.read_text(encoding='utf-8'))
        except: pass

    sys_prompt = "你是一个业务流程审计专家。请从文档中提取原子化的业务规则，必须包含结构化的 properties (如数值、比例、条件)。"
    user_prompt = f"文件: {filename}\n内容: {content[:2000]}\n请提取JSON数组 (含label, content, properties):"
    
    try:
        response = llm.generate(sys_prompt, user_prompt)
        # 兼容不同的字段名
        response = response.replace('"ruleName":', '"label":')
        matches = re.search(r'\[.*\]', response, re.DOTALL)
        if matches:
            atoms_raw = json.loads(matches.group())
            atoms = []
            for a in atoms_raw:
                atoms.append({
                    "id": f"atom_{filename}_{hash(a.get('label',''))}",
                    "label": a.get("label", "Logic"),
                    "type": "semantic_atom",
                    "content": a.get("content", a.get("description", "")),
                    "properties": a.get("properties", {}),
                    "parent_file": filename,
                    "keywords": re.findall(r'[\u4e00-\u9fa5]{2,}', a.get("content", ""))
                })
            cache_path.write_text(json.dumps(atoms, ensure_ascii=False), encoding='utf-8')
            return atoms
    except:
        pass
    return []

def run_expert_build():
    # 1. 初始化
    with open(BASE_DIR / ".wikicoder/config.yaml", 'r', encoding='utf-8') as f:
        config_raw = yaml.safe_load(f)
    
    class MockConfig:
        def __init__(self, d): self.__dict__.update(d)
    
    llm = LLMClient(MockConfig(config_raw['llm']))
    
    raw_dir = Path(r"D:/lihq_obsi/lihq_obsi/LLM_wiki/raw")
    output_json = BASE_DIR / "graphify_out/.graphify_pure_merged.json"
    files = list(raw_dir.glob("**/*.md"))
    
    print(f"--- [EXPERT MODE] Starting sync for {len(files)} files ---")
    
    all_nodes = []
    all_edges = []
    
    def process_file(f_path):
        f_name = f_path.name
        f_id = f"file_{f_name}"
        nodes = [{"id": f_id, "label": f_name, "type": "file", "color": "#00ffff"}]
        edges = []
        try:
            content = f_path.read_text(encoding='utf-8', errors='ignore')
            atoms = extract_semantics_expert(content, f_name, llm)
            for a in atoms:
                a["color"] = "#ff9900"
                nodes.append(a)
                edges.append({"from": f_id, "to": a["id"], "label": "defines"})
            print(f"DONE: {f_name} (Atoms: {len(atoms)})")
            return nodes, edges
        except: return [], []

    # 采用稳健并发
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(process_file, files))

    for n, e in results:
        all_nodes.extend(n); all_edges.extend(e)

    # 汇总并保存
    result = {"nodes": all_nodes, "edges": all_edges}
    output_json.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    print(f"--- [COMPLETED] Total Rich Nodes: {len(all_nodes)} ---")

if __name__ == "__main__":
    run_expert_build()
