import json
import re
from typing import List, Dict, Any

class KnowledgeExtractor:
    """原子模块 1：负责基于 LLM 的结构化知识提取"""

    def __init__(self, llm_client: Any):
        self.llm = llm_client

    def extract(self, text: str) -> List[Dict[str, Any]]:
        """[WikiCoder 动态版] 基于配置指令执行深度提炼"""
        from src.utils.config import load_config
        config = load_config()
        
        # 优先读取配置文件中的提示词
        base_prompt = ""
        try:
            strategy = getattr(config, "wiki_strategy", config)
            prompts = getattr(strategy, "prompts", {})
            if isinstance(prompts, dict):
                base_prompt = prompts.get("knowledge_extraction", "")
            else:
                base_prompt = getattr(prompts, "knowledge_extraction", "")
        except: pass

        if not base_prompt:
            # 极简回退逻辑，确保基础可用性
            base_prompt = "请提取以下片段中的核心实体、职责、界面分界与业务规则，返回 JSON 格式。"

        prompt = f"{base_prompt}\n\n待处理片段：\n---\n{text}\n---\n"
        try:
            # 兼容同步与流式接口，确保返回纯 JSON
            response = self.llm.generate("WikiCoder", prompt)
            return self._parse_json(response)
        except Exception as e:
            print(f"[Extractor Error] {e}")
            return []

    def _parse_json(self, text: str) -> List[Dict[str, Any]]:
        """清洗并解析 LLM 返回的 JSON"""
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        return []

import re
