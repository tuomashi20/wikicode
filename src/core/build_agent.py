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
# 引入 RAG 核心工具
from src.skills.wiki_tools import wiki_search_v2, wiki_read_chunk

@dataclass
class BuildStep:
    thought: str
    action_type: str  # shell, python, create_file, edit_file, read_url, spawn_subagent, search_wiki, finish
    action_input: str
    self_criticism: str = ""
    observation: str = ""
    tasks: list[str] = field(default_factory=list)

class BuildAgent:
    """交互式增量执行 Agent (V3.1: 具备知识库感知能力)"""
    
    def __init__(self, config: AppConfig, cwd: str = None, depth: int = 0):
        self.config = config
        self.cwd = cwd or os.getcwd()
        self.depth = depth
        self.llm = LLMClient(config.llm)
        self.steps: list[BuildStep] = []
        self.tasks: list[str] = []
        self.global_snapshot: str = ""
        self._action_history_hashes: list[str] = []

    def run(
        self, 
        user_query: str, 
        history: list[tuple[str, str]] | None = None,
        on_step: Callable[[BuildStep], bool] | None = None,
        mode: str = "plan"
    ) -> str:
        """主循环：计划 -> 思考 -> 行动 -> 观察"""
        # 强制重置当前任务的上下文状态，防止跨任务串扰
        self._stop_requested = False
        self._action_history_hashes = [] 
        self.steps = []
        self.tasks = []
        self.session_mode = mode 
        
        current_os = platform.system()
        wiki_path = str(self.config.wiki_strategy.raw_path)
        project_root = self.cwd
        
        mode_note = f"【当前模式】: {mode.upper()}\n"
        if mode == "plan":
            mode_note += "【核心指令】：你处于【规划模式】。优先使用 search_wiki 检索本地知识库。只能执行只读操作（shell ls/cat, read_url, search_wiki）。"
        else:
            mode_note += "【核心指令】：你处于【构建模式】。可以执行具有副作用的操作。如需参考规范，请使用 search_wiki。"
        
        os_note = f"【环境】{current_os}。当前工作目录: {project_root}\n{mode_note}\n"
        os_note += f"【重要】本地 Wiki 存放路径为: {wiki_path}\n"

        system_prompt = (
            f"你是一个顶级的系统自动化与情报分析专家。\n{os_note}\n"
            "你可以通过以下动作完成任务：\n"
            "1. search_wiki: 语义检索本地知识库。格式: {\"query\": \"关键词或问题\"}。处理业务规范、结算标准、项目制度的首选动作。\n"
            "2. shell: 执行终端命令。查看目录、读取文件。\n"
            "3. create_file: 创建新文件。格式: {\"path\": \"...\", \"content\": \"...\"}。\n"
            "4. python: 执行 Python 脚本。处理文件、分析数据的强大工具。\n"
            "5. edit_file: 精准编辑文件。input 格式: {\"path\": \"...\", \"old\": \"...\", \"new\": \"...\"}。\n"
            "6. read_url: 获取网页内容。用于获取外部实时信息。\n"
            "7. spawn_subagent: 派生子专家 Agent 处理独立的子任务。\n"
            "8. finish: 任务完成。填写最终报告。\n\n"
            "执行准则：\n"
            "1. **当前需求优先**：检索关键词必须严格提取自【当前用户需求】。历史对话仅供参考背景，严禁直接套用历史对话中的旧关键词进行搜索。\n"
            "2. **Wiki 优先**：凡涉及业务逻辑、公司规范、结算标准的询问，必须首先执行 search_wiki。\n"
            "3. **知难而退**：如果执行 search_wiki 两次且结果相似，请【立即停止重试】。在 finish 中如实告知用户你已尽力检索但库中确实缺失该细节。\n"
            "4. **死循环防御**：严禁连续执行语义重复的动作。如果上一步没进展，必须在 self_criticism 中分析原因并更换思维路径。\n"
            "5. **路径锚定**：始终使用相对路径，严禁绝对路径。\n\n"
            "输出格式严格为 JSON：\n"
            "{\n"
            '  "completed_tasks": ["已完成的任务1"], \n'
            '  "pending_tasks": ["待办任务2"], \n'
            '  "self_criticism": "对上一步的反思...", \n'
            '  "thought": "基于反思后的下一步具体计划。", \n'
            '  "action": "search_wiki|shell|python|edit_file|read_url|spawn_subagent|finish", \n'
            '  "input": "内容"\n'
            "}\n\n"
            "注意：在 PLAN 模式下，不要尝试修改代码或创建文件。"
        )

        context = f"### 【当前用户需求 (最高优先级)】 ###\n>>> {user_query} <<<\n"
        if history:
            context += "\n--- 以下为历史对话背景 (仅供语义参考) ---\n"
            context += "\n".join([f"历史 Q: {q}\n历史 A: {a}" for q, a in history[-5:]])
            context += "\n------------------------------------------\n"

        max_steps = 30
        for i in range(max_steps):
            current_prompt = context + f"\n\n=== [当前位置传感器] ===\n当前绝对路径: {self.cwd}\n"
            if self.global_snapshot:
                current_prompt += f"\n=== 【记忆快照】 ===\n{self.global_snapshot}\n"
            current_prompt += "\n=== 任务清单 ===\n"
            current_prompt += "\n".join([f"- {t}" for t in self.tasks]) if self.tasks else "(尚未规划)"
            
            current_prompt += "\n\n=== 执行历史 ===\n"
            total_steps = len(self.steps)
            for idx, s in enumerate(self.steps):
                is_recent = (idx >= total_steps - 3)
                obs = s.observation
                if not is_recent:
                    obs = obs[:100] + f"\n...[已折叠]"
                else:
                    obs = obs[:2500]
                current_prompt += f"步骤 {idx+1}:\nThought: {s.thought}\nAction: {s.action_type}\nInput: {s.action_input}\nObservation: {obs}\n\n"
            
            current_prompt += "--- 下一步行动 (JSON) ---"

            if getattr(self, '_stop_requested', False):
                return "用户已强行终止会话。"

            try:
                resp_text = self.llm.generate(system_prompt, current_prompt)
                decision = self._parse_and_clean_decision(resp_text)
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
                    self_criticism=decision.get("self_criticism", ""),
                    tasks=self.tasks
                )

                if action_type == "finish":
                    return action_input

                # 语义指纹检测（强化死循环拦截）
                action_hash = f"{action_type}:{action_input[:500]}".strip()
                # 提高灵敏度：连续 2 次完全相同即预警，3 次强制中断
                if len(self._action_history_hashes) >= 2 and self._action_history_hashes[-1] == action_hash:
                     step.self_criticism = "[警告] 检测到重复尝试，请务必更换搜索关键词或尝试其他动作！"
                
                if len(self._action_history_hashes) >= 3 and all(h == action_hash for h in self._action_history_hashes[-3:]):
                    return f"ERROR: 检测到死循环动作 ({action_type})。请停止机械重复，当前知识库中可能确实没有你想要的具体信息。"
                self._action_history_hashes.append(action_hash)

                step.observation = self._execute(action_type, action_input)
                self.steps.append(step)
                
                if on_step:
                    if not on_step(step): break
            except Exception as e:
                return f"执行出错: {str(e)}"
        
        return "达到最大执行步数。"

    def _parse_and_clean_decision(self, text: str) -> Dict[str, Any]:
        try:
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if not match: return None
            json_str = match.group(0)
            cleaned = re.sub(r'//.*', '', json_str)
            return json.loads(cleaned)
        except Exception:
            return None

    def _execute(self, action_type: str, action_input: str) -> str:
        current_mode = getattr(self, 'session_mode', 'plan')
        
        # 权限校验
        if current_mode == 'plan' and action_type in ['create_file', 'edit_file', 'spawn_subagent']:
            return f"ERROR: 【规划模式】禁止修改操作。请转而使用 search_wiki 或产出方案。"

        if action_type == "search_wiki":
            try:
                # 兼容性处理：尝试解析 JSON，如果失败则将整个输入视为 query
                try:
                    data = json.loads(action_input)
                    if isinstance(data, dict):
                        query = data.get("query", action_input)
                    else:
                        query = action_input
                except:
                    query = action_input
                
                # 清洗 query：去除可能的 JSON 残留和两端空格
                query = str(query).strip().strip('"').strip("'")
                
                ws = self.config.wiki_strategy
                results, _ = wiki_search_v2(
                    query, limit=5, 
                    synonyms_path=ws.synonyms_path,
                    business_terms_path=ws.business_terms_path,
                    llm=self.llm
                )
                
                if not results:
                    return f"Wiki: 未找到关于 '{query}' 的相关匹配项。请尝试使用更通用的关键词（如：'结算'、'网线'）。"
                
                output = []
                for r in results[:3]:
                    content = wiki_read_chunk(r["chunk_id"])
                    if content:
                        # 确保编码正确并限制长度
                        output.append(f"《{r['title']}》({r['parent_file']}):\n{content[:2000]}")
                
                if not output:
                    return "Wiki: 找到了匹配项但无法读取具体内容，请检查 Wiki 文件编码或路径。"
                    
                return "\n\n".join(output)
            except Exception as e:
                return f"SearchWiki 异常: {str(e)}\n提示：请直接输入关键词或使用 JSON 格式 {{\"query\": \"...\"}}"

        elif action_type == "shell":
            try:
                encoded_cmd = base64.b64encode(action_input.encode('utf-8')).decode('utf-8')
                final_cmd = f"powershell -NoProfile -EncodedCommand {encoded_cmd}"
                res = subprocess.run(final_cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, cwd=self.cwd)
                return f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}\nExit: {res.returncode}"
            except Exception as e:
                return f"Shell 异常: {e}"

        elif action_type == "python":
            try:
                result = subprocess.run([sys.executable, "-c", action_input], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, cwd=self.cwd)
                return f"Output:\n{result.stdout}\n{result.stderr}"
            except Exception as e:
                return f"Python 异常: {e}"

        elif action_type == "read_url":
            try:
                headers = {"User-Agent": "Mozilla/5.0"}
                with httpx.Client(timeout=30.0, follow_redirects=True, headers=headers, verify=False) as client:
                    resp = client.get(action_input)
                    extracted = trafilatura.extract(resp.text)
                    return f"Content:\n{extracted[:15000] if extracted else '无法提取内容'}"
            except Exception as e:
                return f"ReadURL 异常: {e}"
        
        # ... 其他动作逻辑保持原样 ...
        return "Unknown action."
