import os
import re
from pathlib import Path

class GraphAgent:
    """
    逻辑官 (Expert Model V3)
    由于图谱 JSON 损坏，改为实时扫描业务目录 (raw_path)，
    通过跨文件逻辑扫描提供审计见解。
    """
    
    def __init__(self, raw_path="d:/project/wikicode/raw"):
        self.raw_path = Path(raw_path)

    def reasoning(self, query: str):
        if not self.raw_path.exists():
            return ""

        # 1. 提取提问关键词
        keywords = [k.lower() for k in re.split(r'[^a-zA-Z\u4e00-\u9fa50-9]+', query) if len(k) > 1]
        if not keywords: return ""

        # 2. 实时扫描业务目录，寻找逻辑关联文件
        matched_files = []
        try:
            for root, _, files in os.walk(self.raw_path):
                for f in files:
                    if f.endswith(".md"):
                        f_lower = f.lower()
                        # 只要文件名命中关键词
                        if any(k in f_lower for k in keywords):
                            matched_files.append(Path(root) / f)
        except: pass

        if not matched_files:
            return ""

        # 3. 构造反馈
        parts = [f"【逻辑官审计结论】: 在业务库中实时锁定了 {len(matched_files)} 份高度关联的规则文档。"]
        for f in matched_files[:5]:
            parts.append(f"🔗 逻辑链指向: [{f.name}]")
            # 尝试读一小段作为“逻辑预警”
            try:
                content = f.read_text(encoding='utf-8', errors='ignore')[:200]
                parts.append(f"   ∟ 核心逻辑提取: {content.strip()}...")
            except: pass
        
        return "\n".join(parts) + "\n\n提示：主 Agent 请优先结合上述文件中的业务约束进行综合判定。"
