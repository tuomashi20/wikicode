import json
import time
from typing import List, Optional, Callable, Any, Set
from dataclasses import dataclass
from pathlib import Path

# 核心解耦引用
from src.utils.parser import parse_react_response
from src.core.prompts import get_prompt_assembly

@dataclass
class BuildStep:
    thought: str
    plan: List[str]      # 基础计划
    tasks: List[str]     # UI 冗余：任务清单
    todo: List[str]      # UI 冗余：待办项
    action_type: str
    action_input: str
    observation: str
    files: List[str] = None

class BuildAgent:
    """[WikiCoder 工业级内核] 深度复刻 OpenCode 架构，极致性能优化版"""
    
    def __init__(self, config):
        self.config = config
        self.steps: List[BuildStep] = []
        self._action_hashes: Set[int] = set()
        
        # 核心器件延迟加载容器
        self.interpreter = None
        self.llm = None
        self.expert = None
        self.toolbox = None
        self._cached_orient = None

    def _ensure_infrastructure(self):
        """确保所有核心组件已初始化，并强制对齐路径认知"""
        from src.utils.config import ensure_workspace
        ensure_workspace(self.config)

        if not self.interpreter:
            from src.skills.interpreter import PythonInterpreter
            self.interpreter = PythonInterpreter()
        if not self.llm:
            from src.core.llm_client import LLMClient
            self.llm = LLMClient(self.config.llm)
        if not self.expert:
            from src.skills.wiki_expert import WikiExpert
            self.expert = WikiExpert(self.config, self.llm)
        if not self.toolbox:
            from src.core.toolbox import toolbox
            self.toolbox = toolbox

    def sync(self, on_status=None) -> bool:
        """透传同步指令给专家"""
        self._ensure_infrastructure()
        return self.expert.sync(on_status=on_status)

    def run(self, query, history: List[tuple[str, str]] = None, on_step=None, on_log=None, on_token=None, should_stop=None, mode="chat", **kwargs):
        """[核心调度] 支持断点恢复的高性能循环 (V2.0 Chat/Agent 对齐版)"""
        self._ensure_infrastructure()
        
        # 兼容性映射：如果传入旧的 plan/build，自动对齐
        if mode == "plan": mode = "chat"
        elif mode == "build": mode = "agent"
        
        # --- 断点恢复检测 ---
        is_resume = False
        if self.steps and self.steps[-1].action_type == "ask_user" and not self.steps[-1].observation:
            # 获取之前的问题上下文，防止 AI 复读
            last_action_input = json.loads(self.steps[-1].action_input)
            last_question = last_action_input.get("question", "未知问题")
            
            # 快捷键映射逻辑 (y/n/a)
            mapping = {"y": ["是", "继续", "Yes"], "n": ["否", "终止", "No"], "a": ["全部", "All"]}
            user_reply = query.strip().lower()
            
            # 尝试根据快捷键还原完整回复
            final_reply = query
            for k, vals in mapping.items():
                if user_reply == k:
                    final_reply = vals[0]
                    break
            
            if on_log: on_log(f"\n> 📝 **收到针对问题 '{last_question}' 的指令:** {final_reply}")
            self.steps[-1].observation = f"针对问题 '{last_question}'，用户明确答复: {final_reply}"
            # 将当前步骤加入 scratchpad (V4.0 结构化增强版)
            scratchpad = ""
            for i, step in enumerate(self.steps):
                scratchpad += f"\n### 步骤 {i+1}:\n"
                scratchpad += f"【THOUGHT】: {step.thought}\n"
                scratchpad += f"【ACTION】: {step.action_type}({step.action_input})\n"
                scratchpad += f"【OBSERVATION】: {step.observation}\n"
                scratchpad += f"----------------------------------------\n"
            is_resume = True
        else:
            self.steps = []
            scratchpad = ""
            self._cached_orient = None 

        # --- [V3.0 内置化升级] 长期记忆唤醒逻辑 ---
        gbrain_tools_desc = ""
        if mode == "agent":
            from src.core.memory.manager import memory_manager
            if on_log: on_log("\n> 🧠 **正在同步内置长程记忆与语义引擎...**")
            
            # 获取内置记忆工具描述
            gbrain_tools_desc = "\n### [内置长期记忆工具 (Agent 专属)]\n"
            gbrain_tools_desc += self.toolbox.get_tool_descriptions(filter_list=["gbrain_put_page", "gbrain_get_page", "gbrain_search", "gbrain_list_pages"])
            
            # --- [V3.0] 长期记忆自动唤醒 ---
            try:
                profile = memory_manager.get_page("personal_profile")
                if "⚠️" not in profile:
                    if on_log: on_log("\n> 👤 **已识别用户长期画像，正在加载个性化偏好...**")
                    self._cached_orient = f"【用户长期画像】\n{profile}\n\n" + (self._cached_orient or "")
                else:
                    self._cached_orient = "【记忆库状态】当前尚未检测到用户长期画像。如果用户询问身份，请尝试调用 gbrain_get_page(slug='personal_profile')。\n\n" + (self._cached_orient or "")
                
                consensus = memory_manager.get_page("project_consensus")
                if "⚠️" not in consensus:
                    if on_log: on_log("\n> 🤝 **已加载项目历史共识...**")
                    self._cached_orient = f"【项目长期共识】\n{consensus}\n\n" + (self._cached_orient or "")
            except Exception as e:
                logger.error(f"Failed to load local memory: {e}")

        retry_count = 0
        final_params = {} # [初始化] 用于捕获 summarize 动作的参数
        
        while len(self.steps) < 12:
            if is_resume:
                is_resume = False
                continue

            # 1. 获取全景导航
            if not self._cached_orient:
                self._cached_orient = self.expert.orient()
            
            # 2. 状态组装
            mem_vars = list(self.interpreter.list_variables().keys()) if self.interpreter else []
            mem_state = f"内存变量: {mem_vars if mem_vars else '空'}"
            is_first = (len(self.steps) == 0)

            # --- [V2.0] 工具隔离逻辑 ---
            if mode == "chat":
                # Chat 模式：仅保留检索、阅读、询问工具
                allowed = ["wiki_search", "read_file", "read_excel", "ask_user", "summarize"]
                tools_str = self.toolbox.get_tool_descriptions(filter_list=allowed)
            else:
                # Agent 模式：全量工具 + gbrain 工具
                tools_str = self.toolbox.get_tool_descriptions() + gbrain_tools_desc

            # 4. 记忆精简与意图隔离
            def condense_history(hist):
                if not hist: return "无"
                recent = hist[-6:] if len(hist) > 6 else hist
                return "\n".join([f"User: {h[0]}\nAssistant: {h[1][:300]}..." for h in recent])

            context = condense_history(history)
            task_focus = "\n⚠️ [意图隔离] 请聚焦于当前指令。" if is_first and history else ""
            # 动态注入权限自觉引导
            mode_notice = "\n🛡️ [模式自觉] 当前处于 Chat 只读模式。你**没有**写文件或跑命令的工具，严禁尝试任何修改操作！" if mode == "chat" else ""

            prompt = get_prompt_assembly(
                mode=mode, tools_str=tools_str, orientation=self._cached_orient if is_first else "参考前期业务背景。",
                context=f"{context}{task_focus}{mode_notice}", mem_state=mem_state, query=query, scratchpad=scratchpad if scratchpad else "暂无记录"
            )

            if on_log: on_log(f"\n> 🧠 **WikiCoder [{mode.upper()}] 正在思考方案...** ")

            # 3. 流式生成与思考流净化
            resp = ""
            thought_printed_len = 0
            is_collecting_json = False 
            
            for token in self.llm.generate_stream("WikiCoder", prompt):
                if should_stop and should_stop(): return "任务已终止。"
                resp += token
                
                # 智能拦截：一旦进入结构化数据区 (Plan/Action/Options)，停止流式打印
                if not is_collecting_json:
                    # 兼容性匹配：支持带引号和不带引号的 key
                    indicators = ['"plan"', '"action"', '"options"', 'plan:', 'action:', 'options:']
                    if any(x in resp.lower() for x in indicators):
                        is_collecting_json = True
                
                # 仅在非 JSON 区打印 thought 内容
                if not is_collecting_json and '"thought":' in resp:
                    content_start = resp.find('"thought":') + 10
                    while content_start < len(resp) and resp[content_start] in [' ', ':', '"', '\n']:
                        content_start += 1
                    
                    if len(resp) > content_start + thought_printed_len:
                        new_chunk = resp[content_start + thought_printed_len:]
                        clean_chunk = new_chunk.replace('"', '').replace('}', '').replace(',', '')
                        if clean_chunk and on_log:
                            on_log(clean_chunk)
                            thought_printed_len += len(new_chunk)

            # 4. 解析决策
            decision = parse_react_response(resp)
            if not decision: 
                retry_count += 1
                if retry_count > 3:
                    if on_log: on_log("\n[!] 决策解析失败。")
                    break
                continue
            
            retry_count = 0
            thought = decision.get("thought", "")
            plan = decision.get("plan", [])
            action_data = decision.get("action", {})
            a_name = action_data.get("name")
            a_params = action_data.get("parameters", {})

            if a_name == "summarize" or not a_name: 
                # [核心修复] 将 summarize 携带的参数 (answer/code) 捕获并透传给合成器
                final_params = a_params if isinstance(a_params, dict) else {}
                break

            # 5. 构建完整执行步骤
            step = BuildStep(
                thought=thought, plan=plan, tasks=plan, todo=plan,
                action_type=a_name, 
                action_input=json.dumps(a_params, ensure_ascii=False) if isinstance(a_params, dict) else str(a_params),
                observation=""
            )

            # --- [V3.5 动作解压展示逻辑] ---
            if on_log:
                prefix = self._get_action_desc(a_name)
                # 特殊处理 write_file: 直接展示代码内容
                if a_name == "write_file" and isinstance(a_params, dict):
                    target_file = a_params.get("path") or a_params.get("file_path") or "未知文件"
                    code_content = a_params.get("content") or ""
                    on_log(f"\n{prefix}\n> 📄 目标文件: `{target_file}`\n```python\n{code_content}\n```")
                else:
                    # 其他动作展示格式化后的参数
                    try:
                        pretty_params = json.dumps(a_params, indent=2, ensure_ascii=False)
                    except:
                        pretty_params = str(a_params)
                    on_log(f"\n{prefix}\n```json\n{pretty_params}\n```")

            # 10. 动作查重与死循环熔断 (V4.0 智能指引版)
            a_hash = hash(f"{a_name}:{step.action_input}")
            if a_hash in self._action_hashes:
                repeat_count = getattr(self, "_repeat_count", 0) + 1
                self._repeat_count = repeat_count
                
                if repeat_count >= 3:
                    obs = f"[致命错误] 动作 ({a_name}) 连续重复 3 次。检测到该工具无法提供所需信息。"
                    step.observation = obs
                    self.steps.append(step)
                    if on_log: on_log(f"\n[❌] **熔断：检测到严重死循环，任务已强行中止。**")
                    break 
                
                # 注入强力的系统纠错指引，迫使模型改变策略
                error_guide = f"\n⚠️ [系统干预] 你已经执行过完全相同的动作 ({a_name}) 且未获得有效结果。请停止复读！\n"
                error_guide += "👉 **建议策略**：1. 如果这是一个通用常识问题，请直接根据你的知识库回答；2. 如果本地工具无法处理，请如实告知用户；3. 尝试更换搜索关键词或使用其他工具。\n"
                scratchpad += error_guide
                
                obs = f"⚠️ [严重错误] 动作 ({a_name}) 重复。如果您上一侧动作已成功，请立即总结；严禁再次尝试相同参数，否则任务将因死循环被强制终止！"
                step.observation = obs
                self.steps.append(step)
                if on_log: on_log(f"\n[!] **检测到重复动作，系统已注入“换脑”指引...**")
                continue
            
            self._action_hashes.add(a_hash)
            self._repeat_count = 0
            
            # 11. 执行鉴权与逻辑分流 (V5.0 工业级安全加固版)
            if mode == "chat":
                # 定义 Chat 模式下的绝对白名单
                allowed_chat_tools = ["wiki_search", "read_file", "read_excel", "ask_user", "summarize", "gbrain_search"]
                if a_name not in allowed_chat_tools:
                    # 逻辑对齐：明确告知 Agent 可以输出代码块，只是不能通过工具执行
                    obs = (
                        f"提示：当前处于只读的 Chat 模式，工具 ({a_name}) 暂时锁定。 "
                        "你可以（且应该）直接在对话中以 Markdown 代码块的形式输出完整的 Python 脚本供用户参考，"
                        "但请务必告知用户：如需自动执行该脚本或保存到本地，请点击底部的模式切换按钮进入 Agent 模式。"
                    )
                    step.observation = obs
                    self.steps.append(step)
                    if on_log: on_log(f"\n[i] 模式引导：已告知 Agent 以代码块形式提供方案 (原计划: {a_name})")
                    continue

            # 处理交互中断
            if a_name == "ask_user":
                obs, p_data = self.toolbox.execute(a_name, a_params, engine=self)
                self.steps.append(step)
                return "__INTERRUPTED_WAITING_USER__"

            # --- 工具执行分流 ---
            try:
                # 统一走本地工具箱执行
                obs, _ = self.toolbox.execute(a_name, a_params, engine=self)
            except Exception as e:
                obs = f"[执行异常] 工具 ({a_name}) 运行失败: {str(e)}"

            step.observation = obs
            self.steps.append(step)
            if on_step: on_step(step)

            # 12. 结果反馈统一处理 (保留换行符，确保表格和代码块正常渲染)
            if on_log and obs:
                # 对于过长的输出进行截断，但保留其结构
                display_obs = obs[:2000] + ("\n\n...(内容过长已截断)" if len(obs) > 2000 else "")
                
                # 如果是表格或代码，直接原样输出；否则使用引用块包裹
                if ("|" in display_obs and "---" in display_obs) or "```" in display_obs:
                    on_log(f"\n🛠️ **执行结果 ({a_name}):**\n\n{display_obs}")
                else:
                    on_log(f"\n🛠️ **执行结果 ({a_name}):**\n> {display_obs.replace(chr(10), chr(10)+'> ')}")

            # 将当前步骤加入 scratchpad (V4.0 结构化增强版)
            scratchpad = ""
            for i, s in enumerate(self.steps):
                scratchpad += f"\n### 步骤 {i+1}:\n"
                scratchpad += f"【THOUGHT】: {s.thought}\n"
                scratchpad += f"【ACTION】: {s.action_type}({s.action_input})\n"
                scratchpad += f"【OBSERVATION】: {s.observation}\n"
                scratchpad += f"----------------------------------------\n"

        clean_obs = [f"步骤 {i+1} ({s.action_type}): {s.observation[:5000]}" for i, s in enumerate(self.steps)]
        # [V2.6 强化] 注入 Agent 最后一轮的高光思维，并将 summarize 参数注入 kwargs
        last_thought = self.steps[-1].thought if self.steps else ""
        kwargs.update(final_params) 
        return self.expert.synthesize(query, clean_obs, mode=mode, on_token=on_token, context=last_thought, **kwargs)

    def _get_action_desc(self, name: str) -> str:
        """根据动作类型生成人性化的视觉描述前缀"""
        mapping = {
            "search_web": "🌐 **正在接入互联网检索信息**",
            "search_chunks": "🔍 **正在深度扫描项目规约库**",
            "read_file": "📖 **正在调阅相关文件细节**",
            "read_excel": "📊 **正在对 Excel 数据进行透视分析**",
            "write_file": "📝 **正在将构思同步到本地文件**",
            "gbrain_get_page": "🧠 **正在从语义库提取长期记忆**",
            "gbrain_search": "🧠 **正在利用语义引擎进行关联搜索**",
            "python": "🐍 **正在启动 Python 运行环境**",
            "run_command": "🐚 **正在调用系统终端执行指令**",
            "python_repl": "⚡ **正在通过 REPL 实时执行验证**"
        }
        return mapping.get(name, f"🛠️ **正在执行操作 ({name})**")
