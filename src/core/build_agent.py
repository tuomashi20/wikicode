"""
BuildAgent: 双模式 AI 编码助手
- Plan 模式: 纯 LLM 对话（可通过 @wikiagent 注入知识库背景）
- Build 模式: Agent Loop + OpenCode 风格工具集
"""
import os
import sys
import re
import json
import glob as glob_module
import platform
import subprocess
import base64
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Any, Callable, Optional

import httpx
import trafilatura

from src.core.llm_client import LLMClient
from src.core.wiki_agent import WikiAgent, extract_wiki_query
from src.utils.config import AppConfig


@dataclass
class BuildStep:
    """Agent 的单步执行记录"""
    thought: str
    action_type: str  # bash, write, edit, view, grep, glob, ls, fetch, finish
    action_input: str
    observation: str = ""
    tasks: list[str] = field(default_factory=list)
    self_criticism: str = ""


class BuildAgent:
    """双模式 AI 编码助手（Plan / Build）"""

    MAX_STEPS_BUILD = 30

    def __init__(self, config: AppConfig, cwd: str = None, depth: int = 0):
        self.config = config
        self.cwd = cwd or os.getcwd()
        self.depth = depth
        self.llm = LLMClient(config.llm)
        self.wiki_agent = WikiAgent(self.llm)
        self.steps: list[BuildStep] = []
        self.tasks: list[str] = []
        self._action_history_hashes: list[str] = []

    def run(
        self,
        user_query: str,
        history: list[tuple[str, str]] | None = None,
        on_step: Callable[[BuildStep], bool] | None = None,
        on_log: Callable[[str], None] | None = None,
        mode: str = "plan"
    ) -> str:
        """
        根据模式执行任务。

        参数:
            user_query: 用户输入
            history: 对话历史
            on_step: 步骤回调（Build 模式）
            on_log: 日志回调（WikiAgent 进度推送）
            mode: "plan" 或 "build"
        """
        # 1. 处理 @wikiagent
        wiki_context = ""
        clean_query = user_query
        if "@wikiagent" in user_query.lower():
            wiki_query, remaining = extract_wiki_query(user_query)
            if wiki_query:
                if on_log:
                    on_log(f"[系统] 正在调用 WikiAgent 检索: {wiki_query}")
                wiki_context = self.wiki_agent.search(wiki_query, on_log=on_log)
                clean_query = remaining if remaining else wiki_query

        # 2. 分发到对应模式
        if mode == "plan":
            return self._run_plan(clean_query, wiki_context, history)
        else:
            return self._run_build(clean_query, wiki_context, history, on_step, on_log)

    # ========================
    # Plan 模式：纯 LLM 对话
    # ========================
    def _run_plan(
        self,
        query: str,
        wiki_context: str,
        history: list[tuple[str, str]] | None
    ) -> str:
        """Plan 模式：直接与 LLM 对话"""
        system_prompt = (
            "你是 WikiCoder，一个高级技术顾问。\n"
            "你擅长分析问题、制定方案、解读技术文档和回答技术咨询。\n"
            "请用中文回答，保持专业且简洁。"
        )

        user_prompt = ""
        if history:
            user_prompt += "对话历史:\n"
            for q, a in history[-5:]:
                user_prompt += f"用户: {q}\n助手: {a[:300]}\n"
            user_prompt += "\n"

        if wiki_context:
            user_prompt += f"[知识库参考资料]\n{wiki_context}\n\n"

        user_prompt += f"用户问题: {query}"

        try:
            return self.llm.generate(system_prompt, user_prompt)
        except Exception as e:
            return f"LLM 调用失败: {e}"

    # ========================
    # Build 模式：Agent Loop
    # ========================
    def _run_build(
        self,
        query: str,
        wiki_context: str,
        history: list[tuple[str, str]] | None,
        on_step: Callable[[BuildStep], bool] | None,
        on_log: Callable[[str], None] | None
    ) -> str:
        """Build 模式：复刻 OpenCode 的 Agent Loop"""
        self._action_history_hashes = []
        self.steps = []
        self.tasks = []

        current_os = platform.system()
        shell_name = "powershell" if current_os == "Windows" else "bash"

        system_prompt = (
            "你是 WikiCoder Build Agent，一个强大的 AI 编码助手，类似于 OpenCode / Claude Code。\n"
            "你可以自主执行多步操作来完成用户的编码任务。\n\n"
            f"【环境】OS={current_os}, Shell={shell_name}, CWD={self.cwd}\n\n"
            "【可用工具集】：\n"
            "1. bash: 执行终端命令。参数: {\"command\": \"命令\"}\n"
            "2. write: 创建/覆盖文件。参数: {\"file_path\": \"路径\", \"content\": \"完整内容\"}\n"
            "3. edit: 精确替换文件内容。参数: {\"file_path\": \"路径\", \"old_text\": \"原文\", \"new_text\": \"新文\"}\n"
            "4. view: 查看文件。参数: {\"file_path\": \"路径\", \"offset\": 0, \"limit\": 100}\n"
            "5. grep: 搜索代码。参数: {\"pattern\": \"正则\", \"path\": \"目录\", \"include\": \"*.py\"}\n"
            "6. glob: 文件名匹配。参数: {\"pattern\": \"**/*.py\", \"path\": \".\"}\n"
            "7. ls: 列出目录。参数: {\"path\": \".\"}\n"
            "8. fetch: 读取网页。参数: {\"url\": \"https://...\"}\n"
            "9. finish: 任务完成。参数为最终总结报告文本。\n\n"
            "【执行准则】：\n"
            "- 先理解项目结构（ls, glob），再定位代码（grep, view），最后修改（edit, write）\n"
            "- 每步都要有清晰的思考过程\n"
            "- 修改代码后请运行测试验证\n"
            "- 遇到错误时分析原因并尝试修复\n\n"
            "【输出格式 - 严格 JSON】：\n"
            "{\n"
            '  "completed_tasks": ["已完成"],\n'
            '  "pending_tasks": ["待办"],\n'
            '  "thought": "详细思考过程",\n'
            '  "self_criticism": "自我审视（可选）",\n'
            '  "action": "bash|write|edit|view|grep|glob|ls|fetch|finish",\n'
            '  "input": "动作参数（字符串或JSON对象）"\n'
            "}"
        )

        # 构建初始上下文
        context = f"=== 用户任务 ===\n{query}\n"
        if wiki_context:
            context += f"\n[知识库参考资料]\n{wiki_context}\n"
        if history:
            context += "\n[对话背景]\n" + "\n".join(
                [f"Q: {q}\nA: {a[:200]}" for q, a in history[-3:]]
            )

        for i in range(self.MAX_STEPS_BUILD):
            current_prompt = context
            current_prompt += f"\n\n=== 任务进度 ===\n"
            current_prompt += "\n".join([f"- {t}" for t in self.tasks]) if self.tasks else "(未规划)"

            current_prompt += "\n\n=== 执行记录（最近5步）===\n"
            for idx, s in enumerate(self.steps[-5:]):
                obs = s.observation[:3000] if s.observation else ""
                current_prompt += (
                    f"步骤{idx+1}:\n"
                    f"  Thought: {s.thought}\n"
                    f"  Action: {s.action_type}({s.action_input[:200]})\n"
                    f"  Observation: {obs}\n"
                )

            current_prompt += "\n请给出下一步行动的 JSON:"

            try:
                resp_text = self.llm.generate(system_prompt, current_prompt)
                decision = self._parse_json_robustly(resp_text)
                if not decision:
                    return f"解析决策失败: {resp_text[:300]}"

                action_type = decision.get("action", "")
                action_input = decision.get("input", "")
                if isinstance(action_input, (dict, list)):
                    action_input = json.dumps(action_input, ensure_ascii=False)

                self.tasks = decision.get("pending_tasks", self.tasks)
                step = BuildStep(
                    thought=decision.get("thought", ""),
                    action_type=action_type,
                    action_input=action_input,
                    tasks=self.tasks,
                    self_criticism=decision.get("self_criticism", "")
                )

                if action_type == "finish":
                    step.observation = "任务完成"
                    self.steps.append(step)
                    if on_step:
                        on_step(step)
                    return action_input

                # 死循环拦截
                action_hash = f"{action_type}:{action_input}".strip()
                if self._action_history_hashes.count(action_hash) >= 2:
                    return "ERROR: 检测到动作死循环，已自动中止。"
                self._action_history_hashes.append(action_hash)

                # 执行动作
                step.observation = self._execute(action_type, action_input)
                self.steps.append(step)

                if on_step and not on_step(step):
                    break

            except Exception as e:
                return f"执行出错: {e}"

        return "达到最大执行步数（30步），任务未完成。"

    # ========================
    # 工具执行引擎
    # ========================
    def _execute(self, action_type: str, action_input: str, sudo_password: str = "") -> str:
        """执行 Agent 动作"""
        try:
            params = self._parse_params(action_input)

            if action_type == "bash":
                return self._exec_bash(params)
            elif action_type == "write":
                return self._exec_write(params)
            elif action_type == "edit":
                return self._exec_edit(params)
            elif action_type == "view":
                return self._exec_view(params)
            elif action_type == "grep":
                return self._exec_grep(params)
            elif action_type == "glob":
                return self._exec_glob(params)
            elif action_type == "ls":
                return self._exec_ls(params)
            elif action_type == "fetch":
                return self._exec_fetch(params)
            # 兼容旧版动作
            elif action_type == "shell":
                return self._exec_bash({"command": action_input})
            elif action_type == "wiki_search":
                from src.skills.wiki_tools import wiki_search
                try:
                    data = json.loads(action_input)
                    q = data.get("query", action_input)
                except:
                    q = action_input
                return wiki_search(str(q), llm=self.llm)
            elif action_type == "wiki_list":
                from src.skills.wiki_tools import wiki_list
                try:
                    data = json.loads(action_input)
                    sd = data.get("sub_dir", "")
                except:
                    sd = ""
                return wiki_list(sd)
            elif action_type == "wiki_read":
                from src.skills.wiki_tools import wiki_read
                try:
                    data = json.loads(action_input)
                    p = data.get("path", action_input)
                except:
                    p = action_input
                return wiki_read(p)
            else:
                return f"未知动作: {action_type}"

        except Exception as e:
            return f"执行异常: {e}"

    def _parse_params(self, action_input: str) -> dict:
        """将 action_input 解析为参数字典"""
        try:
            return json.loads(action_input)
        except (json.JSONDecodeError, TypeError):
            return {"raw": action_input}

    # --- 各工具实现 ---

    def _exec_bash(self, params: dict) -> str:
        """执行终端命令"""
        cmd = params.get("command") or params.get("raw", "")
        timeout = params.get("timeout", 30)
        if not cmd:
            return "Error: 未提供命令"

        try:
            current_os = platform.system()
            if current_os == "Windows":
                # Windows: 使用 PowerShell
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", cmd],
                    capture_output=True, text=True, encoding="utf-8",
                    cwd=self.cwd, timeout=timeout, errors="replace"
                )
            else:
                # Linux/UOS: 使用 bash
                result = subprocess.run(
                    ["bash", "-c", cmd],
                    capture_output=True, text=True, encoding="utf-8",
                    cwd=self.cwd, timeout=timeout, errors="replace"
                )

            output = ""
            if result.stdout:
                output += f"STDOUT:\n{result.stdout[:5000]}\n"
            if result.stderr:
                output += f"STDERR:\n{result.stderr[:2000]}\n"
            output += f"ExitCode: {result.returncode}"
            return output

        except subprocess.TimeoutExpired:
            return f"Error: 命令超时（{timeout}s）"
        except Exception as e:
            return f"Error: {e}"

    def _exec_write(self, params: dict) -> str:
        """创建或覆盖文件"""
        file_path = params.get("file_path", "")
        content = params.get("content", "")
        if not file_path:
            return "Error: 未提供 file_path"

        try:
            p = Path(self.cwd) / file_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return f"OK: 已写入 {file_path}（{len(content)} 字符）"
        except Exception as e:
            return f"Error: {e}"

    def _exec_edit(self, params: dict) -> str:
        """精确替换文件内容"""
        file_path = params.get("file_path", "")
        old_text = params.get("old_text", "")
        new_text = params.get("new_text", "")
        if not file_path or not old_text:
            return "Error: 需要 file_path 和 old_text"

        try:
            p = Path(self.cwd) / file_path
            if not p.exists():
                return f"Error: 文件不存在 {file_path}"

            content = p.read_text(encoding="utf-8")
            count = content.count(old_text)
            if count == 0:
                return f"Error: 未找到匹配文本（可能有空白差异）"
            if count > 1:
                return f"Warning: 找到 {count} 处匹配，请提供更精确的上下文。仅替换第一处。"

            new_content = content.replace(old_text, new_text, 1)
            p.write_text(new_content, encoding="utf-8")
            return f"OK: 已编辑 {file_path}（替换了 {len(old_text)} → {len(new_text)} 字符）"
        except Exception as e:
            return f"Error: {e}"

    def _exec_view(self, params: dict) -> str:
        """查看文件内容"""
        file_path = params.get("file_path") or params.get("raw", "")
        offset = params.get("offset", 0)
        limit = params.get("limit", 100)
        if not file_path:
            return "Error: 未提供 file_path"

        try:
            p = Path(self.cwd) / file_path
            if not p.exists():
                return f"Error: 文件不存在 {file_path}"

            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            total = len(lines)
            selected = lines[offset:offset + limit]
            result = f"--- {file_path} (行 {offset+1}-{min(offset+limit, total)}/{total}) ---\n"
            for i, line in enumerate(selected, start=offset + 1):
                result += f"{i:4d} | {line}\n"
            return result
        except Exception as e:
            return f"Error: {e}"

    def _exec_grep(self, params: dict) -> str:
        """代码搜索"""
        pattern = params.get("pattern") or params.get("raw", "")
        path = params.get("path", ".")
        include = params.get("include", "")
        if not pattern:
            return "Error: 未提供 pattern"

        try:
            search_path = Path(self.cwd) / path
            results = []
            file_pattern = include or "*"

            for fp in search_path.rglob(file_pattern):
                if fp.is_file() and not any(
                    part.startswith('.') or part in ('__pycache__', 'node_modules', '.venv', '.git')
                    for part in fp.parts
                ):
                    try:
                        text = fp.read_text(encoding="utf-8", errors="replace")
                        for i, line in enumerate(text.splitlines(), 1):
                            if re.search(pattern, line):
                                rel = fp.relative_to(Path(self.cwd))
                                results.append(f"{rel}:{i}: {line.strip()}")
                                if len(results) >= 50:
                                    break
                    except Exception:
                        continue
                if len(results) >= 50:
                    break

            if not results:
                return f"未找到匹配: {pattern}"
            return f"找到 {len(results)} 处匹配:\n" + "\n".join(results)
        except Exception as e:
            return f"Error: {e}"

    def _exec_glob(self, params: dict) -> str:
        """文件名匹配"""
        pattern = params.get("pattern") or params.get("raw", "")
        path = params.get("path", ".")
        if not pattern:
            return "Error: 未提供 pattern"

        try:
            search_path = Path(self.cwd) / path
            files = []
            for fp in search_path.rglob(pattern):
                if not any(
                    part.startswith('.') or part in ('__pycache__', 'node_modules', '.venv')
                    for part in fp.parts
                ):
                    rel = fp.relative_to(Path(self.cwd))
                    files.append(str(rel))
                    if len(files) >= 100:
                        break

            if not files:
                return f"未找到匹配文件: {pattern}"
            return f"匹配 {len(files)} 个文件:\n" + "\n".join(files)
        except Exception as e:
            return f"Error: {e}"

    def _exec_ls(self, params: dict) -> str:
        """列出目录内容"""
        path = params.get("path") or params.get("raw", ".")
        try:
            target = Path(self.cwd) / path
            if not target.exists():
                return f"Error: 路径不存在 {path}"

            items = sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name))
            lines = []
            for item in items[:100]:
                if item.name.startswith('.'):
                    continue
                prefix = "📁" if item.is_dir() else "📄"
                size = ""
                if item.is_file():
                    s = item.stat().st_size
                    size = f" ({s:,} bytes)" if s < 1024*1024 else f" ({s/1024/1024:.1f} MB)"
                lines.append(f"{prefix} {item.name}{size}")

            return f"目录: {path} ({len(lines)} 项)\n" + "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    def _exec_fetch(self, params: dict) -> str:
        """读取网页内容"""
        url = params.get("url") or params.get("raw", "")
        if not url:
            return "Error: 未提供 url"

        try:
            resp = httpx.get(url, timeout=15, follow_redirects=True)
            resp.raise_for_status()
            text = trafilatura.extract(resp.text) or resp.text[:5000]
            return f"--- 网页内容: {url} ---\n{text[:8000]}"
        except Exception as e:
            return f"Error fetching {url}: {e}"

    # ========================
    # JSON 解析工具
    # ========================
    def _parse_json_robustly(self, text: str) -> Dict[str, Any] | None:
        """增强型 JSON 解析器"""
        try:
            start = text.find('{')
            end = text.rfind('}')
            if start == -1 or end == -1:
                return None
            json_str = text[start:end+1]

            # 修复字符串内部的物理换行
            def _fix_newlines(m):
                content = m.group(2).replace('\n', '\\n').replace('\r', '\\r')
                return f'{m.group(1)}"{content}"{m.group(3)}'
            json_str = re.sub(
                r'(\":\s*)\"(.*?)\"(\s*[,}])',
                _fix_newlines, json_str, flags=re.DOTALL
            )

            return json.loads(json_str)
        except json.JSONDecodeError:
            # 正则保底提取
            res = {}
            for key in ["action", "thought", "self_criticism"]:
                m = re.search(fr'"{key}"\s*:\s*"([^"]+)"', text)
                if m:
                    res[key] = m.group(1)
            inp_m = re.search(r'"input"\s*:\s*(\{.*?\}|"([^"]*)")', text, re.DOTALL)
            if inp_m:
                val = inp_m.group(1)
                try:
                    res["input"] = json.loads(val) if val.startswith('{') else inp_m.group(2)
                except json.JSONDecodeError:
                    res["input"] = inp_m.group(2) or val
            return res if "action" in res else None
