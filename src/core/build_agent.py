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
        # 针对 Windows 的环境提示
        os_note = ""
        if current_os == "Windows":
            os_note = (
                "【重要】当前为 Windows 环境。系统已自动配置 PATH，你可以直接使用 pip, python, uv。\n"
                "你可以直接输入 PowerShell 命令（如 Get-ChildItem），引擎会自动处理包装。\n"
                "优先使用 'uv add <pkg>' 安装库，速度更快且符合项目规范。"
            )

        system_prompt = (
            f"你是一个顶级的系统自动化专家。当前运行环境为: {current_os}。\n{os_note}\n"
            "你通过不断的'思考-行动-观察'循环来完成任务。每一步只做一个动作，确保结果符合预期。\n"
            "你可以使用以下动作类型：\n"
            "1. shell: 执行终端命令。只需填写纯命令字符串。\n"
            "2. python: 执行 Python 代码。用于复杂逻辑或处理 Excel 等文件。\n"
            "3. finish: 任务完成。填写最终的执行总结报告。\n\n"
            "输出格式必须严格为 JSON：\n"
            '{"thought": "当前步骤的详细思考", "action": "shell|python|finish", "input": "命令内容或代码"}\n\n'
            "准则：\n"
            "- 解决问题：如果遇到 ModuleNotFoundError，立即使用 uv add 安装对应库。\n"
            "- 避免死循环：如果某个命令失败，必须分析原因并尝试【不同】的方法，严禁连续执行完全相同的失败命令。\n"
            "- 观察结果：必须根据 Observation 的具体内容判断成功与否，不要假设命令一定成功。"
        )

        context = f"用户需求：{user_query}\n"
        if history:
            context += "\n历史背景：\n" + "\n".join([f"Q: {q}\nA: {a}" for q, a in history[-5:]])

        max_steps = 25
        for i in range(max_steps):
            # 1. 组装当前 Prompt
            current_prompt = context + "\n\n=== 执行历史 ===\n"
            if not self.steps:
                current_prompt += "(暂无执行历史)\n"
            for idx, s in enumerate(self.steps):
                current_prompt += f"步骤 {idx+1}:\nThought: {s.thought}\nAction: {s.action_type}\nInput: {s.action_input}\nObservation: {s.observation}\n\n"
            
            current_prompt += "--- 请决定下一步行动 (JSON格式) ---"

            # 2. LLM 决策
            try:
                resp_text = self.llm.generate(system_prompt, current_prompt)
                decision = self._parse_and_clean_decision(resp_text)
                if not decision:
                    return f"解析决策失败，LLM 输出内容：\n{resp_text}"
                
                step = BuildStep(
                    thought=decision.get("thought", "无思考内容"),
                    action_type=decision.get("action", "finish").lower(),
                    action_input=decision.get("input", "")
                )
                
                # 兼容性修复
                if "(" in step.action_type:
                    m = re.match(r"(\w+)\((.*)\)", step.action_type)
                    if m:
                        step.action_type = m.group(1).lower()
                        if not step.action_input:
                            step.action_input = m.group(2).strip("'\"")
                
                # 3. 回调通知
                if on_step:
                    allowed = on_step(step)
                    if not allowed:
                        return "用户拒绝执行此步骤，会话终止。"

                # 4. 执行行动
                if step.action_type == "finish":
                    return step.action_input or "任务执行完毕。"
                
                observation = self._execute(step.action_type, step.action_input)
                step.observation = observation
                self.steps.append(step)

            except Exception as e:
                return f"执行循环异常：{str(e)}\n{traceback.format_exc()}"

        return "已达到最大执行步数限制 (25步)。"

    def _parse_and_clean_decision(self, text: str) -> dict | None:
        try:
            import re
            m = re.search(r"\{[\s\S]*\}", text)
            if not m: return None
            data = json.loads(m.group())
            if "action" in data and "input" in data:
                act = data["action"].lower()
                inp = data["input"]
                if act in {"shell", "python"}:
                    wrap_match = re.match(rf"^{act}\(([\s\S]*)\)$", inp.strip(), re.IGNORECASE)
                    if wrap_match:
                        data["input"] = wrap_match.group(1).strip("'\"")
            return data
        except:
            return None

    def _execute(self, action_type: str, action_input: str) -> str:
        if action_type == "shell":
            try:
                env = os.environ.copy()
                # 自动注入当前 Python 环境路径 (确保 pip, uv, python 可用)
                py_dir = os.path.dirname(sys.executable)
                env["PATH"] = py_dir + os.pathsep + env.get("PATH", "")
                
                final_cmd = action_input
                if platform.system() == "Windows":
                    # 1. 自动识别 PowerShell 命令并包装
                    ps_keywords = ["Get-", "Set-", "New-", "Remove-", "Select-", "Where-", "$", "ls ", "cp ", "mv "]
                    is_ps = any(k in action_input for k in ps_keywords) or "|" in action_input
                    if is_ps and not action_input.lower().startswith("powershell"):
                        # 包装成 PS 执行，且避免因为双引号冲突导致的问题
                        encoded_cmd = action_input.replace('"', '`"')
                        final_cmd = f'powershell -NoProfile -ExecutionPolicy Bypass -Command "{encoded_cmd}"'
                    
                    # 2. 强制 UTF-8 编码环境
                    final_cmd = f"chcp 65001 > nul && {final_cmd}"
                
                res = subprocess.run(
                    final_cmd, 
                    shell=True, 
                    capture_output=True, 
                    text=True, 
                    encoding="utf-8", 
                    errors="replace",
                    timeout=90, # 稍微增加超时时间
                    env=env
                )
                return f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}\nExitCode: {res.returncode}"
            except Exception as e:
                return f"执行 Shell 报错: {str(e)}"
        
        elif action_type == "python":
            import io
            import contextlib
            f = io.StringIO()
            try:
                with contextlib.redirect_stdout(f):
                    exec_globals = {"__builtins__": __builtins__, "os": os, "sys": sys, "platform": platform}
                    exec(action_input, exec_globals)
                return f"Output:\n{f.getvalue()}"
            except Exception as e:
                return f"执行 Python 报错: {str(e)}\n{traceback.format_exc()}"
        
        return f"未知的行动类型: {action_type}"
