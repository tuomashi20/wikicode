import os
import sys
import re
import json
import platform
import subprocess
import base64
from dataclasses import dataclass, field
from typing import List, Dict, Any, Callable, Optional
import httpx
import trafilatura

from src.core.llm_client import LLMClient
from src.utils.config import AppConfig
# 引入重构后的规范导航工具
from src.skills.wiki_tools import wiki_search, wiki_list, wiki_read

@dataclass
class BuildStep:
    thought: str
    action_type: str  # wiki_search, wiki_list, wiki_read, shell, python, finish
    action_input: str
    observation: str = ""
    tasks: list[str] = field(default_factory=list)

class BuildAgent:
    """交互式智能 Agent (Hermes-Wiki 范式对齐版)"""
    
    def __init__(self, config: AppConfig, cwd: str = None, depth: int = 0):
        self.config = config
        self.cwd = cwd or os.getcwd()
        self.depth = depth
        self.llm = LLMClient(config.llm)
        self.steps: list[BuildStep] = []
        self.tasks: list[str] = []
        self._action_history_hashes: list[str] = []

    def run(
        self, 
        user_query: str, 
        history: list[tuple[str, str]] | None = None,
        on_step: Callable[[BuildStep], bool] | None = None,
        mode: str = "plan"
    ) -> str:
        self._stop_requested = False
        self._action_history_hashes = [] 
        self.steps = []
        self.tasks = []
        
        current_os = platform.system()
        wiki_root = str(self.config.wiki_strategy.raw_path)
        
        system_prompt = (
            f"你是一个名为 WikiCoder 的顶级的技术情报分析专家。你的目标是利用企业内部知识库精准回答用户问题。\n\n"
            f"【环境上下文】：\n"
            f"- 操作系统: {current_os}\n"
            f"- 当前目录: {self.cwd}\n"
            f"- 知识库根路径: {wiki_root}\n\n"
            "【可用动作集】：\n"
            "1. wiki_search: 语义搜索知识库片段。格式: {\"query\": \"关键词\"}。\n"
            "2. wiki_list: 查看知识库目录结构。格式: {\"sub_dir\": \"\"}。\n"
            "3. wiki_read: 通读指定的规范分册内容。格式: {\"path\": \"运维/分册A.md\"}。\n"
            "4. shell: 执行终端命令。查看代码或系统状态。\n"
            "5. finish: 任务完成。请给出结论并标注知识来源路径（Citations）。\n\n"
            "【Hermes-Wiki 决策准则】：\n"
            "1. **路径溯源**：知识库中的每一条信息都带有 [路径背书]。在回答用户时，必须明确指出该结论出自哪个具体的规范文件。\n"
            "2. **迭代式导航**：如果搜索结果不足以得出结论，必须使用 wiki_list 查看相关目录，并使用 wiki_read 对可能包含答案的规范进行深挖阅读。\n"
            "3. **证据闭环**：严禁凭空想象或带入旧的对话背景。每一步思考都必须基于观察到的确凿证据。\n\n"
            "【输出格式 - 必须严格遵守】：\n"
            "{\n"
            "  \"completed_tasks\": [\"已完成\"],\n"
            "  \"pending_tasks\": [\"待办\"],\n"
            "  \"thought\": \"你的思考过程（必须详细说明你的搜索意图）。\",\n"
            "  \"action\": \"wiki_search|wiki_list|wiki_read|shell|finish\",\n"
            "  \"input\": \"动作参数（字符串或对象）\"\n"
            "}"
        )

        context = f"=== [最高优先级目标] ===\n>>> {user_query} <<<\n"
        if history:
            context += "\n[对话背景]\n" + "\n".join([f"Q: {q}\nA: {a}" for q, a in history[-3:]])

        for i in range(20):
            current_prompt = context + f"\n\n=== 任务进度 ===\n"
            current_prompt += "\n".join([f"- {t}" for t in self.tasks]) if self.tasks else "(未规划)"
            
            current_prompt += "\n\n=== 执行记录 ===\n"
            for idx, s in enumerate(self.steps[-5:]): # 只保留最近 5 步
                obs = s.observation[:3000] if s.observation else ""
                current_prompt += f"步骤 {idx+1}:\nThought: {s.thought}\nAction: {s.action_type}({s.action_input})\nObservation: {obs}\n"
            
            current_prompt += "\n请给出下一步行动的 JSON 响应:"

            try:
                resp_text = self.llm.generate(system_prompt, current_prompt)
                decision = self._parse_json_robustly(resp_text)
                if not decision: return f"解析决策失败: {resp_text}"
                
                action_type = decision.get("action", "")
                action_input = decision.get("input", "")
                if isinstance(action_input, (dict, list)):
                    action_input = json.dumps(action_input, ensure_ascii=False)
                
                self.tasks = decision.get("pending_tasks", [])
                step = BuildStep(
                    thought=decision.get("thought", ""),
                    action_type=action_type,
                    action_input=action_input,
                    tasks=self.tasks
                )

                if action_type == "finish":
                    return action_input

                # 死循环拦截
                action_hash = f"{action_type}:{action_input}".strip()
                if self._action_history_hashes.count(action_hash) >= 2:
                    return f"ERROR: 检测到动作死循环。请尝试不同的关键词或动作。"
                self._action_history_hashes.append(action_hash)

                step.observation = self._execute(action_type, action_input)
                self.steps.append(step)
                
                if on_step and not on_step(step): break
            except Exception as e:
                return f"执行出错: {str(e)}"
        
        return "达到最大执行步数。"

    def _parse_json_robustly(self, text: str) -> Dict[str, Any]:
        """增强型 JSON 解析器：处理物理换行和非标准格式"""
        try:
            start = text.find('{')
            end = text.rfind('}')
            if start == -1 or end == -1: return None
            json_str = text[start:end+1]
            
            # 修复字符串内部的物理换行
            def _fix_newlines(m):
                content = m.group(2).replace('\n', '\\n').replace('\r', '\\r')
                return f'{m.group(1)}"{content}"{m.group(3)}'
            json_str = re.sub(r'(":\s*)"(.*?)"(\s*[,}])', _fix_newlines, json_str, flags=re.DOTALL)
            
            return json.loads(json_str)
        except:
            # 正则保底提取
            res = {}
            for key in ["action", "thought"]:
                m = re.search(fr'"{key}":\s*"([^"]+)"', text)
                if m: res[key] = m.group(1)
            inp_m = re.search(r'"input":\s*({.*?}|"([^"]+)")', text, re.DOTALL)
            if inp_m:
                val = inp_m.group(1)
                res["input"] = json.loads(val) if val.startswith('{') else inp_m.group(2)
            return res if "action" in res else None

    def _execute(self, action_type: str, action_input: str) -> str:
        try:
            if action_type == "wiki_search":
                try: data = json.loads(action_input); q = data.get("query", action_input)
                except: q = action_input
                return wiki_search(str(q), llm=self.llm)

            elif action_type == "wiki_list":
                try: data = json.loads(action_input); sd = data.get("sub_dir", "")
                except: sd = ""
                return wiki_list(sd)

            elif action_type == "wiki_read":
                try: data = json.loads(action_input); p = data.get("path", action_input)
                except: p = action_input
                return wiki_read(p)

            elif action_type == "shell":
                encoded = base64.b64encode(action_input.encode('utf-8')).decode('utf-8')
                res = subprocess.run(f"powershell -NoProfile -EncodedCommand {encoded}", shell=True, capture_output=True, text=True, encoding="utf-8")
                return f"STDOUT: {res.stdout}\nSTDERR: {res.stderr}"

            return "Unknown action."
        except Exception as e:
            return f"执行异常: {str(e)}"
