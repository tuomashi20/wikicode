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

class WikiCoderEngine:
    """[WikiCoder 工业级内核] 深度复刻 OpenCode 架构，极致性能优化版"""
    
    def __init__(self, config):
        self.config = config
        self.steps: List[BuildStep] = []
        self._action_hashes: Set[int] = set()
        
        # 核心器官延迟加载容器
        self.interpreter = None
        self.llm = None
        self.expert = None
        self.toolbox = None
        self._cached_orient = None

    def _ensure_infrastructure(self):
        """确保所有核心组件已初始化，仅在首次运行时加载"""
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

    def run(self, query, history: List[tuple[str, str]] = None, on_step=None, on_log=None, on_token=None, should_stop=None, mode="plan", **kwargs):
        """[核心调度] 支持断点恢复的高性能循环"""
        self._ensure_infrastructure()
        
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
                    # 查找上一步选项中哪个匹配这个快捷键
                    # 注意：这里简化处理，直接取列表第一个作为标准回复
                    final_reply = vals[0]
                    break
            
            if on_log: on_log(f"\n> 📝 **收到针对问题 '{last_question}' 的指令:** {final_reply}")
            self.steps[-1].observation = f"针对问题 '{last_question}'，用户明确答复: {final_reply}"
            is_resume = True
            scratchpad = "\n".join([f"步骤 {i+1}:\n- 思考: {s.thought[:100]}\n- 动作: {s.action_type}\n- 结果: {s.observation[:1000]}" for i, s in enumerate(self.steps)])
        else:
            self.steps = []
            scratchpad = ""
            self._cached_orient = None 

        retry_count = 0
        action_history = [] # 记录执行过的动作，用于死循环检测
        
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

            if mode == "build":
                tools_str = self.toolbox.get_tool_descriptions()
            else:
                # Plan 模式：封印 ask_user，仅允许查询与读取类工具
                tools_str = self.toolbox.get_tool_descriptions(filter_list=["wiki_search", "read_file", "read_excel"])

            # 4. 记忆精简与意图隔离 (对齐 OpenCode 架构)
            def condense_history(hist):
                if not hist: return "无"
                # 仅保留最近 3 轮对话，防止历史任务干扰新任务
                recent = hist[-6:] if len(hist) > 6 else hist
                return "\n".join([f"User: {h[0]}\nAssistant: {h[1][:300]}..." for h in recent])

            context = condense_history(history)
            
            # 如果是新任务的第一步，显式打入隔离补丁
            task_focus = ""
            if is_first and history:
                task_focus = "\n⚠️ [意图隔离] 上一个任务已结束。请忽略历史执行细节，完全聚焦于用户当前的最新指令，重新评估环境。特别是注意内存变量是否与当前任务相关。"

            prompt = get_prompt_assembly(
                mode=mode, tools_str=tools_str, orientation=self._cached_orient if is_first else "参考前期业务背景。",
                context=f"{context}{task_focus}", mem_state=mem_state, query=query, scratchpad=scratchpad if scratchpad else "暂无记录"
            )

            if on_log: on_log(f"\n> 🧠 **WikiCoder 正在思考方案...** ")
            
            resp = ""
            thought_printed_len = 0
            for token in self.llm.generate_stream("WikiCoder", prompt):
                if should_stop and should_stop(): return "任务已终止。"
                resp += token
                
                # 鲁棒型 thought 抓取：只要在 thought 字段范围内，就实时输出
                if '"thought":' in resp:
                    # 找到 thought 内容的开始位置 (跳过 "thought": " 等字符)
                    content_start = resp.find('"thought":') + 10
                    while content_start < len(resp) and resp[content_start] in [' ', ':', '"', '\n']:
                        content_start += 1
                    
                    # 检查是否已经到了 plan 或 action 的地界
                    if any(x in resp[-20:] for x in ['"plan"', '"action"', '"options"']):
                        pass 
                    else:
                        # 增量输出新产生的内容
                        if len(resp) > content_start + thought_printed_len:
                            new_content = resp[content_start + thought_printed_len:]
                            # 清洗掉结尾可能的引号
                            clean_chunk = new_content.replace('"', '').replace('}', '')
                            if clean_chunk and on_log:
                                on_log(clean_chunk)
                                thought_printed_len += len(new_content)

            # 3. 解析决策
            decision = parse_react_response(resp)
            if not decision: 
                retry_count += 1
                if retry_count > 3:
                    if on_log: on_log("\n[!] 连续解析失败，请检查模型输出或尝试切换模式。")
                    break
                if on_log: on_log(f" (解析重试 {retry_count}/3)... "); continue
            
            # 重置重试计数
            retry_count = 0
            
            thought = decision.get("thought", "")
            
            # 暴力纠错版 plan 提取
            plan = decision.get("plan", [])
            if isinstance(plan, str):
                # 兼容处理：[任务A, 任务B] 字符串
                clean_plan = plan.strip("[] ").replace('"', '').replace("'", "")
                plan = [p.strip() for p in clean_plan.split(',') if p.strip()]
            elif not isinstance(plan, list):
                plan = [str(plan)]
            
            # 显式向 UI 报告任务清单 (部分 UI 框架监听此格式)
            if on_log and plan:
                for p_task in plan:
                    if p_task: on_log(f"\n[PLAN] {p_task}")

            action_data = decision.get("action", {})
            a_name = action_data.get("name")
            a_params = action_data.get("parameters", {})

            # 7. 终止检查
            if a_name == "summarize" or not a_name: break

            # 8. 智能提取涉及的文件 (用于侧边栏展示)
            files = []
            param_str = json.dumps(a_params)
            # 简单的启发式搜索：寻找路径格式的字符串
            import re
            found_paths = re.findall(r'[a-zA-Z]:\\[^"\'\s]+|/[^"\'\s]+', param_str)
            files = [p.split('\\')[-1].split('/')[-1] for p in found_paths if '.' in p]

            # 9. 构建完整执行步骤
            step = BuildStep(
                thought=thought, 
                plan=plan,
                tasks=plan,
                todo=plan,
                action_type=a_name, 
                action_input=json.dumps(a_params, ensure_ascii=False) if isinstance(a_params, dict) else str(a_params),
                observation="",
                files=list(set(files)) # 去重
            )

            # 10. 动作查重与死循环防御
            a_hash = hash(f"{a_name}:{step.action_input}")
            if a_hash in self._action_hashes:
                obs = f"[系统纠错] 你已经执行过完全相同的动作 ({a_name}) 且输入参数完全一致。严禁重复！请分析上一次的 Observation，调整策略或承认无法完成，禁止原地打转。"
                step.observation = obs
                self.steps.append(step)
                if on_log: on_log(f"\n[!] 检测到重复动作，已强制干预。")
                continue
            
            self._action_hashes.add(a_hash)
            
            # 11. 工具调用疲劳检测 (防止同一个工具反复横跳)
            tool_calls = [s.action_type for s in self.steps]
            if tool_calls.count(a_name) >= 3:
                # 注入强力的系统负面反馈
                scratchpad += f"\n⚠️ [系统警告] 你已经连续使用了 {a_name} 工具 3 次以上。如果你依然无法获得满意结果，说明搜索方向有误。请尝试: 1. 换个截然不同的关键词; 2. 检查附件内容; 3. 承认无法找到确切答案并结案。严禁继续执行无意义的相同操作！\n"

            # 12. 核心中断逻辑
            if a_name == "ask_user":
                obs, p_data = self.toolbox.execute(a_name, a_params, engine=self)
                question = p_data.get("question", "")
                hint = p_data.get("hint", "")
                
                # 使用醒目高亮格式提示用户
                if on_log: 
                    on_log(f"\n\n{'='*50}\n")
                    on_log(f"❓ **[交互申请]** {question}\n")
                    on_log(f"👉 **请输入回复:** [{hint}]\n")
                    on_log(f"{'='*50}\n")
                
                self.steps.append(step)
                # 重要：发送给 UI 渲染前清空 thought，避免与流式输出内容重复
                step.thought = "" 
                if on_step: on_step(step)
                return "__INTERRUPTED_WAITING_USER__"

            if on_log: on_log(f"\n> ⚙️ **正在执行:** {a_name}...")
            obs, _ = self.toolbox.execute(a_name, a_params, engine=self)
            step.observation = obs
            self.steps.append(step)
            
            # 重要：发送给 UI 渲染前清空 thought，避免与流式输出内容重复
            step.thought = "" 
            if on_step: on_step(step)

            scratchpad += f"\n步骤 {len(self.steps)}:\n- 思考: {thought[:100]}\n- 动作: {a_name}\n- 结果: {obs[:2000]}\n"

        clean_obs = [f"Step {i+1} ({s.action_type}): {s.observation[:1000]}" for i, s in enumerate(self.steps)]
        return self.expert.synthesize(query, clean_obs, mode=mode, on_token=on_token, **kwargs)

BuildAgent = WikiCoderEngine