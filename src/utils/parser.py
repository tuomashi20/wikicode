import re
import json
from typing import Optional, Dict, Any

def parse_react_response(text: str) -> Optional[Dict[str, Any]]:
    """[工业级] JSON 解析器：深度容错，支持缺失大括号或碎片化输出"""
    try:
        # 1. 尝试寻找 Markdown 代码块
        match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        content = match.group(1) if match else text
        
        # 2. 尝试寻找最外层的 {} (最理想情况)
        match_json = re.search(r"(\{.*\})", content, re.DOTALL)
        if match_json:
            try:
                return json.loads(match_json.group(1).strip())
            except:
                pass # 如果整体解析失败，进入字段提取模式
        
        # 3. 字段提取模式 (针对极端不听话的 AI)
        res = {}
        # 强力抠出 thought (支持带或不带引号)
        t_match = re.search(r'["\']?thought["\']?:\s*["\']?(.*?)["\']?(?:,|$)', text, re.IGNORECASE)
        res["thought"] = t_match.group(1).strip() if t_match else "分析中..."
        
        # 强力抠出 action name
        n_match = re.search(r'["\']?name["\']?:\s*["\']?(\w+)["\']?', text)
        action_name = n_match.group(1) if n_match else "summarize"
        
        # 强力抠出 parameters 里的 query 或 content
        # 针对您截图中出现的 "query: FTTR网线连接 结算标准" 这种情况
        p_res = {}
        q_match = re.search(r'["\']?(?:query|content|summary)["\']?:\s*["\']?(.*?)["\']?(?:\s*\}|,|$)', text, re.DOTALL)
        if q_match:
            p_res["query"] = q_match.group(1).strip()
            p_res["content"] = q_match.group(1).strip() # 兼容不同工具
        
        res["action"] = {"name": action_name, "parameters": p_res}
        res["plan"] = [] # Plan 碎了也没关系，不影响执行
        
        return res if action_name else None
    except Exception:
        return None
