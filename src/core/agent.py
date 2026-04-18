from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from src.core.llm_client import LLMClient
from src.skills.wiki_tools import wiki_read_chunk, wiki_search_v2
from src.utils.config import AppConfig
from src.utils.logger import get_file_logger


ResponseMode = Literal["answer", "patch"]
SessionMode = Literal["auto", "wiki_only", "general_only"]


@dataclass
class AgentResponse:
    thought: str
    actions: list[str]
    output: str


class WikiFirstAgent:
    """Wiki-first ReAct: search wiki first, fallback to general model when no reliable hit."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = get_file_logger("session", "session.log")
        self.llm = LLMClient(config.llm)

    def run(
        self,
        user_input: str,
        force_wiki: bool = False,
        mode: SessionMode = "auto",
        code_context: str = "",
        response_mode: ResponseMode = "answer",
        target_file: str = "",
        history: list[tuple[str, str]] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> AgentResponse:
        actions: list[str] = []
        query = user_input.strip()

        def _act(msg: str) -> None:
            actions.append(msg)
            if on_status is not None:
                on_status(msg)

        if not query and not force_wiki:
            return AgentResponse(thought="empty-input", actions=actions, output="Please enter a question.")

        if mode == "general_only":
            thought = "General-only: skip wiki and answer directly."
            if on_status is not None:
                on_status("mode=general_only: skip wiki retrieval")
            output = self._general_chat(
                query=query,
                actions=actions,
                code_context=code_context,
                response_mode=response_mode,
                target_file=target_file,
                history=history,
                on_token=on_token,
                on_status=on_status,
            )
            self.logger.info("thought=%s | actions=%s | output_len=%s", thought, actions, len(output))
            return AgentResponse(thought=thought, actions=actions, output=output)

        thought = "Wiki-first: search wiki before final response."
        if on_status is not None:
            on_status("wiki_search: started")

        results, rw = (
            wiki_search_v2(query, limit=8, synonyms_path=self.config.wiki_strategy.synonyms_path) if query else ([], None)
        )
        if rw is not None:
            _act(f"query_rewrite(keywords={rw.keywords[:6]}, expanded={rw.expanded_terms[:6]})")
        _act(f"wiki_search_v2(query={query!r}) -> {len(results)}")

        terms = self._build_query_terms(query, rw)
        reliable = self._filter_reliable_results(results, terms)
        if results and not reliable:
            _act("wiki_relevance_filter: drop_all_low_relevance")
        elif reliable and len(reliable) < len(results):
            _act(f"wiki_relevance_filter: keep={len(reliable)}/{len(results)}")

        if reliable:
            reasons = [str(r.get("_hit_reason", "")) for r in reliable[:3]]
            _act(f"wiki_hit_reason(top3={reasons})")

        chunks: list[dict[str, str]] = []
        for r in reliable[:3]:
            content = wiki_read_chunk(r["chunk_id"])
            _act(f"wiki_read_chunk(chunk_id={r['chunk_id']})")
            chunks.append(
                {
                    "chunk_id": str(r["chunk_id"]),
                    "title": str(r["title"]),
                    "parent_file": str(r["parent_file"]),
                    "content": content,
                }
            )

        if not chunks and mode == "wiki_only":
            output = "No wiki content matched (wiki_only mode does not fallback to general LLM)."
            self.logger.info("thought=%s | actions=%s | output_len=%s", thought, actions, len(output))
            return AgentResponse(thought=thought + " (wiki-only-nohit)", actions=actions, output=output)

        if not chunks:
            output = self._general_chat(
                query=query,
                actions=actions,
                code_context=code_context,
                response_mode=response_mode,
                target_file=target_file,
                history=history,
                on_token=on_token,
                on_status=on_status,
            )
            if rw is not None and rw.suggest_terms:
                output += "\n\nSuggested keywords: " + ", ".join(rw.suggest_terms[:6])
            self.logger.info("thought=%s | actions=%s | output_len=%s", thought, actions, len(output))
            return AgentResponse(thought=thought + " (fallback-general)", actions=actions, output=output)

        output = self._wiki_grounded_chat(
            query=query,
            chunks=chunks,
            actions=actions,
            code_context=code_context,
            response_mode=response_mode,
            target_file=target_file,
            history=history,
            on_token=on_token,
            on_status=on_status,
        )
        self.logger.info("thought=%s | actions=%s | output_len=%s", thought, actions, len(output))
        return AgentResponse(thought=thought, actions=actions, output=output)

    @staticmethod
    def _build_query_terms(query: str, rw) -> list[str]:
        raw_terms: list[str] = []
        if rw is not None:
            raw_terms.extend([str(x) for x in rw.expanded_terms])
            raw_terms.extend([str(x) for x in rw.keywords])
        raw_terms.append(query)

        out: list[str] = []
        seen: set[str] = set()
        for t in raw_terms:
            parts = re.split(r"[\s,.;:!?()\[\]{}<>\"'`]+", t.strip().lower())
            for part in parts:
                if not part or len(part) < 2:
                    continue
                if part in seen:
                    continue
                seen.add(part)
                out.append(part)
        return out[:16]

    @staticmethod
    def _filter_reliable_results(results: list[dict], terms: list[str]) -> list[dict]:
        if not results or not terms:
            return []

        reliable: list[dict] = []
        for r in results:
            text = " ".join(
                [
                    str(r.get("title", "")),
                    str(r.get("tags", "")),
                    str(r.get("parent_file", "")),
                    str(r.get("content_text", ""))[:1200],
                ]
            ).lower()
            hit = sum(1 for t in terms if t and t in text)
            if hit >= 2:
                reliable.append(r)
                continue
            if hit == 1:
                single = next((t for t in terms if t in text), "")
                if len(single) >= 3:
                    reliable.append(r)
        return reliable

    @staticmethod
    def _is_code_query(query: str, code_context: str = "") -> bool:
        q = (query or "").lower()
        keys = [
            "python",
            "脚本",
            "代码",
            "debug",
            "报错",
            "异常",
            "修复",
            "bug",
            ".py",
            "自动化",
            "合并",
        ]
        return bool(code_context.strip()) or any(k in q for k in keys)

    def _wiki_grounded_chat(
        self,
        query: str,
        chunks: list[dict[str, str]],
        actions: list[str],
        code_context: str = "",
        response_mode: ResponseMode = "answer",
        target_file: str = "",
        history: list[tuple[str, str]] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> str:
        context_blocks = []
        for idx, c in enumerate(chunks, start=1):
            context_blocks.append(
                f"[CHUNK {idx}]\n"
                f"id: {c['chunk_id']}\n"
                f"title: {c['title']}\n"
                f"source: {c['parent_file']}\n"
                f"content:\n{c['content'][:2400]}"
            )

        style = self.config.wiki_strategy.style_guidelines or {}
        style_text = ", ".join(f"{k}={v}" for k, v in style.items()) if style else "none"
        history_block = self._format_history_block(history)

        if response_mode == "patch":
            system_prompt = (
                "You are a senior code review assistant. Prioritize provided wiki policy. "
                "Output MUST use SEARCH/REPLACE block format as shown below. "
                "Do not explain. If no change is needed, output NO_CHANGES.\n\n"
                "=== OUTPUT FORMAT (required) ===\n"
                "For EACH change, output a block like:\n\n"
                "<<<< SEARCH\n"
                "exact lines to find in the original file\n"
                "====\n"
                "replacement lines\n"
                ">>>> REPLACE\n\n"
                "=== EXAMPLE ===\n\n"
                "<<<< SEARCH\n"
                "def old_func():\n"
                "    return 1\n"
                "====\n"
                "def old_func():\n"
                "    return 2\n"
                ">>>> REPLACE\n\n"
                "IMPORTANT: The SEARCH block must exactly match existing code. "
                "Include enough context lines for unique matching. "
                "You may output multiple SEARCH/REPLACE blocks for multiple changes."
            )
            code_part = f"\n\nTarget file: {target_file}\nCode:\n{code_context[:12000]}" if code_context else ""
            user_prompt = (
                f"Requirement: {query}\n\n"
                "Wiki policy:\n"
                + "\n\n".join(context_blocks)
                + history_block
                + code_part
                + "\n\nReturn SEARCH/REPLACE blocks only."
            )
        else:
            system_prompt = (
                "You are WikiCoder assistant. Follow provided wiki policy first. "
                "If policy conflicts with common practice, policy wins; if policy is insufficient, state assumptions. "
                f"Style constraints: {style_text}."
            )
            code_part = f"\n\nCurrent code context:\n{code_context[:8000]}" if code_context else ""
            user_prompt = (
                f"User question:\n{query}\n\n"
                "Relevant wiki snippets:\n"
                + "\n\n".join(context_blocks)
                + history_block
                + code_part
                + "\n\nAnswer based on policy and cite evidence with [1]/[2]/[3] when possible."
            )

        try:
            if on_status is not None:
                on_status("llm_generate: wiki-grounded started")
            parts: list[str] = []
            for tok in self.llm.generate_stream(system_prompt=system_prompt, user_prompt=user_prompt):
                if not tok:
                    continue
                parts.append(tok)
                if on_token is not None:
                    on_token(tok)
            llm_text = "".join(parts).strip()
            if not llm_text:
                llm_text = self.llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)
            actions.append(
                f"llm_generate(provider={self.config.llm.provider}, model={self.config.llm.model}, mode=wiki:{response_mode})"
            )
            if on_status is not None:
                on_status("llm_generate: wiki-grounded completed")
            output = llm_text or "LLM returned empty output."
            if response_mode == "answer":
                output2 = self._ensure_citations(output, chunks)
                if on_token is not None and output2.startswith(output):
                    tail = output2[len(output):]
                    if tail:
                        on_token(tail)
                output = output2
            return output
        except Exception as e:  # noqa: BLE001
            actions.append(f"llm_generate(failed, mode=wiki:{response_mode})")
            if on_status is not None:
                on_status(f"llm_generate: failed ({e})")
            snippet_text = "\n".join([f"- {c['title']} ({c['parent_file']})" for c in chunks])
            return (
                f"LLM call failed: {e}\n\n"
                f"Retrieved wiki snippets:\n{snippet_text}\n\n"
                "Please verify llm.provider / api_key configuration."
            )

    def _general_chat(
        self,
        query: str,
        actions: list[str],
        code_context: str = "",
        response_mode: ResponseMode = "answer",
        target_file: str = "",
        history: list[tuple[str, str]] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> str:
        history_block = self._format_history_block(history)

        if response_mode == "patch":
            system_prompt = (
                "You are a senior code assistant. No wiki policy matched. "
                "Output MUST use SEARCH/REPLACE block format. "
                "No explanation. If no change is needed, output NO_CHANGES.\n\n"
                "=== OUTPUT FORMAT (required) ===\n"
                "For EACH change, output a block like:\n\n"
                "<<<< SEARCH\n"
                "exact lines to find in the original file\n"
                "====\n"
                "replacement lines\n"
                ">>>> REPLACE\n\n"
                "IMPORTANT: The SEARCH block must exactly match existing code. "
                "Include enough context lines for unique matching."
            )
            code_part = f"\n\nTarget file: {target_file}\nCode:\n{code_context[:12000]}" if code_context else ""
            user_prompt = f"Requirement:\n{query}{history_block}{code_part}\n\nReturn SEARCH/REPLACE blocks only."
        else:
            if self._is_code_query(query, code_context):
                system_prompt = (
                    "You are a practical coding assistant. "
                    "Prioritize directly usable code and executable steps. "
                    "When fixing issues, explain root cause briefly, then provide corrected code. "
                    "Do not add unnecessary theory."
                )
            else:
                system_prompt = (
                    "You are WikiCoder assistant. No wiki policy matched. "
                    "Answer user questions directly; do NOT limit to wiki-domain topics. "
                    "If uncertain, state uncertainty clearly."
                )
            code_part = f"\n\nCurrent code context:\n{code_context[:8000]}" if code_context else ""
            user_prompt = f"User question:\n{query}{history_block}{code_part}"

        try:
            if on_status is not None:
                on_status("llm_generate: general started")
            parts: list[str] = []
            for tok in self.llm.generate_stream(system_prompt=system_prompt, user_prompt=user_prompt):
                if not tok:
                    continue
                parts.append(tok)
                if on_token is not None:
                    on_token(tok)
            llm_text = "".join(parts).strip()
            if not llm_text:
                llm_text = self.llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)
            actions.append(
                f"llm_generate(provider={self.config.llm.provider}, model={self.config.llm.model}, mode=general:{response_mode})"
            )
            if on_status is not None:
                on_status("llm_generate: general completed")
            return llm_text or "LLM returned empty output."
        except Exception as e:  # noqa: BLE001
            actions.append(f"llm_generate(failed, mode=general:{response_mode})")
            if on_status is not None:
                on_status(f"llm_generate: failed ({e})")
            return f"Wiki miss and general LLM call failed: {e}\nPlease verify llm.api_key / provider config."

    @staticmethod
    def _format_history_block(history: list[tuple[str, str]] | None, max_turns: int = 8, max_chars: int = 4800) -> str:
        if not history:
            return ""
        turns = history[-max_turns:]
        lines = ["\n\nRecent conversation context:"]
        for idx, (q, a) in enumerate(turns, start=1):
            q1 = (q or "").strip()
            a1 = (a or "").strip()
            if len(a1) > 600:
                a1 = a1[:600] + "..."
            lines.append(f"\n[{idx}] user: {q1}")
            lines.append(f"[{idx}] assistant: {a1}")
        out = "\n".join(lines)
        if len(out) > max_chars:
            out = out[-max_chars:]
        return out

    @staticmethod
    def _render_citations(chunks: list[dict[str, str]]) -> str:
        lines = ["References:"]
        for i, c in enumerate(chunks[:3], start=1):
            line_range = WikiFirstAgent._chunk_local_line_range(c.get("content", ""))
            snippet = WikiFirstAgent._evidence_snippet(c.get("content", ""))
            anchor = f"chunk://{c['chunk_id']}#L{line_range}"
            lines.append(
                f"- [{i}] {c['title']} | source={c['parent_file']} | chunk_id={c['chunk_id']} | anchor={anchor}"
            )
            if snippet:
                lines.append(f"  snippet: {snippet}")
        return "\n".join(lines)

    def _ensure_citations(self, answer: str, chunks: list[dict[str, str]]) -> str:
        if not chunks:
            return answer
        citations = self._render_citations(chunks)
        if "References:" in answer or "参考片段" in answer:
            return answer
        out = answer.rstrip()
        if "[1]" not in out and "[2]" not in out and "[3]" not in out:
            out = self._auto_attach_citation_markers(out, chunks)
            out += "\n\n(Evidence markers: [1]/[2]/[3] correspond to References below.)"
        return out + "\n\n" + citations

    @staticmethod
    def _auto_attach_citation_markers(answer: str, chunks: list[dict[str, str]]) -> str:
        lines = answer.splitlines()
        out: list[str] = []
        for ln in lines:
            s = ln.strip()
            if not s:
                out.append(ln)
                continue
            if not WikiFirstAgent._should_cite_line(s):
                out.append(ln)
                continue
            idx = WikiFirstAgent._best_chunk_index(s, chunks)
            if idx > 0 and f"[{idx}]" not in s:
                out.append(f"{ln} [{idx}]")
            else:
                out.append(ln)
        return "\n".join(out)

    @staticmethod
    def _should_cite_line(text: str) -> bool:
        s = text.strip()
        if len(s) < 12:
            return False
        # skip pure heading / divider / obvious non-factual lines
        if s.startswith(("#", "---", "```")):
            return False
        if s.lower().startswith(("summary", "结论", "建议", "tips", "注意")) and len(s) < 24:
            return False
        # questions are usually not factual assertions
        if "?" in s or "？" in s:
            return False
        # require some lexical substance
        has_en = bool(re.search(r"[a-zA-Z]{3,}", s))
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]{4,}", s))
        has_num = bool(re.search(r"\d", s))
        return has_en or has_cjk or has_num

    @staticmethod
    def _best_chunk_index(text: str, chunks: list[dict[str, str]]) -> int:
        q_terms = WikiFirstAgent._lex_terms(text)
        if not q_terms:
            return 0
        best_idx = 0
        best_score = 0
        for i, c in enumerate(chunks[:3], start=1):
            c_terms = WikiFirstAgent._lex_terms(
                f"{c.get('title', '')} {c.get('parent_file', '')} {c.get('content', '')[:800]}"
            )
            if not c_terms:
                continue
            score = len(q_terms & c_terms)
            if score > best_score:
                best_score = score
                best_idx = i
        return best_idx if best_score > 0 else 0

    @staticmethod
    def _lex_terms(text: str) -> set[str]:
        t = text.lower()
        terms = set(re.findall(r"[a-z0-9_]{2,}", t))
        cjk = "".join(re.findall(r"[一-鿿]+", text))
        for i in range(0, max(0, len(cjk) - 1)):
            terms.add(cjk[i : i + 2])
        return {x for x in terms if x}

    @staticmethod
    def _chunk_local_line_range(content: str) -> str:
        lines = content.splitlines()
        if not lines:
            return "1-1"
        return f"1-{len(lines)}"

    @staticmethod
    def _evidence_snippet(content: str, max_len: int = 140) -> str:
        lines = [x.strip() for x in content.splitlines() if x.strip()]
        body = " ".join(lines[1:] if len(lines) > 1 else lines)
        if not body:
            return ""
        return body if len(body) <= max_len else (body[:max_len].rstrip() + "...")
