from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / ".wikicoder" / "config.yaml"


@dataclass
class LLMConfig:
    provider: str
    model: str
    api_key: str
    base_url: str | None
    image_understand_model: str | None
    image_generate_model: str | None
    image_understand_url: str | None
    image_generate_url: str | None
    temperature: float
    timeout_seconds: int


@dataclass
class WikiStrategyConfig:
    raw_path: Path
    split_mode: str
    heading_level: int
    style_guidelines: dict[str, Any]


@dataclass
class SyncConfig:
    auto_on_startup: bool


@dataclass
class AppConfig:
    llm: LLMConfig
    wiki_strategy: WikiStrategyConfig
    sync: SyncConfig


REQUIRED_DIRS = [
    PROJECT_ROOT / "data",
    PROJECT_ROOT / "data" / "raw",
    PROJECT_ROOT / "data" / "wiki_processed",
    PROJECT_ROOT / "data" / "wiki_processed" / "chunks",
    PROJECT_ROOT / "logs",
]



def _resolve_path(value: str | Path) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    return p



def _read_api_key(llm_data: dict[str, Any]) -> str:
    configured = str(llm_data.get("api_key", "")).strip()
    if configured and configured not in {"YOUR_KEY", "YOUR_JIUTIAN_API_KEY"} and not configured.startswith("YOUR_"):
        return configured

    provider = str(llm_data.get("provider", "")).strip().lower()
    env_candidates = {
        "openai": ["OPENAI_API_KEY"],
        "google_api_studio": ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
        "ollama": ["OLLAMA_API_KEY"],
        "jiutian": ["JIUTIAN_API_KEY"],
    }.get(provider, ["JIUTIAN_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"])

    for env_name in env_candidates:
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return ""



def ensure_workspace() -> None:
    for d in REQUIRED_DIRS:
        d.mkdir(parents=True, exist_ok=True)



def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    llm_data = data.get("llm", {})
    wiki_data = data.get("wiki_strategy", {})
    sync_data = data.get("sync", {})

    return AppConfig(
        llm=LLMConfig(
            provider=str(llm_data.get("provider", "jiutian")),
            model=str(llm_data.get("model", "jiutian-think-v3")),
            api_key=_read_api_key(llm_data),
            base_url=llm_data.get("base_url"),
            image_understand_model=llm_data.get("image_understand_model"),
            image_generate_model=llm_data.get("image_generate_model"),
            image_understand_url=llm_data.get("image_understand_url"),
            image_generate_url=llm_data.get("image_generate_url"),
            temperature=float(llm_data.get("temperature", 0.2)),
            timeout_seconds=int(llm_data.get("timeout_seconds", 45)),
        ),
        wiki_strategy=WikiStrategyConfig(
            raw_path=_resolve_path(wiki_data.get("raw_path", "./data/raw")),
            split_mode=str(wiki_data.get("split_mode", "heading")),
            heading_level=int(wiki_data.get("heading_level", 2)),
            style_guidelines=wiki_data.get("style_guidelines", {}),
        ),
        sync=SyncConfig(auto_on_startup=bool(sync_data.get("auto_on_startup", True))),
    )
