from __future__ import annotations

import json
import os
import subprocess
import sys
import platform
import traceback
import re
from dataclasses import dataclass
from typing import Callable, Any

from src.core.llm_client import LLMClient
from src.utils.config import AppConfig

@dataclass
class BuildStep:
    thought: str
    action_type: str  # shell, python, finish
    action_input: str
    observation: str = ""

class BuildAgent:
    """交互式增量执行 Agent (类 OpenCode 模式)"""
    
    def __init__(self, config: AppConfig):
        self.config = config
        self.llm = LLMClient(config.llm)
        self.steps: list[BuildStep] = []

    def run(
        self, 
        user_query: str, 
        history: list[tuple[str, str]] | None = None,
        on_step: Callable[[BuildStep], bool] | None = None # 返回 False 则终止
    ) -> str:
        """主循环：思考 -> 行动 -> 观察"""
        
        current_os = platform.system()
        system_prompt = (
            f"你是一个高级系统工程师（类交互式执行模式）。当前运行环境为: {current_os}。\n"
            "你的任务是根据当前操作系统的语法直接操作本机系统来解决用户需求。\n"
            "你可以使用以下工具：\n"
            "1. shell: 执行终端命令\n"
            "2. python: 执行 Python 代码块\n"
            "3. finish: 任务完成并输出总结\n\n"
            "输出格式必须严格为 JSON 对象：\n"
            '{"thought": "思考过程", "action": "shell|python|finish", "input": "命令或代码内容"}\n\n'
            "规范：\n"
            f"- 必须使用适用于 {current_os} 的语法。\n"
            "- 严禁虚报成功！必须根据 Observation 中的内容判断是否真正执行成功。\n"
            "- 如果 Observation 中包含 Error、Exception 或 ExitCode 非 0，必须承认失败并在 Thought 中分析原因进行修复。\n"
            "- 每次只执行一个小步骤，观察结果后再决定下一步。"
        )

        context = f"用户需求：{user_query}\n"
        if history:
            context += "\n历史背景：\n" + "\n".join([f"Q: {q}\nA: {a}" for q, a in history[-5:]])

        max_steps = 15
        for i in range(max_steps):
            # 1. 组装当前 Prompt
            current_prompt = context + "\n\n当前步骤历史：\n"
            for s in self.steps:
                current_prompt += f"Thought: {s.thought}\nAction: {s.action_type}({s.action_input})\nObservation: {s.observation}\n\n"
            
            current_prompt += "请给出下一步行动的 JSON。"

            # 2. LLM 决策
            try:
                resp_text = self.llm.generate(system_prompt, current_prompt)
                # 尝试解析 JSON
                decision = self._parse_json(resp_text)
                if not decision:
                    return f"解析决策失败：{resp_text}"
                
                step = BuildStep(
                    thought=decision.get("thought", ""),
                    action_type=decision.get("action", "finish"),
                    action_input=decision.get("input", "")
                )
                
                # 3. 回调通知 (包含授权检查)
                if on_step:
                    allowed = on_step(step)
                    if not allowed:
                        return "用户已取消执行。"

                # 4. 执行行动
                if step.action_type == "finish":
                    return step.action_input or "任务已完成。"
                
                observation = self._execute(step.action_type, step.action_input)
                step.observation = observation
                self.steps.append(step)

            except Exception as e:
                return f"执行循环异常：{str(e)}\n{traceback.format_exc()}"

        return "已达到最大执行步数限制。"

    def _parse_json(self, text: str) -> dict | None:
        try:
            # 尝试提取代码块中的内容
            clean = re.sub(r"```json\n?|\n?```", "", text).strip()
            return json.loads(clean)
        except:
            # 尝试模糊匹配第一个 { 到最后一个 }
            import re
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                try: return json.loads(m.group())
                except: return None
            return None

    def _execute(self, action_type: str, action_input: str) -> str:
        if action_type == "shell":
            try:
                res = subprocess.run(
                    action_input, 
                    shell=True, 
                    capture_output=True, 
                    text=True, 
                    encoding="utf-8", 
                    errors="ignore",
                    timeout=60
                )
                return f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}\nExitCode: {res.returncode}"
            except Exception as e:
                return f"执行 Shell 报错: {str(e)}"
        
        elif action_type == "python":
            # 简单的 exec，捕获 stdout
            import io
            import contextlib
            f = io.StringIO()
            try:
                with contextlib.redirect_stdout(f):
                    exec(action_input, globals())
                return f"Output:\n{f.getvalue()}"
            except Exception as e:
                return f"执行 Python 报错: {str(e)}\n{traceback.format_exc()}"
        
        return f"未知的行动类型: {action_type}"
