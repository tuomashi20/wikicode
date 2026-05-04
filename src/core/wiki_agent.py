"""
WikiAgent: 知识库检索子Agent
通过 @wikiagent 唤醒，执行多步推理循环完成知识库检索
"""
import json
import re
from typing import Any, Callable, Optional

from src.core.llm_client import LLMClient
from src.skills.wiki_tools import wiki_search, wiki_list, wiki_read


class WikiAgent:
    """知识库检索子Agent，通过 @wikiagent 唤醒"""

    MAX_STEPS = 10

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self._action_hashes: list[str] = []

    def search(self, query: str, on_log: Optional[Callable[[str], None]] = None) -> str:
        """
        执行多步知识库检索，返回带路径引用的结构化摘要。

        参数:
            query: 用户的检索关键词
            on_log: 可选的日志回调函数，用于向 TUI 推送检索进度
        返回:
            结构化的知识库摘要文本
        """
        self._action_hashes = []

        system_prompt = (
            "你是 WikiAgent，一个专注于企业内部知识库检索的子 Agent。\n"
            "你的唯一目标是根据用户的查询，在知识库中找到最相关的信息并汇总返回。\n\n"
            "【可用动作】：\n"
            "1. wiki_search: 语义搜索知识库片段。格式: {\"query\": \"关键词\"}\n"
            "2. wiki_list: 查看知识库目录结构。格式: {\"sub_dir\": \"\"}\n"
            "3. wiki_read: 通读指定的规范文件。格式: {\"path\": \"文件路径\"}\n"
            "4. finish: 检索完成，输出汇总结果。注意：汇总结果必须是结构化的 Markdown，包含清晰的标题和要点。每条关键结论必须在末尾使用 [Source: 相对路径] 标注出处。\n\n"
            "【决策准则】：\n"
            "- 先用 wiki_search 快速定位，再用 wiki_read 深挖关键文件\n"
            "- 汇总结果必须高度整理，禁止简单堆砌原文\n"
            "- 必须标注知识来源路径，且路径应为真实的相对路径\n"
            "- 如果搜索无结果，尝试换关键词或用 wiki_list 浏览目录\n"
            "- 最多执行 10 步，尽快得出结论\n\n"
            "【输出格式 - JSON】：\n"
            '{"thought": "思考过程", "action": "wiki_search|wiki_list|wiki_read|finish", "input": "汇总后的结构化内容"}'
        )

        context = f"用户查询: {query}\n"
        steps_log = []

        for i in range(self.MAX_STEPS):
            prompt = context + "\n执行记录:\n"
            for idx, s in enumerate(steps_log[-5:]):
                prompt += f"步骤{idx+1}: {s}\n"
            prompt += "\n请给出下一步行动的 JSON:"

            try:
                resp = self.llm.generate(system_prompt, prompt)
                decision = self._parse_json(resp)
                if not decision:
                    return f"[WikiAgent] 解析失败: {resp[:200]}"

                action = decision.get("action", "")
                action_input = decision.get("input", "")
                thought = decision.get("thought", "")

                if isinstance(action_input, (dict, list)):
                    action_input = json.dumps(action_input, ensure_ascii=False)

                # 日志推送
                if on_log:
                    on_log(f"[WikiAgent] 步骤{i+1}: {thought[:80]}... → {action}")

                # 完成
                if action == "finish":
                    return f"[WikiAgent 检索结果]\n{action_input}"

                # 死循环检测
                h = f"{action}:{action_input}".strip()
                if self._action_hashes.count(h) >= 2:
                    return "[WikiAgent] 检索循环，已中止。请尝试更具体的关键词。"
                self._action_hashes.append(h)

                # 执行动作
                obs = self._execute(action, action_input)
                steps_log.append(f"Thought: {thought}\nAction: {action}({action_input})\nObs: {obs[:1500]}")

            except Exception as e:
                return f"[WikiAgent] 执行异常: {e}"

        return "[WikiAgent] 达到最大步数，未完成检索。"

    def _execute(self, action: str, action_input: str) -> str:
        """执行知识库操作"""
        try:
            if action == "wiki_search":
                try:
                    data = json.loads(action_input)
                    q = data.get("query", action_input)
                except (json.JSONDecodeError, TypeError):
                    q = action_input
                return wiki_search(str(q), llm=self.llm)

            elif action == "wiki_list":
                try:
                    data = json.loads(action_input)
                    sd = data.get("sub_dir", "")
                except (json.JSONDecodeError, TypeError):
                    sd = str(action_input) if action_input else ""
                return wiki_list(sd)

            elif action == "wiki_read":
                try:
                    data = json.loads(action_input)
                    p = data.get("path", action_input)
                except (json.JSONDecodeError, TypeError):
                    p = action_input
                return wiki_read(p)

            return f"未知动作: {action}"
        except Exception as e:
            return f"执行异常: {e}"

    def _parse_json(self, text: str) -> dict | None:
        """健壮的 JSON 解析"""
        try:
            start = text.find('{')
            end = text.rfind('}')
            if start == -1 or end == -1:
                return None
            return json.loads(text[start:end+1])
        except json.JSONDecodeError:
            res = {}
            for key in ["action", "thought"]:
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


def extract_wiki_query(user_input: str) -> tuple[str, str]:
    """
    从用户输入中提取 @wikiagent 查询和剩余文本。

    返回: (wiki_query, remaining_text)
    示例:
        "根据 @wikiagent 编码规范 帮我重构" → ("编码规范", "根据  帮我重构")
        "@wikiagent 运维手册" → ("运维手册", "")
    """
    pattern = r'@wikiagent\s+(.*?)(?:\s+(?:帮|请|给|用|按照|根据|依据)|$)'
    m = re.search(pattern, user_input, re.IGNORECASE)
    if m:
        wiki_query = m.group(1).strip()
        remaining = re.sub(r'@wikiagent\s+' + re.escape(wiki_query), '', user_input).strip()
        return wiki_query, remaining

    # 简单回退：取 @wikiagent 后的所有内容作为查询
    parts = re.split(r'@wikiagent\s*', user_input, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2:
        return parts[1].strip(), parts[0].strip()

    return user_input, ""
