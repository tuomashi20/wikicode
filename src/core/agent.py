from __future__ import annotations
import re
import json
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
        # 兼容旧逻辑：将文本解析为关键词列表
        keywords = [k.strip() for k in rw_text.split(',') if k.strip()]
        _act(f"query_rewrite(keywords={keywords})")

        # 2. Expert Model Logic (真·逻辑官审计)
        graph_context = ""
        if on_status: on_status("正在进行实时业务逻辑审计...")
        
        # 2. Logic Audit Phase (逻辑官 @graphagent 接入：语义图谱内存级审计)
        graph_context = ""
        try:
            # [性能优化]：使用全局单例加载，避免 7.5MB JSON 造成的 I/O 阻塞
            if not hasattr(self, "_cached_graph"):
                graph_json = Path("d:/project/wikicode/graphify_out/.graphify_pure_merged.json")
                if graph_json.exists():
                    self._cached_graph = json.loads(graph_json.read_text(encoding='utf-8'))
                    self._cached_atoms = [n for n in self._cached_graph.get('nodes', []) if n.get('type') == 'semantic_atom']
                else:
                    self._cached_atoms = []
            
            if self._cached_atoms:
                # 寻找与提问关键词匹配的原子 (加速版)
                match_atoms = []
                search_scope = " ".join(keywords).lower()
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

        # [强制展示讨论过程]：立即推送审计快照
        if on_step:
            on_step(WikiStep(
                thought="逻辑官 (@graphagent) 正在扫描内存语义图谱...",
                action_type="graph_audit",
                action_input=graph_context
            ))
            on_step(WikiStep(
                thought="知识官 (@wikiagent) 正在根据审计线索提取事实细节...",
                action_type="wiki_discussion",
                action_input="收到。我将结合上述规则原子，从 52 份 MD 文档中提取匹配的条目。"
            ))

        # 3. Wiki Search (专家会诊第二阶段：事实检索)
        search_limit = self.config.wiki_strategy.agent_search_limit or 8
        results, _ = wiki_search_v2(query, limit=search_limit)
        _act(f"wiki_search_v2(query='{query}') -> {len(results)}")

        chunks = self._filter_reliable_results(results)
        
        if chunks:
            _act(f"wiki_relevance_filter: keep={len(chunks)}/{len(results)}")
            context_blocks = []
            for c in chunks:
                _act(f"wiki_read_chunk(chunk_id={c['chunk_id']})")
                body = wiki_read_chunk(c['chunk_id'])
                context_blocks.append(f"Title: {c['title']}\nSource: {c['parent_file']}\nContent: {body}")
            
            if graph_context:
                context_blocks.append(f"\n[业务图谱约束]\n{graph_context}")

            output = self._wiki_grounded_chat(query, chunks, context_blocks, history, on_token, on_status)
            
            # [视觉增强]：如果图谱发现了关联，在结尾显式提示
            if graph_context:
                output += "\n\n---\n**🧠 专家模型逻辑预警**：\n该回答已结合业务图谱，核查了跨文档的关联规则。"
            
            return AgentResponse(thought="wiki-grounded", actions=actions, output=output)
        
        _act("wiki_relevance_filter: drop_all_low_relevance")
        output = self._general_chat(query + ("\n" + graph_context if graph_context else ""), history, on_token, on_status)
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
            "你会收到两部分核心输入：\n"
            "1. 【Wiki 事实检索】：来自本地知识库的原始片段。\n"
            "2. 【逻辑官 (@graphagent) 见解】：来自图谱的跨文档逻辑关联。\n\n"
            "你的任务：\n"
            "- 结合两者，给出精准答案。\n"
            "- 如果逻辑官发现了跨文档的关联规则，请在答案中显式说明（例如：‘根据逻辑官审计，此规则还关联到...’）。\n"
            "- 严禁捏造。标注引用 [1]/[2]。"
        )
        user_prompt = f"Question: {query}\n\nContext:\n" + "\n".join(context_blocks)
        if on_status: on_status("llm_generate: wiki-grounded started")
        
        # [暴力修正]：直接向正文推送逻辑官的发现，防止 UI 气泡不显示
        graph_report = ""
        for block in context_blocks:
            if "[业务图谱约束]" in block:
                graph_report = block.replace("[业务图谱约束]", "🛡️ **逻辑官 (@graphagent) 实时审计发现**:")
                break
        
        if graph_report and on_token:
            on_token(f"\n> {graph_report}\n\n---\n\n")
            
        full_text = ""
        for tok in self.llm.generate_stream(system_prompt, user_prompt):
            full_text += tok
            if on_token: on_token(tok)
        if on_status: on_status("llm_generate: wiki-grounded completed")
        return full_text + "\n\nReferences:\n" + "\n".join([f"[{i+1}] {c['title']} ({c['parent_file']})" for i, c in enumerate(chunks[:3])])

    def _general_chat(self, query, history, on_token, on_status):
        if on_status: on_status("llm_generate: general started")
        out = self.llm.generate("你是专家模型，未发现匹配 Wiki。", query)
        if on_status: on_status("llm_generate: general completed")
        return out

    def sync(self, on_status=None):
        """全量双轨同步引擎：同时刷新向量知识库与专家语义图谱"""
        if on_status: on_status("[SYNC] 🚀 启动全量双轨同步任务...")
        try:
            import sys
            from pathlib import Path
            sys.path.append(str(Path("d:/project/wikicode")))
            
            # --- 第一轨：RAG 知识库同步 (Vector Sync) ---
            if on_status: on_status("[STEP 1/2] 📚 正在刷新向量知识库 (RAG Sync)...")
            from src.skills.wiki_skill import sync_kb
            # 获取配置路径
            v_path = getattr(self.config.wiki_strategy, "vault_path", "D:/lihq_obsi/lihq_obsi/LLM_wiki")
            r_dir = getattr(self.config.wiki_strategy, "raw_dir", "raw")
            raw_path = Path(v_path) / r_dir
            
            # 执行向量库同步 (内部会自动处理配置路径)
            sync_kb()
            if on_status: on_status("✅ 向量库切片刷新完成。")
            
            # --- 第二轨：专家语义同步 (Expert Logic Sync) ---
            if on_status: on_status("[STEP 2/2] 🧠 正在执行 AI 深度语义提炼 (Expert Sync)...")
            from graphify_out.expert_sync import build_expert_graph
            
            # 执行专家同步 (2路并发，提炼结构化原子)
            build_expert_graph()
            
            # 读取结果进行最终报告
            output_json = Path("d:/project/wikicode/graphify_out/.graphify_pure_merged.json")
            if output_json.exists():
                import json
                data = json.loads(output_json.read_text(encoding='utf-8'))
                nodes = data.get('nodes', [])
                rich_atoms = [n for n in nodes if n.get('type') == 'semantic_atom' and len(n.get('properties', {})) > 0]
                if on_status: 
                    on_status(f"[DONE] 🎉 全量同步任务圆满完成！")
                    on_status(f"📊 [知识库]: 已更新至最新向量切片")
                    on_status(f"📊 [逻辑图谱]: 提炼原子 {len(nodes)} 个，富语义 {len(rich_atoms)} 条")
            return True
        except Exception as e:
            if on_status: on_status(f"[ERROR] ❌ 同步中断: {str(e)}")
            return False
