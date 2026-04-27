from __future__ import annotations

import json
import os
import subprocess
import sys
import platform
import traceback
import re
from dataclasses import dataclass, field
from typing import Callable, Any

from src.core.llm_client import LLMClient
from src.utils.config import AppConfig

@dataclass
class BuildStep:
    thought: str
    action_type: str  # shell, python, edit_file, read_url, finish
    action_input: str
    self_criticism: str = ""  # 自我批评/反思
    observation: str = ""
    tasks: list[str] = field(default_factory=list)

class BuildAgent:
    """交互式增量执行 Agent (V2.2: 环境感知增强版)"""
    
    def __init__(self, config: AppConfig):
        self.config = config
        self.llm = LLMClient(config.llm)
        self.steps: list[BuildStep] = []
        self.tasks: list[str] = []
        # 记录最近的动作哈希，用于死循环检测
        self._action_history_hashes: list[str] = []

    def run(
        self, 
        user_query: str, 
        history: list[tuple[str, str]] | None = None,
        on_step: Callable[[BuildStep], bool] | None = None
    ) -> str:
        """主循环：计划 -> 思考 -> 行动 -> 观察"""
        
        current_os = platform.system()
        wiki_path = str(self.config.wiki_strategy.raw_path)
        project_root = os.getcwd()
        
        os_note = f"【环境】{current_os}。项目根目录: {project_root}\n"
        os_note += f"【重要】本地 Wiki 存放路径为: {wiki_path}\n"
        if current_os == "Windows":
            os_note += "【提醒】Windows 下请使用 dir, type 等命令，或直接在 Python 中操作路径。PowerShell 语法已自动支持。"

        system_prompt = (
            f"你是一个顶级的系统自动化与情报分析专家。\n{os_note}\n"
            "你可以通过以下动作完成任务：\n"
            "1. shell: 执行终端命令。\n"
            "2. python: 执行 Python 脚本。处理文件、分析数据或【绕过反爬/模拟搜索】的首选方式。\n"
            "3. edit_file: 精准编辑文件。input 格式: {\"path\": \"...\", \"old\": \"...\", \"new\": \"...\"}。\n"
            "4. read_url: 获取网页内容。输入必须是确切的 URL。如果遭遇 403/SSL 错误，严禁重试相同 URL，必须切换策略。\n"
            "5. finish: 任务完成。填写最终报告。\n\n"
            "输出格式严格为 JSON：\n"
            "{\n"
            '  "tasks": ["任务1", "任务2", ...], \n'
            '  "self_criticism": "【复盘与证伪】：上一步是否有效？当前的 action 是否在重复？是否有更直接的路径（如 Python 脚本）？", \n'
            '  "thought": "基于反思后的下一步具体计划", \n'
            '  "action": "shell|python|edit_file|read_url|finish", \n'
            '  "input": "内容"\n'
            "}\n\n"
            "1. 【先反思，后决策】：在给出 thought 之前，必须先在 self_criticism 中对历史进行复盘。如果 Observation 显示没有进展，严禁重复，必须在 self_criticism 中承认失败并提出备选方案。\n"
            "2. 如果用户仅发送简单的问候（如“在吗”、“你好”、“Hello”），请只需礼貌回应并询问任务，【严禁】在此类情况下启动任何文件探测、Shell 执行或网络搜索。\n"
            "3. 严禁连续尝试同一个失败的 action+input。如果一种方案失败，必须立即切换思路。\n"
            "- 【知己知彼】：优先查看 {wiki_path}。不要猜测不存在的系统路径。\n"
            "- 【任务驱动】：第一步必须规划全局。"
        )

        context = f"用户需求：{user_query}\n"
        if history:
            context += "\n历史背景：\n" + "\n".join([f"Q: {q}\nA: {a}" for q, a in history[-5:]])

        max_steps = 30
        for i in range(max_steps):
            current_prompt = context + f"\n\n=== 你的 Wiki 路径 ===\n{wiki_path}\n"
            current_prompt += "\n=== 任务清单 ===\n"
            current_prompt += "\n".join([f"- {t}" for t in self.tasks]) if self.tasks else "(尚未规划)"
            current_prompt += "\n\n=== 执行历史 ===\n"
            for idx, s in enumerate(self.steps):
                current_prompt += f"步骤 {idx+1}:\nThought: {s.thought}\nAction: {s.action_type}\nInput: {s.action_input}\nObservation: {s.observation[:5000]}\n\n"
            
            current_prompt += "--- 请决定下一步行动 (JSON) ---"

            try:
                resp_text = self.llm.generate(system_prompt, current_prompt)
                decision = self._parse_and_clean_decision(resp_text)
                if not decision: return f"解析决策失败: {resp_text}"
                
                if "tasks" in decision and isinstance(decision["tasks"], list):
                    self.tasks = decision["tasks"]
                
                step = BuildStep(
                    self_criticism=decision.get("self_criticism", ""),
                    thought=decision.get("thought", ""),
                    action_type=decision.get("action", "finish").lower(),
                    action_input=decision.get("input", ""),
                    tasks=list(self.tasks)
                )
                
                # --- 死循环硬检测 ---
                action_hash = f"{step.action_type}:{step.action_input.strip()}"
                repeat_count = 0
                for h in reversed(self._action_history_hashes):
                    if h == action_hash:
                        repeat_count += 1
                    else:
                        break
                self._action_history_hashes.append(action_hash)

                if on_step and not on_step(step):
                    return "会话被用户终止。"

                if step.action_type == "finish":
                    return step.action_input
                
                observation = self._execute(step.action_type, step.action_input)
                
                # 如果检测到重复动作，通过 Observation 强制 LLM 切换思路
                if repeat_count >= 1:
                    warning = (
                        f"\n\n【!!! 核心准则冲突警告 !!!】：你正在原地踏步！\n"
                        f"你已经连续 {repeat_count+1} 次执行完全相同的动作且结果没有改变。\n"
                        "禁止再次重试该动作，否则系统将强制中断。请立即切换思路（例如：访问上级页面、换一个搜索关键词、使用 Python 脚本或请求用户提供具体文档）。"
                    )
                    observation = warning + "\n" + observation
                
                if repeat_count >= 3:
                    return f"错误：检测到死循环。连续 4 次执行相同动作 {action_hash}，任务已物理中断以保护 Token 额度。"

                step.observation = observation
                self.steps.append(step)

            except Exception as e:
                return f"运行异常: {str(e)}"

        return "达到最大步数限制。"

    def _parse_and_clean_decision(self, text: str) -> dict | None:
        try:
            m = re.search(r"\{[\s\S]*\}", text)
            return json.loads(m.group()) if m else None
        except: return None

    def _execute(self, action_type: str, action_input: str, sudo_password: str = "") -> str:
        if action_type == "shell":
            try:
                env = os.environ.copy()
                py_dir = os.path.dirname(sys.executable)
                env["PATH"] = py_dir + os.pathsep + env.get("PATH", "")
                
                final_cmd = action_input
                run_kwargs = {}
                if sudo_password and "sudo " in final_cmd:
                    final_cmd = final_cmd.replace("sudo ", "sudo -S ")
                    run_kwargs["input"] = sudo_password + "\n"

                if platform.system() == "Windows":
                    # 针对 PS 的特殊字符转义
                    if "||" in action_input or "&&" in action_input:
                        # 如果包含 Linux 风格的链式命令，尝试转换为 CMD 模式执行
                        final_cmd = f'cmd.exe /c "{action_input}"'
                    else:
                        ps_keywords = ["Get-", "Set-", "$", "ls ", "cp ", "mv "]
                        if any(k in action_input for k in ps_keywords) or "|" in action_input:
                            encoded_cmd = action_input.replace('"', '`"')
                            final_cmd = f'powershell -NoProfile -ExecutionPolicy Bypass -Command "{encoded_cmd}"'
                    final_cmd = f"chcp 65001 > nul && {final_cmd}"
                
                res = subprocess.run(final_cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, env=env, **run_kwargs)
                return f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}\nExitCode: {res.returncode}"
            except Exception as e: return str(e)
        
        elif action_type == "python":
            try:
                # 使用外部进程执行 Python，防止主进程被脚本卡死
                env = os.environ.copy()
                # 注入必要的环境变量，确保编码正确
                env["PYTHONIOENCODING"] = "utf-8"
                
                # 将脚本写入临时文件或直接通过 -c 传入（如果脚本较长建议写入临时文件，此处暂用 -c）
                result = subprocess.run(
                    [sys.executable, "-c", action_input],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    env=env
                )
                return f"Output:\n{result.stdout}\n{result.stderr}"
            except subprocess.TimeoutExpired:
                return "错误：Python 脚本执行超时（60秒）。该操作已被物理终止，请检查网络环境或脚本逻辑。"
            except Exception as e:
                return f"Python 执行异常: {str(e)}"

        elif action_type == "edit_file":
            try:
                params = json.loads(action_input) if isinstance(action_input, str) else action_input
                path, old, new = params.get("path"), params.get("old"), params.get("new")
                if not os.path.exists(path): return "File not found."
                content = open(path, "r", encoding="utf-8").read()
                if old not in content: return "Target string not found."
                if content.count(old) > 1: return "Target string not unique."
                with open(path, "w", encoding="utf-8") as f: f.write(content.replace(old, new))
                return "Success."
            except Exception as e: return str(e)
        
        elif action_type == "read_url":
            try:
                import httpx, trafilatura
                # 增强 headers 模拟
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
                }
                # 尝试忽略 SSL 校验（针对某些老旧或特殊证书站点）
                with httpx.Client(timeout=30.0, follow_redirects=True, headers=headers, verify=False) as client:
                    resp = client.get(action_input)
                    resp.raise_for_status()
                    extracted = trafilatura.extract(resp.text)
                    return f"Content:\n{extracted[:20000] if extracted else '无法提取文本内容'}"
            except Exception as e:
                err_msg = str(e)
                if "403" in err_msg:
                    return f"Error: 403 Forbidden. 对方服务器拒绝了简单的抓取请求。建议：1. 尝试访问首页；2. 使用 python 脚本并设置更复杂的 headers/cookies。"
                elif "SSL" in err_msg:
                    return f"Error: SSL Certificate Error. 证书校验失败。建议：1. 确认 URL 是否正确；2. 尝试 http 而非 https；3. 使用 python 脚本尝试绕过校验。"
                return f"Error: {err_msg}"
        
        return "Unknown action."
