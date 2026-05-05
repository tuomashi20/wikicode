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
from src.core.mcp_client import GBrainMCPClient

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
            
        try:
            self.gbrain = GBrainMCPClient()
        except Exception as e:
            self.logger.error(f"Failed to load gbrain MCP client: {e}")
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
                gbrain_res = self.gbrain.call_tool("query", {"query": query})
                self.logger.info(f"gbrain query for '{query}' returned: {gbrain_res}")
                if gbrain_res and "No results found" not in gbrain_res and "MCP Tool Error" not in gbrain_res:
                    gbrain_memory = f"【gbrain 经历记忆】:\n{gbrain_res}"
                    _act("gbrain_memory_retrieved")
                    if on_step:
                        on_step(WikiStep(
                            thought="记忆官 (@gbrain) 正在唤醒长效个人经历...",
                            action_type="gbrain_audit",
                            action_input="检索到相关历史经历记录。"
                        ))
            except Exception as e:
                self.logger.error(f"gbrain query failed: {e}")

        # 5. gbrain Memory Write
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
            for c in chunks:
                body = wiki_read_chunk(c['chunk_id'])
                context_blocks.append(f"Title: {c['title']}\nSource: {c['parent_file']}\nContent: {body}")
            
            if graph_context:
                context_blocks.append(f"\n[业务图谱约束]\n{graph_context}")
            if gbrain_memory:
                context_blocks.append(f"\n[个人经历记忆]\n{gbrain_memory}")

            output = self._wiki_grounded_chat(query, chunks, context_blocks, history, on_token, on_status)
            return AgentResponse(thought="wiki-grounded", actions=actions, output=output)
        
        _act("wiki_relevance_filter: drop_all_low_relevance")
        fallback_ctx = []
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
            "1. 【Wiki 事实检索】：来自本地知识库的原始片段。\n"
            "2. 【逻辑官 (@graphagent) 见解】：来自图谱的跨文档逻辑关联。\n"
            "3. 【个人经历 (@gbrain) 记忆】：来自过往的沟通、任务和个人时间线。\n\n"
            "你的任务：\n"
            "- 结合两者，给出精准答案。\n"
            "- 如果逻辑官发现了跨文档的关联规则，请在答案中显式说明。\n"
            "- 严禁捏造。标注引用 [1]/[2]。"
        )
        user_prompt = f"Question: {query}\n\nContext:\n" + "\n".join(context_blocks)
        
        full_text = ""
        for tok in self.llm.generate_stream(system_prompt, user_prompt):
            full_text += tok
            if on_token: on_token(tok)
        return full_text + "\n\nReferences:\n" + "\n".join([f"[{i+1}] {c['title']} ({c['parent_file']})" for i, c in enumerate(chunks[:3])])

    def _general_chat(self, query, history, on_token, on_status):
        out = self.llm.generate("你是专家模型，未发现匹配 Wiki。", query)
        if on_token: on_token(out)
        return out

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
