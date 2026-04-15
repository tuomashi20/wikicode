from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.core.llm_client import LLMClient
from src.skills.wiki_tools import wiki_read_chunk, wiki_search
from src.utils.config import AppConfig
from src.utils.logger import get_file_logger


ResponseMode = Literal["answer", "patch"]


@dataclass
class AgentResponse:
    thought: str
    actions: list[str]
    output: str


class WikiFirstAgent:
    """Wiki-first ReAct: try Wiki grounding first, fallback to general LLM when no Wiki hit."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = get_file_logger("session", "session.log")
        self.llm = LLMClient(config.llm)

    def run(
        self,
        user_input: str,
        force_wiki: bool = False,
        code_context: str = "",
        response_mode: ResponseMode = "answer",
        target_file: str = "",
    ) -> AgentResponse:
        actions: list[str] = []
        query = user_input.strip()

        if not query and not force_wiki:
            return AgentResponse(thought="empty-input", actions=actions, output="请输入问题。")

        thought = "Wiki-first: search wiki before final response."

        results = wiki_search(query, limit=8) if query else []
        actions.append(f"wiki_search(query={query!r}) -> {len(results)}")

        chunks: list[dict[str, str]] = []
        for r in results[:3]:
            content = wiki_read_chunk(r["chunk_id"])
            actions.append(f"wiki_read_chunk(chunk_id={r['chunk_id']})")
            chunks.append(
                {
                    "chunk_id": str(r["chunk_id"]),
                    "title": str(r["title"]),
                    "parent_file": str(r["parent_file"]),
                    "content": content,
                }
            )

        if not chunks:
            output = self._general_chat(
                query=query,
                actions=actions,
                code_context=code_context,
                response_mode=response_mode,
                target_file=target_file,
            )
            self.logger.info("thought=%s | actions=%s | output_len=%s", thought, actions, len(output))
            return AgentResponse(thought=thought + " (fallback-general)", actions=actions, output=output)

        output = self._wiki_grounded_chat(
            query=query,
            chunks=chunks,
            actions=actions,
            code_context=code_context,
            response_mode=response_mode,
            target_file=target_file,
        )
        self.logger.info("thought=%s | actions=%s | output_len=%s", thought, actions, len(output))
        return AgentResponse(thought=thought, actions=actions, output=output)

    def _wiki_grounded_chat(
        self,
        query: str,
        chunks: list[dict[str, str]],
        actions: list[str],
        code_context: str = "",
        response_mode: ResponseMode = "answer",
        target_file: str = "",
    ) -> str:
        context_blocks = []
        for idx, c in enumerate(chunks, start=1):
            context_blocks.append(
                f"[CHUNK {idx}]\n"
                f"id: {c['chunk_id']}\n"
                f"title: {c['title']}\n"
                f"source: {c['parent_file']}\n"
                f"content:\n{c['content'][:1800]}"
            )

        style = self.config.wiki_strategy.style_guidelines or {}
        style_text = ", ".join(f"{k}={v}" for k, v in style.items()) if style else "none"

        if response_mode == "patch":
            system_prompt = (
                "你是资深代码审阅助手。优先遵循提供的 Wiki 规范。"
                "输出必须是 unified diff（git diff 风格），必须包含 "
                "'--- a/<file>' 和 '+++ b/<file>' 以及 '@@' hunk。不要输出解释。"
                "如果不需要改动，输出字符串: NO_CHANGES。"
            )
            code_part = f"\n\n目标文件: {target_file}\n代码:\n{code_context[:9000]}" if code_context else ""
            user_prompt = (
                f"需求: {query}\n\n"
                "Wiki 规范:\n"
                + "\n\n".join(context_blocks)
                + code_part
                + "\n\n仅返回 unified diff。"
            )
        else:
            system_prompt = (
                "你是 WikiCoder 助手。必须优先遵循提供的 Wiki 规范。"
                "若规范与常识冲突，以规范为准；若规范不足，请明确假设。"
                f"风格约束: {style_text}。"
            )
            code_part = f"\n\n当前代码上下文:\n{code_context[:6000]}" if code_context else ""
            user_prompt = (
                f"用户问题:\n{query}\n\n"
                "相关 Wiki 规范片段:\n"
                + "\n\n".join(context_blocks)
                + code_part
                + "\n\n请基于这些规范回答，并在末尾给出“参考片段”（title + source）。"
            )

        try:
            llm_text = self.llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)
            actions.append(
                f"llm_generate(provider={self.config.llm.provider}, model={self.config.llm.model}, mode=wiki:{response_mode})"
            )
            return llm_text or "LLM 返回为空，请检查模型配置。"
        except Exception as e:  # noqa: BLE001
            actions.append(f"llm_generate(failed, mode=wiki:{response_mode})")
            snippet_text = "\n".join([f"- {c['title']} ({c['parent_file']})" for c in chunks])
            return (
                f"LLM 调用失败：{e}\n\n"
                f"已检索规范片段：\n{snippet_text}\n\n"
                "请检查 llm.provider / api_key 配置，或改用 ollama 本地模型。"
            )

    def _general_chat(
        self,
        query: str,
        actions: list[str],
        code_context: str = "",
        response_mode: ResponseMode = "answer",
        target_file: str = "",
    ) -> str:
        if response_mode == "patch":
            system_prompt = (
                "你是资深代码助手。当前没有命中 Wiki 规范。"
                "请直接根据需求生成 unified diff（git diff 风格），必须包含 "
                "'--- a/<file>' 和 '+++ b/<file>' 以及 '@@' hunk。不要解释。"
                "如果不需要修改，输出 NO_CHANGES。"
            )
            code_part = f"\n\n目标文件: {target_file}\n代码:\n{code_context[:9000]}" if code_context else ""
            user_prompt = f"需求:\n{query}{code_part}\n\n仅返回 unified diff。"
        else:
            system_prompt = (
                "你是 WikiCoder 助手。当前没有命中 Wiki 规范。"
                "请直接给出通用、可执行、简洁回答；如果不确定请明确指出。"
            )
            code_part = f"\n\n当前代码上下文:\n{code_context[:6000]}" if code_context else ""
            user_prompt = f"用户问题:\n{query}{code_part}"

        try:
            llm_text = self.llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)
            actions.append(
                f"llm_generate(provider={self.config.llm.provider}, model={self.config.llm.model}, mode=general:{response_mode})"
            )
            return llm_text or "LLM 返回为空，请检查模型配置。"
        except Exception as e:  # noqa: BLE001
            actions.append(f"llm_generate(failed, mode=general:{response_mode})")
            return f"未命中 Wiki，且通用 LLM 调用失败：{e}\n请检查 llm.api_key / provider 配置后重试。"
