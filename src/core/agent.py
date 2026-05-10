from __future__ import annotations
import re
import json
import time
import datetime
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Any, List, Optional
from pathlib import Path

from src.core.llm_client import LLMClient
from src.core.query_rewriter import QueryRewrite, load_business_terms
from src.skills.wiki_tools import wiki_read_chunk, wiki_search_v2
from src.utils.config import AppConfig
from src.utils.logger import get_file_logger
from src.core.graph_agent import GraphAgent
# from src.core.mcp_client import GBrainMCPClient

ResponseMode = Literal["answer", "patch", "explain"]

@dataclass
class AgentResponse:
    thought: str
    actions: list[str]
    output: str

@dataclass
class WikiStep:
    thought: str
    action_type: str
    action_input: str
    tasks: list[str] = field(default_factory=list)

class WikiFirstAgent:
    """Wiki-first ReAct: search wiki first, fallback to general model when no reliable hit."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.llm = LLMClient(config.llm)
        self.logger = get_file_logger("agent", "agent.log")
        self.core_keywords = load_business_terms(config.wiki_strategy.business_terms_path)
        try:
            self.graph_agent = GraphAgent()
        except:
            self.graph_agent = None
            
        # 已切换为内置 memory_manager，不再使用外部 MCP
        self.gbrain = None

    def run(
        self,
        user_input: str,
        target_file: str = "",
        code_context: str = "",
        response_mode: ResponseMode = "answer",
        force_wiki: bool = False,
        history: list[tuple[str, str]] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_step: Callable[[Any], bool] | None = None,
    ) -> AgentResponse:
        actions: list[str] = []
        query = user_input.strip()

        def _act(msg: str) -> None:
            actions.append(msg)
            if on_status: on_status(msg)
            if on_step:
                act_type = msg.split('(')[0] if '(' in msg else "info"
                on_step(WikiStep(thought="", action_type=act_type, action_input=msg))

        if not query and not force_wiki:
            return AgentResponse(thought="empty-input", actions=actions, output="Please enter a question.")

        if on_status: on_status("Plan mode: analyzing requirements")
        # [DEBUG] 强制版本印记，确保运行的是最新代码
        self.logger.info(f"--- WikiCoder Agent V4.0 (Web-Enabled) RUNNING --- Query: {query}")
        
        # 强制联网判定 (前置化，确保最高优先级)
        web_context = ""
        force_web_keywords = ["搜", "查", "联网", "互联网", "金价", "今日", "价格", "天气", "新闻"]
        if any(k in query for k in force_web_keywords):
            if on_status: on_status("检测到实时情报需求，正在跨越防火墙搜索云端数据...")
            if on_token: on_token("\n[bold yellow]󰖟 正在启动云端情报引擎...[/bold yellow]\n")
            from src.skills.web_browser import web_search, web_fetch
            try:
                search_res = web_search(query)
                if "未找到" not in search_res:
                    urls = re.findall(r'URL: (https?://\S+)', search_res)
                    if urls:
                        if on_status: on_status(f"搜索成功，正在读取核心情报: {urls[0]}")
                        detail = web_fetch(urls[0])
                        web_context = f"\n【互联网实时情报】:\n{search_res}\n\n【深度摘要】:\n{detail}"
                    else:
                        web_context = f"\n【互联网实时情报】:\n{search_res}"
            except Exception as e:
                self.logger.error(f"Force web search failed: {e}")

        rw_text = self.llm.generate(
            system_prompt="You are a query expert. Extract keywords as a comma-separated list.",
            user_prompt=f"Task: {query}"
        )
        keywords = [k.strip() for k in rw_text.split(',') if k.strip()]
        _act(f"query_rewrite(keywords={keywords})")

        # 2. Expert Model Logic
        graph_context = ""
        if on_status: on_status("正在进行实时业务逻辑审计...")
        
        try:
            if not hasattr(self, "_cached_graph"):
                graph_json = Path("d:/project/wikicode/graphify_out/.graphify_pure_merged.json")
                if graph_json.exists():
                    self._cached_graph = json.loads(graph_json.read_text(encoding='utf-8'))
                    self._cached_atoms = [n for n in self._cached_graph.get('nodes', []) if n.get('type') == 'semantic_atom']
                else:
                    self._cached_atoms = []
            
            if self._cached_atoms:
                match_atoms = []
                for atom in self._cached_atoms:
                    label = atom.get('label', '').lower()
                    if any(kw in label for kw in keywords if len(kw) > 1):
                        match_atoms.append(atom)
                
                if match_atoms:
                    logic_lines = [f"【逻辑官审计结论】: 发现 {len(match_atoms)} 条跨文档业务规则约束。"]
                    for a in match_atoms[:5]:
                        props = a.get('properties', {})
                        prop_str = f" (核心参数: {json.dumps(props, ensure_ascii=False)})" if props else ""
                        logic_lines.append(f"📌 原子规则: {a['label']}{prop_str}")
                    graph_context = "\n".join(logic_lines)
                else:
                    graph_context = "【逻辑官审计结论】: 未发现硬性业务规则原子。"
            else:
                graph_context = "【逻辑官审计结论】: 语义图谱底座缺失。"
        except Exception as e:
            graph_context = f"逻辑审计异常: {str(e)}"

        if on_step:
            on_step(WikiStep(
                thought="逻辑官 (@graphagent) 正在扫描内存语义图谱...",
                action_type="graph_audit",
                action_input=graph_context
            ))

        # 3. Wiki Search
        search_limit = self.config.wiki_strategy.agent_search_limit or 8
        results, _ = wiki_search_v2(query, limit=search_limit)
        _act(f"wiki_search_v2(query='{query}') -> {len(results)}")

        # 4. gbrain Memory Search
        gbrain_memory = ""
        if self.gbrain:
            if on_status: on_status("正在检索 gbrain 个人经历记忆...")
            try:
                # 策略 A: 强制/主动召回用户画像 (作为核心上下文)
                # 针对 Windows 启动慢，大幅增加重试次数
                for attempt in range(20):
                    profile_res = self.gbrain.call_tool("get_page", {"slug": "user-profile"})
                    # 使用英文锚点判断，免疫乱码
                    if "STILL_WAKING_UP" in profile_res:
                        if on_status: on_status(f"记忆官正在深度唤醒中... ({attempt+1}/20)")
                        time.sleep(1.5)
                        continue
                    if profile_res and "Page not found" not in profile_res and "MCP Tool Error" not in profile_res:
                        # 清洗 JSON 数据，提取纯文本以降低大模型理解成本
                        try:
                            data = json.loads(profile_res)
                            if isinstance(data, list) and len(data) > 0:
                                text = data[0].get("chunk_text", "")
                                gbrain_memory += f"\n【系统强制确认的用户身份】: {text}\n"
                            else:
                                gbrain_memory += f"\n【系统强制确认的用户身份】: {profile_res}\n"
                        except:
                            gbrain_memory += f"\n【系统强制确认的用户身份】: {profile_res}\n"
                    break
                
                # 策略 B: 通用语义检索 (针对具体问题)
                for _ in range(2):
                    gbrain_res = self.gbrain.call_tool("query", {"query": query})
                    if "STILL_WAKING_UP" in gbrain_res:
                        time.sleep(1)
                        continue
                    if gbrain_res and "No results found" not in gbrain_res and "MCP Tool Error" not in gbrain_res:
                        gbrain_memory += f"\n【长效记忆记录】:\n{gbrain_res}"
                    break
                
                if gbrain_memory:
                    _act("gbrain_memory_retrieved")
                    if on_step:
                        on_step(WikiStep(
                            thought="记忆官 (@gbrain) 正在唤醒长效个人经历...",
                            action_type="gbrain_audit",
                            action_input="已加载并清洗用户信息。"
                        ))
            except Exception as e:
                self.logger.error(f"gbrain memory integration failed: {e}")

        if self.gbrain and ("记住" in query or "保存" in query):
            try:
                timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                slug = f"personal/memory_{int(time.time())}"
                content = f"---\ntype: memory\ntitle: User Request at {timestamp}\n---\n\n{query}\n\n---\n- {timestamp}: User requested to remember this."
                res = self.gbrain.call_tool("put_page", {"slug": slug, "content": content})
                self.logger.info(f"gbrain store result for {slug}: {res}")
                _act(f"gbrain_memory_stored(slug={slug})")
                if on_step:
                    on_step(WikiStep(
                        thought="正在将此段经历存入长效记忆库...",
                        action_type="gbrain_store",
                        action_input=f"已存入 gbrain: {slug}"
                    ))
            except Exception as e:
                self.logger.error(f"gbrain store failed: {e}")

        chunks = self._filter_reliable_results(results)

        if chunks:
            _act(f"wiki_relevance_filter: keep={len(chunks)}/{len(results)}")
            context_blocks = []
            
            # 权重调整：将互联网实时数据置于首位，确保模型第一眼看到
            if web_context:
                context_blocks.append(f"【!!! 极高优先级：当前互联网实时情报 !!!】\n{web_context}")
            
            for c in chunks:
                body = wiki_read_chunk(c['chunk_id'])
                context_blocks.append(f"Title: {c['title']}\nSource: {c['parent_file']}\nContent: {body}")
            
            if graph_context: context_blocks.append(f"\n[业务图谱约束]\n{graph_context}")
            if gbrain_memory: context_blocks.append(f"\n[个人经历记忆]\n{gbrain_memory}")

            output = self._wiki_grounded_chat(query, chunks, context_blocks, history, on_token, on_status)
            return AgentResponse(thought="wiki-grounded", actions=actions, output=output)
        
        _act("wiki_relevance_filter: drop_all_low_relevance")
        fallback_ctx = []
        if web_context: fallback_ctx.append(f"【!!! 极高优先级：当前互联网实时情报 !!!】\n{web_context}")
        if graph_context: fallback_ctx.append(graph_context)
        if gbrain_memory: fallback_ctx.append(gbrain_memory)
        
        output = self._general_chat(query + ("\n\n" + "\n\n".join(fallback_ctx) if fallback_ctx else ""), history, on_token, on_status)
        return AgentResponse(thought="general-fallback", actions=actions, output=output)

    def _filter_reliable_results(self, results) -> list[dict]:
        reliable = []
        for r in results:
            pf = str(r.get("parent_file", "")).lower()
            if any(k in pf for k in ["/knowledge/", "knowledge\\", "chat_archive", "draft_"]): continue
            reliable.append(r)
        return reliable[:10]

    def _wiki_grounded_chat(self, query, chunks, context_blocks, history, on_token, on_status):
        system_prompt = (
            "你是 WikiCoder 专家模型。当前正在进行【专家会诊】模式。\n"
            "你会收到三部分核心输入：\n"
            "1. 【Wiki 聚合事实】：这是由 WikiCoder 编译器从全库中提取并聚合而成的百科页面，具有最高优先级和权威性。\n"
            "2. 【逻辑官 (@graphagent) 见解】：来自图谱的跨文档逻辑关联。\n"
            "3. 【个人经历 (@gbrain) 记忆】：来自过往的沟通、任务和个人时间线。\n\n"
            "你的任务：\n"
            "- 优先通过【Wiki 聚合事实】来回答问题。如果搜索结果中包含【Wiki 聚合页】，请务必以其为核心准则。\n"
            "- 如果聚合页信息不全，再结合原始片段进行补充。\n"
            "- 严禁捏造，标注引用 [1]/[2]。"
        )
        user_prompt = f"Question: {query}\n\nContext:\n" + "\n".join(context_blocks)
        
        full_text = ""
        for tok in self.llm.generate_stream(system_prompt, user_prompt):
            full_text += tok
            if on_token: on_token(tok)
        return full_text + "\n\nReferences:\n" + "\n".join([f"[{i+1}] {c['title']} ({c['parent_file']})" for i, c in enumerate(chunks[:3])])

    def _general_chat(self, query, history, on_token, on_status):
        system_prompt = (
            "你是 WikiCoder 专家模型。\n"
            "你会收到由系统通过互联网、逻辑图谱和个人记忆汇总而成的上下文信息。\n"
            "你的核心任务：\n"
            "1. 优先使用上下文中的【互联网实时情报】来回答实时性问题（如金价、新闻等）。\n"
            "2. 结合【逻辑官审计结论】确保回答符合业务逻辑约束。\n"
            "3. 如果记忆中包含了用户的身份或背景，请在回答中体现。\n"
            "4. 如果没有任何参考信息，请基于你的通用知识回答。"
        )
        # 深度诊断日志
        self.logger.info(f"--- DEBUG LLM CALL ---\nSYSTEM: {system_prompt}\nUSER: {query}\n----------------------")
        
        full_text = ""
        for tok in self.llm.generate_stream(system_prompt, query):
            full_text += tok
            if on_token: on_token(tok)
        return full_text

    def sync(self, on_status=None):
        if on_status: on_status("[SYNC] 🚀 启动全量双轨同步任务...")
        try:
            import sys
            from pathlib import Path
            sys.path.append(str(Path("d:/project/wikicode")))
            from src.skills.wiki_skill import sync_kb
            sync_kb()
            from graphify_out.expert_sync import build_expert_graph
            build_expert_graph()
            return True
        except Exception as e:
            if on_status: on_status(f"[ERROR] ❌ 同步中断: {str(e)}")
            return False
