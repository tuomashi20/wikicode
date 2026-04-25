from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Any

from src.core.agent import WikiFirstAgent, AgentResponse
from src.utils.config import AppConfig

class ScriptPipeline:
    def __init__(self, agent: WikiFirstAgent, config: AppConfig, on_status: Callable[[str], None] | None = None):
        self.agent = agent
        self.config = config
        self.on_status = on_status

    def _notify(self, msg: str):
        if self.on_status:
            self.on_status(msg)

    def run(self, user_query: str, history: list[tuple[str, str]] | None = None) -> AgentResponse:
        self._notify("pipeline_start: 开始自动化执行流程")
        
        # 1. 探测环境
        self._notify("probe: 正在进行环境探测...")
        probe_code = self._generate_probe_code(user_query, history)
        if not probe_code:
            return AgentResponse(thought="probe_failed", actions=[], output="无法生成环境探测脚本。")

        probe_res = self._execute_probe(probe_code)
        self._notify(f"probe_completed: 探测完成 (状态: {probe_res['status']})")
        
        # 2. 生成并执行业务脚本
        self._notify("build: 正在生成业务执行脚本...")
        script_code = self._generate_task_code(user_query, probe_res['summary'], history)
        if not script_code:
            return AgentResponse(thought="build_failed", actions=[], output="无法生成业务执行脚本。")

        # 3. 运行并自动修复
        self._notify("execute: 正在执行脚本并启动自动修复逻辑...")
        final_resp = self._execute_and_fix_loop(user_query, script_code, probe_res['summary'], history)
        
        return final_resp

    def _generate_probe_code(self, query: str, history: list[tuple[str, str]] | None) -> str:
        prompt = (
            "请生成一个只读的 Python 探测脚本，用于分析用户需求涉及的环境和数据结构。\n"
            "要求：\n"
            "1) 检测操作系统类型、版本及包管理器（如 apt, yum, brew）\n"
            "2) 探测涉及的路径或文件是否存在，若是表格文件请报告结构（行列、空值等）\n"
            "3) 最后在 stdout 输出一行：WIKICODER_PROBE_JSON=<json>（使用 json.dumps 输出）\n"
            "4) 仅输出 Python 代码，不要解释"
        )
        resp = self.agent.run(prompt + f"\n\n用户需求：{query}", mode="general_only", history=history)
        return self._extract_code(resp.output)

    def _execute_probe(self, code: str) -> dict:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = Path(f"wikicoder_probe_{ts}.py")
        path.write_text(code, encoding="utf-8")
        try:
            res = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, encoding="utf-8", errors="ignore")
            probe_json = ""
            for line in res.stdout.splitlines():
                if line.startswith("WIKICODER_PROBE_JSON="):
                    probe_json = line.replace("WIKICODER_PROBE_JSON=", "").strip()
            return {"status": "ok" if res.returncode == 0 else "failed", "summary": probe_json or res.stdout[:1000]}
        finally:
            if path.exists(): path.unlink()

    def _generate_task_code(self, query: str, probe_summary: str, history: list[tuple[str, str]] | None) -> str:
        prompt = (
            "你将根据探测结果实现自动化脚本。请仅输出完整 Python 代码，不要解释。\n\n"
            "=== 代码规范 ===\n"
            "1) 必须包含 if __name__ == '__main__':\n"
            "2) 若涉及系统操作（如 UOS/Linux 安装软件），请使用 subprocess.run 调用系统命令\n"
            "3) 优先处理权限问题，若需 sudo 请确保逻辑闭环\n"
            "4) 写文件使用 utf-8 编码\n"
            "5) 打印关键进度，最终输出结果\n"
            f"环境探测结果：{probe_summary}"
        )
        resp = self.agent.run(prompt + f"\n\n用户需求：{query}", mode="general_only", history=history)
        return self._extract_code(resp.output)

    def _execute_and_fix_loop(self, query: str, code: str, probe_summary: str, history: list[tuple[str, str]] | None) -> AgentResponse:
        current_code = code
        attempt = 1
        all_logs = []
        
        while attempt <= 5:
            self._notify(f"execute_attempt: 第 {attempt} 轮执行中...")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            script_path = Path(f"wikicoder_task_{ts}.py")
            script_path.write_text(current_code, encoding="utf-8")
            
            try:
                res = subprocess.run([sys.executable, str(script_path)], capture_output=True, text=True, encoding="utf-8", errors="ignore")
                log = f"--- 第 {attempt} 轮执行结果 ---\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
                all_logs.append(log)
                
                if res.returncode == 0:
                    self._notify("execute_success: 脚本执行成功")
                    return AgentResponse(thought="success", actions=[f"attempt_{attempt}"], output=f"执行成功！\n\n{log}")
                
                self._notify(f"execute_error: 执行失败，正在分析错误并进行第 {attempt+1} 轮修复...")
                current_code = self._generate_fix_code(query, current_code, log, attempt, history)
                if not current_code: break
                attempt += 1
            finally:
                if script_path.exists(): script_path.unlink()
        
        return AgentResponse(thought="max_retries", actions=[], output=f"已达到最大修复次数，仍未解决：\n\n" + "\n".join(all_logs))

    def _generate_fix_code(self, query: str, old_code: str, error_log: str, attempt: int, history: list[tuple[str, str]] | None) -> str:
        prompt = (
            f"脚本在第 {attempt} 轮执行时报错。请修复以下代码中的错误并输出完整的修复版 Python 代码。\n"
            "仅输出代码，不要解释。重点关注报错信息中的具体原因（如依赖缺失、权限不足、路径错误等）。\n\n"
            f"报错日志：\n{error_log}\n\n"
            f"原始代码：\n{old_code}"
        )
        resp = self.agent.run(prompt, mode="general_only", history=history)
        return self._extract_code(resp.output)

    def _extract_code(self, text: str) -> str:
        m = re.search(r"```python\n(.*?)```", text, re.DOTALL)
        if m: return m.group(1).strip()
        m = re.search(r"```\n(.*?)```", text, re.DOTALL)
        if m: return m.group(1).strip()
        return text.strip()
