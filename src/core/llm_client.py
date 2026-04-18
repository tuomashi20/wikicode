from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import httpx

from src.utils.config import LLMConfig


class LLMClient:
    JIUTIAN_CHAT_URL = "https://jiutian.10086.cn/largemodel/moma/api/v3/chat/completions"
    JIUTIAN_BASE_URL = "https://jiutian.10086.cn/largemodel/moma/api/v3"
    JIUTIAN_IMAGE_UNDERSTAND_URL = "https://jiutian.10086.cn/largemodel/moma/api/v3/image/text"
    JIUTIAN_IMAGE_GENERATE_URL = "https://jiutian.10086.cn/largemodel/moma/api/v3/images/generations"

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.provider = config.provider.lower().strip()

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        if self.provider == "jiutian":
            return self._call_jiutian_chat(system_prompt, user_prompt)
        if self.provider == "ollama":
            return self._call_ollama(system_prompt, user_prompt)
        if self.provider in {"google_api_studio", "google", "gemini"}:
            return self._call_google(system_prompt, user_prompt)
        return self._call_openai(system_prompt, user_prompt)

    def generate_stream(self, system_prompt: str, user_prompt: str) -> Iterator[str]:
        if self.provider == "jiutian":
            yield from self._call_jiutian_chat_stream(system_prompt, user_prompt)
            return
        if self.provider in {"openai", "openai_compatible"}:
            yield from self._call_openai_stream(system_prompt, user_prompt)
            return
        if self.provider == "ollama":
            yield from self._call_ollama_stream(system_prompt, user_prompt)
            return
        if self.provider in {"google_api_studio", "google", "gemini"}:
            yield from self._call_google_stream(system_prompt, user_prompt)
            return
        # fallback: 不支持流式的 provider 一次性输出
        text = self.generate(system_prompt, user_prompt)
        if text:
            yield text

    def image_understand(self, prompt: str, image_url: str) -> str:
        if self.provider != "jiutian":
            raise RuntimeError("image_understand currently supports provider=jiutian only.")
        if not self.config.api_key:
            raise RuntimeError("Missing Jiutian API key. Set llm.api_key or JIUTIAN_API_KEY.")
        model = self.config.image_understand_model or self.config.model
        payload = {
            "model": model,
            "temperature": self.config.temperature,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        data = self._post_json(self._jiutian_image_understand_url(), payload, headers)
        return self._extract_text_response(data)

    def image_generate(self, prompt: str, size: str = "1024x1024") -> str:
        if self.provider != "jiutian":
            raise RuntimeError("image_generate currently supports provider=jiutian only.")
        if not self.config.api_key:
            raise RuntimeError("Missing Jiutian API key. Set llm.api_key or JIUTIAN_API_KEY.")
        model = self.config.image_generate_model
        if not model:
            raise RuntimeError("Missing llm.image_generate_model in config for Jiutian image generation.")
        payload = {
            "model": model,
            "size": size,
            "prompt": prompt,
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        data = self._post_json(self._jiutian_image_generate_url(), payload, headers)
        # Return full JSON text because different image models may return URL/base64 in different fields.
        return json.dumps(data, ensure_ascii=False, indent=2)

    def _post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"LLM HTTP {e.code}: {detail[:500]}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"LLM connection error: {e}") from e

    def _call_openai(self, system_prompt: str, user_prompt: str) -> str:
        if not self.config.api_key:
            raise RuntimeError("Missing OpenAI API key. Set llm.api_key or OPENAI_API_KEY.")

        base = (self.config.base_url or "https://api.openai.com").rstrip("/")
        url = f"{base}/v1/chat/completions"
        payload = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        data = self._post_json(url, payload, headers)
        return self._extract_text_response(data)

    def _call_jiutian_chat(self, system_prompt: str, user_prompt: str) -> str:
        if not self.config.api_key:
            raise RuntimeError("Missing Jiutian API key. Set llm.api_key or JIUTIAN_API_KEY.")
        # Prefer OpenAI-compatible SDK path (same as Jiutian official sample).
        try:
            from openai import OpenAI  # type: ignore

            client = OpenAI(
                base_url=self._jiutian_base_url(),
                api_key=self.config.api_key,
            )
            resp = client.chat.completions.create(
                model=self.config.model or "jiutian-think-v3",
                temperature=self.config.temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                stream=False,
            )
            content = resp.choices[0].message.content if resp.choices else ""
            return (content or "").strip()
        except Exception:
            # Fallback to raw HTTP
            payload = {
                "model": self.config.model or "jiutian-think-v3",
                "temperature": self.config.temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            headers = {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            }
            data = self._post_json_jiutian(self._jiutian_chat_url(), payload, headers)
            return self._extract_text_response(data)

    def _call_jiutian_chat_stream(self, system_prompt: str, user_prompt: str) -> Iterator[str]:
        if not self.config.api_key:
            raise RuntimeError("Missing Jiutian API key. Set llm.api_key or JIUTIAN_API_KEY.")
        try:
            from openai import OpenAI  # type: ignore

            client = OpenAI(
                base_url=self._jiutian_base_url(),
                api_key=self.config.api_key,
            )
            stream = client.chat.completions.create(
                model=self.config.model or "jiutian-think-v3",
                temperature=self.config.temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                stream=True,
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                piece = ""
                if hasattr(delta, "content") and delta.content:
                    piece = str(delta.content)
                if piece:
                    yield piece
            return
        except Exception:
            # fallback to one-shot request
            text = self._call_jiutian_chat(system_prompt, user_prompt)
            if text:
                yield text

    def _call_google(self, system_prompt: str, user_prompt: str) -> str:
        if not self.config.api_key:
            raise RuntimeError("Missing Google API key. Set llm.api_key or GOOGLE_API_KEY.")

        base = (self.config.base_url or "https://generativelanguage.googleapis.com").rstrip("/")
        model = self.config.model
        url = f"{base}/v1beta/models/{model}:generateContent?key={self.config.api_key}"

        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": self.config.temperature,
            },
        }
        headers = {"Content-Type": "application/json"}
        data = self._post_json(url, payload, headers)

        candidates = data.get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(str(p.get("text", "")) for p in parts)
        return text.strip()

    def _call_openai_stream(self, system_prompt: str, user_prompt: str) -> Iterator[str]:
        """OpenAI 兼容的流式输出。"""
        try:
            from openai import OpenAI  # type: ignore
            base = (self.config.base_url or "https://api.openai.com/v1").rstrip("/")
            client = OpenAI(base_url=base, api_key=self.config.api_key)
            stream = client.chat.completions.create(
                model=self.config.model,
                temperature=self.config.temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                stream=True,
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if hasattr(delta, "content") and delta.content:
                    yield str(delta.content)
            return
        except ImportError:
            # openai SDK 未安装时回退到一次性请求
            text = self._call_openai(system_prompt, user_prompt)
            if text:
                yield text
        except Exception:
            text = self._call_openai(system_prompt, user_prompt)
            if text:
                yield text

    def _call_ollama_stream(self, system_prompt: str, user_prompt: str) -> Iterator[str]:
        """Ollama 流式输出。"""
        base = (self.config.base_url or "http://localhost:11434").rstrip("/")
        url = f"{base}/api/chat"
        payload = {
            "model": self.config.model,
            "stream": True,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "options": {"temperature": self.config.temperature},
        }
        try:
            with httpx.Client(timeout=self.config.timeout_seconds) as client:
                with client.stream("POST", url, json=payload) as resp:
                    for line in resp.iter_lines():
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                            piece = data.get("message", {}).get("content", "")
                            if piece:
                                yield piece
                            if data.get("done", False):
                                return
                        except (json.JSONDecodeError, ValueError):
                            continue
        except Exception:
            text = self._call_ollama(system_prompt, user_prompt)
            if text:
                yield text

    def _call_google_stream(self, system_prompt: str, user_prompt: str) -> Iterator[str]:
        """Google Gemini 流式输出。"""
        base = (self.config.base_url or "https://generativelanguage.googleapis.com").rstrip("/")
        model = self.config.model
        url = f"{base}/v1beta/models/{model}:streamGenerateContent?alt=sse&key={self.config.api_key}"
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": self.config.temperature},
        }
        try:
            with httpx.Client(timeout=self.config.timeout_seconds) as client:
                with client.stream("POST", url, json=payload, headers={"Content-Type": "application/json"}) as resp:
                    for line in resp.iter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data: "):
                            continue
                        try:
                            data = json.loads(line[6:])
                            candidates = data.get("candidates", [])
                            if candidates:
                                parts = candidates[0].get("content", {}).get("parts", [])
                                for p in parts:
                                    t = p.get("text", "")
                                    if t:
                                        yield t
                        except (json.JSONDecodeError, ValueError):
                            continue
        except Exception:
            text = self._call_google(system_prompt, user_prompt)
            if text:
                yield text

    def _call_ollama(self, system_prompt: str, user_prompt: str) -> str:
        base = (self.config.base_url or "http://localhost:11434").rstrip("/")
        url = f"{base}/api/chat"
        payload = {
            "model": self.config.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "options": {"temperature": self.config.temperature},
        }
        headers = {"Content-Type": "application/json"}
        data = self._post_json(url, payload, headers)
        return str(data.get("message", {}).get("content", "")).strip()

    def _extract_text_response(self, data: dict[str, Any]) -> str:
        choices = data.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    if isinstance(item, dict) and "text" in item:
                        parts.append(str(item.get("text", "")))
                return "".join(parts).strip()

        # some APIs may return text under different paths
        if "output_text" in data:
            return str(data.get("output_text", "")).strip()
        return ""

    def _jiutian_chat_url(self) -> str:
        raw = (self.config.base_url or "").strip()
        if not raw:
            return self.JIUTIAN_CHAT_URL
        raw = raw.rstrip("/")
        if raw.endswith("/chat/completions"):
            return raw
        if raw.endswith("/v3"):
            return f"{raw}/chat/completions"
        return f"{raw}/chat/completions"

    def _jiutian_base_url(self) -> str:
        raw = (self.config.base_url or "").strip()
        if not raw:
            return self.JIUTIAN_BASE_URL
        raw = raw.rstrip("/")
        if raw.endswith("/chat/completions"):
            return raw.rsplit("/chat/completions", 1)[0]
        return raw

    def _jiutian_image_understand_url(self) -> str:
        raw = (self.config.image_understand_url or "").strip()
        if raw:
            return raw.rstrip("/")
        return self.JIUTIAN_IMAGE_UNDERSTAND_URL

    def _jiutian_image_generate_url(self) -> str:
        raw = (self.config.image_generate_url or "").strip()
        if raw:
            return raw.rstrip("/")
        return self.JIUTIAN_IMAGE_GENERATE_URL

    def _post_json_jiutian(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=self.config.timeout_seconds, trust_env=False) as client:
                resp = client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:500]}")
            return resp.json()
        except httpx.HTTPError as e:
            raise RuntimeError(f"LLM connection error: {e}") from e
