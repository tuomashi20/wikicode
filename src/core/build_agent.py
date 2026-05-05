"""
BuildAgent: 双模式 AI 编码助手
- Plan 模式: 纯 LLM 对话（可通过 @wikiagent 注入知识库背景）
- Build 模式: Agent Loop + OpenCode 风格工具集
"""
import os
import sys
import re
import json
import json
import glob as glob_module
import platform
import subprocess
import base64
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Any, Callable, Optional

import httpx
import trafilatura

from src.core.llm_client import LLMClient
from src.core.agent import WikiFirstAgent
from src.core.wiki_agent import extract_wiki_query
from src.utils.config import AppConfig
from src.core.mcp_client import GBrainMCPClient
from src.skills.wiki_tools import wiki_search


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

    MAX_STEPS_BUILD = 100

    def __init__(self, config: AppConfig, cwd: str = None, depth: int = 0):
        self.config = config
        self.cwd = cwd or os.getcwd()
        self.depth = depth
        self.llm = LLMClient(config.llm)
        self.wiki_agent = WikiFirstAgent(config)
        self.steps: list[BuildStep] = []
        self.tasks: list[str] = []
        self._action_history_hashes: list[str] = []
        self.interrupt_signal = False 
        
        try:
            self.gbrain = GBrainMCPClient()
        except:
            self.gbrain = None
    
    def sync(self, on_status=None):
        """委托 WikiAgent 执行深度同步"""
        return self.wiki_agent.sync(on_status=on_status)

    def run(
        self,
        user_query: str,
        history: list[tuple[str, str]] | None = None,
        on_step: Callable[[BuildStep], bool] | None = None,
        on_log: Callable[[str], None] | None = None,
        mode: str = "plan",
        should_stop: Callable[[], bool] | None = None
    ) -> str:
        """
        根据模式执行任务。

        参数:
            user_query: 用户输入
            history: 对话历史
            on_step: 步骤回调（Build 模式）
            on_log: 日志回调（WikiAgent 进度推送）
            mode: "plan" 或 "build"
            should_stop: 可选的中断检查回调
        """
        # [关键映射]：启动即推送，消除黑洞期
        if on_step:
            on_step(BuildStep(
                thought=f"收到老板指令：'{user_query[:20]}...'，正在进入 {mode} 模式进行深度拆解和构思。",
                action_type="thinking",
                action_input="internal_brainstorm"
            ))
            
        self.interrupt_signal = False # 重置中断信号

        # 1. 处理 @wikiagent
        wiki_context = ""
        clean_query = user_query
        if "@wikiagent" in user_query.lower():
            wiki_query, remaining = extract_wiki_query(user_query)
            if wiki_query:
                # 调用新版 WikiFirstAgent
                res = self.wiki_agent.run(
                    wiki_query, 
                    on_status=on_log, 
                    on_step=on_step,
                    response_mode="answer"
                )
                wiki_context = res.output
                clean_query = remaining if remaining else wiki_query

        # 1.5 处理 gbrain 个人记忆 (双脑集成)
        gbrain_context = ""
        if self.gbrain:
            if on_log: on_log("正在从 gbrain 唤醒个人记忆...")
            try:
                # 记录“记住”操作
                if "记住" in clean_query or "保存" in clean_query:
                    import datetime
                    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    slug = f"personal/memory_{int(time.time())}"
                    content = f"---\ntype: memory\ntitle: Saved via BuildAgent at {timestamp}\n---\n\n{clean_query}"
                    self.gbrain.call_tool("put_page", {"slug": slug, "content": content})
                    if on_log: on_log(f"✅ 已存入 gbrain 长效存储: {slug}")
                
                # 检索记忆
                gbrain_res = self.gbrain.call_tool("query", {"query": clean_query})
                from src.utils.logger import get_file_logger
                agent_logger = get_file_logger("agent", "agent.log")
                agent_logger.info(f"gbrain query for '{clean_query}' returned: {gbrain_res}")
                
                if gbrain_res and "No results found" not in gbrain_res and "Error:" not in gbrain_res:
                    gbrain_context = gbrain_res
            except Exception as e:
                from src.utils.logger import get_file_logger
                get_file_logger("agent", "agent.log").error(f"gbrain query failed: {e}")
                pass

        # 2. 分发到对应模式
        if mode == "plan":
            return self._run_plan(clean_query, wiki_context, gbrain_context, history, on_log, should_stop)
        else:
            return self._run_build(clean_query, wiki_context, gbrain_context, history, on_step, on_log, should_stop)

    # ========================
    # Plan 模式：纯 LLM 对话
    # ========================
    def _run_plan(
        self,
        query: str,
        wiki_context: str,
        gbrain_context: str,
        history: list[tuple[str, str]] | None,
        on_log: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None
    ) -> str:
        """Plan 模式：直接与 LLM 对话"""
        system_prompt = (
            "你是 WikiCoder，一个高级技术顾问。\n"
            "你擅长分析问题、制定方案、解读技术文档和回答技术咨询。\n\n"
            "【输出准则】：\n"
            "1. 必须用中文回答，风格专业且有条理。\n"
            "2. 如果提供了 [知识库参考资料]，你的回答必须基于该资料，禁止捏造。\n"
            "3. **必须保留来源引用**：在每一段或每一条核心结论后，请保留或加上 [Source: 相对路径] 格式的来源标注。\n"
            "4. 结构化输出：使用 Markdown 标题、列表、粗体等让内容一目了然。"
        )

        user_prompt = ""
        if history:
            user_prompt += "对话历史:\n"
            for q, a in history[-5:]:
                user_prompt += f"用户: {q}\n助手: {a[:300]}\n"
            user_prompt += "\n"

        if wiki_context:
            user_prompt += f"[知识库参考资料]\n{wiki_context}\n\n"
            
        if gbrain_context:
            user_prompt += f"[gbrain 个人记忆]\n{gbrain_context}\n\n"

        user_prompt += f"用户问题: {query}"

        try:
            full_resp = []
            line_buffer = ""
            for chunk in self.llm.generate_stream(system_prompt, user_prompt):
                # 检查回调或内部信号
                if self.interrupt_signal or (should_stop and should_stop()):
                    full_resp.append("\n\n[任务已由用户手动终止]")
                    break
                
                full_resp.append(chunk)
                if on_log: on_log(chunk)
            if on_log and line_buffer:
                on_log(line_buffer)
                
            return "".join(full_resp)
        except Exception as e:
            return f"LLM 调用失败: {e}"

    # ========================
    # Build 模式：Agent Loop
    # ========================
    def _run_build(
        self,
        query: str,
        wiki_context: str,
        gbrain_context: str,
        history: list[tuple[str, str]] | None,
        on_step: Callable[[BuildStep], bool] | None,
        on_log: Callable[[str], None] | None,
        should_stop: Callable[[], bool] | None = None
    ) -> str:
        """Build 模式：复刻 OpenCode 的 Agent Loop"""
        self._action_history_hashes = []
        self._consecutive_errors = 0
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
            "- 遇到错误时分析原因并尝试修复\n"
            "- 【循环避让】：如果你的某个动作返回了错误，或者你发现自己想重复执行刚才的操作，**禁止盲目重试**。你必须先使用 ls 或 view 确认文件是否存在、内容是否符合预期，或者尝试更换不同的工具（例如用 write 替代失败的 edit）。\n"
            "- 【重要】bash 命令每次只执行一条，不要用 && 或 ; 连接多条命令。如果需要多步，请分多次 bash 调用\n"
            "- 在 Windows 上使用 PowerShell 语法，不要用 bash 特有语法\n\n"
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
        if gbrain_context:
            context += f"\n[gbrain 个人记忆]\n{gbrain_context}\n"
        if history:
            context += "\n[对话背景]\n" + "\n".join(
                [f"Q: {q}\nA: {a[:200]}" for q, a in history[-3:]]
            )

        for i in range(self.MAX_STEPS_BUILD):
            # 检查回调或内部信号
            if self.interrupt_signal or (should_stop and should_stop()):
                return "任务已由用户手动终止"
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

                # 归一化 Hash（忽略空白和路径斜杠差异）
                normalized_input = re.sub(r'[\s/\\\\]+', ' ', action_input).strip()
                action_hash = f"{action_type}:{normalized_input}"
                repeat_count = self._action_history_hashes.count(action_hash)

                if action_type == "finish":
                    step.observation = "任务完成"
                    self.steps.append(step)
                    if on_step:
                        on_step(step)
                    return action_input

                # 智能死循环拦截
                if repeat_count == 1:
                    # 第一次重复：强制干预，喂给它真实的文件列表
                    real_context = self._execute("ls", ".")
                    step.observation = (
                        f"[系统强力警告] 检测到动作完全重复：{action_type}。\n"
                        f"你可能正在陷入死循环！请立即停止重试刚才的操作。\n"
                        f"【当前目录真实状态】:\n{real_context}\n"
                        "请重新审视路径和命令，尝试完全不同的方案（例如：如果启动失败，先查日志或看 package.json）。"
                    )
                    self._action_history_hashes.append(action_hash)
                    self.steps.append(step)
                    if on_step and not on_step(step): break
                    continue # 强制重思考
                elif repeat_count == 2:
                    step.observation = (
                        f"[系统最后通牒] 这是该操作第 3 次重复：{action_type}。\n"
                        "如果你继续重复，系统将为了保护资源强制中止任务。\n"
                        "建议：换一个文件、换一个命令，或者先执行 ls -R 查看全局结构。"
                    )
                    self._action_history_hashes.append(action_hash)
                    self.steps.append(step)
                    if on_step and not on_step(step): break
                    continue
                elif repeat_count >= 3:
                    return f"ERROR: 动作死循环（已重复 {repeat_count + 1} 次：{action_type}）。Agent 无法自主破局，已中止以保护资源。"
                
                self._action_history_hashes.append(action_hash)

                # 执行动作
                step.observation = self._execute(action_type, action_input)
                self.steps.append(step)

                # 连续错误检测：如果连续多次报错，注入强制提示
                if step.observation.startswith("Error"):
                    self._consecutive_errors += 1
                    if self._consecutive_errors >= 3:
                        step.observation += "\n\n[系统警告] 已连续失败3次。请立即更换策略：使用 ls 查看目录、用 write 创建新文件而非 edit，或改用不同的命令。"
                else:
                    self._consecutive_errors = 0

                if on_step and not on_step(step):
                    break

            except Exception as e:
                return f"执行出错: {e}"

        return f"达到最大执行步数（{self.MAX_STEPS_BUILD} 步），任务未完成。"

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
                cmd_input = action_input
                if isinstance(action_input, str) and action_input.startswith("{"):
                    try:
                        data = json.loads(action_input)
                        cmd_input = data.get("command", action_input)
                    except: pass
                return self._exec_bash({"command": cmd_input})
            elif action_type == "wiki_search":
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
                # 自动兼容：将 && 替换为 ; 以适配 PowerShell
                cmd = cmd.replace(" && ", "; ")
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
        """精确替换文件内容（带空白容错）"""
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

            # 精确匹配
            if old_text in content:
                count = content.count(old_text)
                if count > 1:
                    new_content = content.replace(old_text, new_text, 1)
                    p.write_text(new_content, encoding="utf-8")
                    return f"OK: 已编辑 {file_path}（找到 {count} 处，替换了第一处）"
                new_content = content.replace(old_text, new_text, 1)
                p.write_text(new_content, encoding="utf-8")
                return f"OK: 已编辑 {file_path}（替换了 {len(old_text)} → {len(new_text)} 字符）"

            # 容错：去除行尾空白后再匹配
            def normalize(s):
                return "\n".join(line.rstrip() for line in s.splitlines())

            norm_content = normalize(content)
            norm_old = normalize(old_text)
            if norm_old in norm_content:
                # 找到归一化匹配的位置，在原文中做逐行替换
                lines = content.splitlines(keepends=True)
                old_lines = old_text.splitlines()
                for i in range(len(lines)):
                    if lines[i].rstrip() == old_lines[0].rstrip():
                        match = True
                        for j in range(len(old_lines)):
                            if i + j >= len(lines) or lines[i+j].rstrip() != old_lines[j].rstrip():
                                match = False
                                break
                        if match:
                            new_lines = new_text.splitlines(keepends=True)
                            if new_text and not new_text.endswith("\n"):
                                new_lines[-1] = new_lines[-1] if new_lines[-1].endswith("\n") else new_lines[-1] + "\n"
                            result_lines = lines[:i] + new_lines + lines[i+len(old_lines):]
                            p.write_text("".join(result_lines), encoding="utf-8")
                            return f"OK: 已编辑 {file_path}（空白容错匹配成功）"

            # 匹配失败：返回文件片段帮助 Agent 修正
            preview = content[:1500] if len(content) < 3000 else content[:800] + "\n...\n" + content[-700:]
            return f"Error: 未找到匹配文本。文件实际内容预览:\n{preview}"
        except Exception as e:
            return f"Error: {e}"

    def _exec_view(self, params: dict) -> str:
        """查看文件内容（目录自动转为 ls）"""
        file_path = params.get("file_path") or params.get("raw", "")
        offset = params.get("offset", 0)
        limit = params.get("limit", 100)
        if not file_path:
            return "Error: 未提供 file_path"

        try:
            p = Path(self.cwd) / file_path
            if not p.exists():
                return f"Error: 文件不存在 {file_path}"

            # 如果是目录，自动转为 ls 操作
            if p.is_dir():
                return self._exec_ls({"path": file_path})

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
        """
        增强型 JSON 解析器 - 多层容错策略
        
        LLM 生成 write 动作时经常在 content 字段中嵌入大段代码，
        导致引号/换行破坏 JSON 格式。本方法通过多层策略依次尝试恢复。
        """
        if not text or not text.strip():
            return None

    def _parse_json_robustly(self, text: str) -> dict | None:
        """极度强健的 JSON 提取器，应对各种 LLM 幻觉和截断"""
        if not text or not text.strip(): return None
        
        # 尝试1: 标准代码块提取
        json_match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
        if json_match:
            try: return json.loads(json_match.group(1))
            except: text = json_match.group(1) # 回退到提取后的文本继续解析

        # 尝试2: 寻找第一个 '{' 并进行智能括号匹配
        start = text.find('{')
        if start != -1:
            depth = 0; in_str = False; escape = False; brace_end = -1
            for i in range(start, len(text)):
                c = text[i]
                if escape: escape = False; continue
                if c == '\\': escape = True; continue
                if c == '"' and not escape: in_str = not in_str; continue
                if not in_str:
                    if c == '{': depth += 1
                    elif c == '}':
                        depth -= 1
                        if depth == 0: brace_end = i; break
            
            if brace_end != -1:
                raw_json = text[start:brace_end+1]
                try: return json.loads(raw_json)
                except json.JSONDecodeError:
                    # 尝试修复内部换行和尾部逗号
                    cleaned = re.sub(r',\s*\}', '}', raw_json)
                    cleaned = re.sub(r'(":\s*)"(.*?)"(\s*[,}])', 
                                   lambda m: f'{m.group(1)}"{m.group(2).replace("\n", "\\n")}"{m.group(3)}', 
                                   cleaned, flags=re.DOTALL)
                    try: return json.loads(cleaned)
                    except: pass

        # 尝试3: 启发式正则提取
        res = {}
        for key in ["action", "thought", "self_criticism"]:
            m = re.search(fr'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
            if m: res[key] = m.group(1).replace('\\n', '\n')
        
        # 递归处理 input
        inp_start = text.find('"input"')
        if inp_start != -1:
            colon_pos = text.find(':', inp_start)
            if colon_pos != -1:
                rem = text[colon_pos+1:].strip()
                if rem.startswith('{'):
                    d = 0; s = False; e = False; b_end = -1
                    for i in range(len(rem)):
                        c = rem[i]
                        if e: e = False; continue
                        if c == '\\': e = True; continue
                        if c == '"' and not e: s = not s; continue
                        if not s:
                            if c == '{': d += 1
                            elif c == '}':
                                d -= 1
                                if d == 0: b_end = i; break
                    if b_end != -1:
                        try: res["input"] = json.loads(rem[:b_end+1])
                        except: res["input"] = rem[:b_end+1]
                elif rem.startswith('"'):
                    s_m = re.search(r'"((?:[^"\\]|\\.)*)"', rem)
                    if s_m: res["input"] = s_m.group(1).replace('\\n', '\n')
        
        # 提取任务列表
        tasks_m = re.search(r'"pending_tasks"\s*:\s*\[(.*?)\]', text, re.DOTALL)
        if tasks_m:
            try:
                res["pending_tasks"] = json.loads(f"[{tasks_m.group(1)}]")
            except json.JSONDecodeError:
                # 简单提取引号内的字符串
                res["pending_tasks"] = re.findall(r'"([^"]+)"', tasks_m.group(1))
        
        return res if "action" in res else None

