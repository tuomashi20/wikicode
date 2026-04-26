from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / ".wikicoder" / "config.yaml"
DEFAULT_CONFIG_EXAMPLE_PATH = PROJECT_ROOT / ".wikicoder" / "config.example.yaml"


@dataclass
class LLMConfig:
    provider: str
    model: str
    api_key: str
    base_url: str | None
    image_asset_host: str | None
    image_understand_model: str | None
    image_generate_model: str | None
    image_understand_url: str | None
    image_generate_url: str | None
    temperature: float
    timeout_seconds: int


@dataclass
class WikiStrategyConfig:
    vault_path: Path | None
    temperature: float
    timeout_seconds: int


@dataclass
class WikiStrategyConfig:
    vault_path: Path | None
    raw_path: Path
    wiki_path: Path
    processed_path: Path
    raw_subdirs: list[str]
    wiki_subdirs: list[str]
    raw_to_wiki_map: dict[str, str]
    synonyms_path: Path
    business_terms_path: Path
    split_mode: str
    heading_level: int
    wiki_compile_on_sync: bool
    style_guidelines: dict[str, Any]
    concept_cues: list[str]
    comparison_hints: list[str]
    entity_org_suffixes: list[str]
    entity_type_hints: list[str]
    entity_exclude_terms: list[str]
    entity_content_cues: list[str]
    entity_ignore_terms: list[str]
    entity_card_min_mentions: int
    entity_card_max_pages: int
    entity_card_name_max_len: int
    chapter_title_patterns: list[str]
    chapter_exact_terms: list[str]
    tag_stopwords: list[str]
    tag_block_patterns: list[str]
    tag_block_prefixes: list[str]
    tag_min_len: int
    tag_max_len: int
    # --- RAG 业务策略参数 ---
    rag_retrieval_fanout: int           # 检索初筛的关键词名额 (默认 12)
    rag_rewrite_priority: str           # 重写词优先级 (append/prepend)
    rag_core_boost_score: float         # 核心业务词加权分 (默认 1000)
    rag_link_follow_limit: int          # 双链联动感知深度 (默认 3)
    rag_context_max_chars: int          # 单个 Chunk 注入上下文的最大长度


@dataclass
class SyncConfig:
    auto_on_startup: bool


@dataclass
class AppConfig:
    llm: LLMConfig
    wiki_strategy: WikiStrategyConfig
    sync: SyncConfig



def _resolve_path(value: str | Path) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    return p


def _load_default_rules_from_yaml() -> dict[str, Any]:
    try:
        if not DEFAULT_CONFIG_EXAMPLE_PATH.exists():
            return {}
        data = yaml.safe_load(DEFAULT_CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8")) or {}
        ws = data.get("wiki_strategy") or {}
        rules = ws.get("rules") or {}
        return rules if isinstance(rules, dict) else {}
    except Exception:
        return {}



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


def _infer_wiki_category(name: str) -> str:
    n = str(name).strip().lower()
    if not n:
        return "concepts"
    if any(k in n for k in ["对比", "比较", "评估", "选型", "comparison", "compare"]):
        return "comparisons"
    if any(k in n for k in ["问答", "faq", "问题", "query", "queries"]):
        return "queries"
    if any(k in n for k in ["组织", "角色", "岗位", "客户", "供应商", "终端", "设备", "entity", "entities"]):
        return "entities"
    return "concepts"



def _build_wiki_strategy(wiki_data: dict[str, Any]) -> WikiStrategyConfig:
    vault_raw = str(wiki_data.get("vault_path", "")).strip()
    vault_path = _resolve_path(vault_raw) if vault_raw else None

    raw_dir = str(wiki_data.get("raw_dir", "raw"))
    wiki_dir = str(wiki_data.get("wiki_dir", "wiki"))
    processed_dir = str(wiki_data.get("processed_dir", "wiki_processed"))

    if vault_path:
        raw_default = vault_path / raw_dir
        wiki_default = vault_path / wiki_dir
        processed_default = vault_path / processed_dir
    else:
        raw_default = PROJECT_ROOT / "data" / "raw"
        wiki_default = PROJECT_ROOT / "data" / "wiki"
        processed_default = PROJECT_ROOT / "data" / "wiki_processed"

    raw_path = _resolve_path(wiki_data.get("raw_path", str(raw_default)))
    wiki_path = _resolve_path(wiki_data.get("wiki_path", str(wiki_default)))
    processed_path = _resolve_path(wiki_data.get("processed_path", str(processed_default)))
    synonyms_path = _resolve_path(wiki_data.get("synonyms_path", "./data/dictionaries/synonyms_zh.yaml"))
    business_terms_path = _resolve_path(wiki_data.get("business_terms_path", "./data/dictionaries/business_terms.yaml"))

    # Support both raw_subdirs and legacy typo row_subdirs
    raw_subdirs_cfg = wiki_data.get("raw_subdirs")
    if raw_subdirs_cfg is None:
        raw_subdirs_cfg = wiki_data.get("row_subdirs")
    raw_subdirs = [str(x) for x in (raw_subdirs_cfg or []) if str(x).strip()]
    wiki_subdirs = [str(x) for x in (wiki_data.get("wiki_subdirs") or []) if str(x).strip()]
    raw_to_wiki_map = {
        str(k): str(v)
        for k, v in (wiki_data.get("raw_to_wiki_map") or {}).items()
        if str(k).strip() and str(v).strip()
    }
    if not raw_to_wiki_map and raw_subdirs:
        raw_to_wiki_map = {x: _infer_wiki_category(x) for x in raw_subdirs}

    rules_data = wiki_data.get("rules") or {}
    if not isinstance(rules_data, dict):
        rules_data = {}
    default_rules = _load_default_rules_from_yaml()

    def _rule_list(key: str) -> list[str]:
        v = rules_data.get(key, default_rules.get(key, []))
        if not isinstance(v, list):
            return [str(x) for x in default_rules.get(key, [])]
        return [str(x) for x in v if str(x).strip()]

    def _rule_int(key: str, fallback: int = 0) -> int:
        default = int(default_rules.get(key, fallback))
        try:
            return int(rules_data.get(key, default))
        except Exception:
            return default

    return WikiStrategyConfig(
        vault_path=vault_path,
        raw_path=raw_path,
        wiki_path=wiki_path,
        processed_path=processed_path,
        raw_subdirs=raw_subdirs,
        wiki_subdirs=wiki_subdirs,
        raw_to_wiki_map=raw_to_wiki_map,
        synonyms_path=synonyms_path,
        business_terms_path=business_terms_path,
        split_mode=str(wiki_data.get("split_mode", "heading")),
        heading_level=int(wiki_data.get("heading_level", 2)),
        wiki_compile_on_sync=bool(wiki_data.get("wiki_compile_on_sync", True)),
        style_guidelines=wiki_data.get("style_guidelines", {}),
        concept_cues=_rule_list("concept_cues"),
        comparison_hints=_rule_list("comparison_hints"),
        entity_org_suffixes=_rule_list("entity_org_suffixes"),
        entity_type_hints=_rule_list("entity_type_hints"),
        entity_exclude_terms=_rule_list("entity_exclude_terms"),
        entity_content_cues=_rule_list("entity_content_cues"),
        entity_ignore_terms=_rule_list("entity_ignore_terms"),
        entity_card_min_mentions=max(1, _rule_int("entity_card_min_mentions", 2)),
        entity_card_max_pages=max(1, _rule_int("entity_card_max_pages", 200)),
        entity_card_name_max_len=max(8, _rule_int("entity_card_name_max_len", 40)),
        chapter_title_patterns=_rule_list("chapter_title_patterns"),
        chapter_exact_terms=_rule_list("chapter_exact_terms"),
        tag_stopwords=_rule_list("tag_stopwords"),
        tag_block_patterns=_rule_list("tag_block_patterns"),
        tag_block_prefixes=_rule_list("tag_block_prefixes"),
        tag_min_len=max(1, _rule_int("tag_min_len", 2)),
        tag_max_len=max(2, _rule_int("tag_max_len", 20)),
        # --- 解析 RAG 策略配置 ---
        rag_retrieval_fanout=max(1, _rule_int("rag_retrieval_fanout", 12)),
        rag_rewrite_priority=str(rules_data.get("rag_rewrite_priority", default_rules.get("rag_rewrite_priority", "append"))),
        rag_core_boost_score=float(rules_data.get("rag_core_boost_score", default_rules.get("rag_core_boost_score", 1000.0))),
        rag_link_follow_limit=max(0, _rule_int("rag_link_follow_limit", 3)),
        rag_context_max_chars=max(100, _rule_int("rag_context_max_chars", 2400)),
    )



def ensure_workspace(config: AppConfig | None = None) -> None:
    cfg = config
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:
            cfg = None

    dirs = [PROJECT_ROOT / "logs", PROJECT_ROOT / "data" / "dictionaries", PROJECT_ROOT / ".wikicoder"]
    if cfg is not None:
        ws = cfg.wiki_strategy
        dirs.extend([ws.raw_path, ws.wiki_path, ws.processed_path, ws.processed_path / "chunks"])
        dirs.extend([ws.raw_path / d for d in ws.raw_subdirs])
        dirs.extend([ws.wiki_path / d for d in ws.wiki_subdirs])
    else:
        dirs.extend([
            PROJECT_ROOT / "data" / "raw",
            PROJECT_ROOT / "data" / "wiki",
            PROJECT_ROOT / "data" / "wiki_processed",
            PROJECT_ROOT / "data" / "wiki_processed" / "chunks",
        ])

    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)



def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    llm_data = data.get("llm", {})
    wiki_data = data.get("wiki_strategy", {})
    sync_data = data.get("sync", {})

    cfg = AppConfig(
        llm=LLMConfig(
            provider=str(llm_data.get("provider", "jiutian")),
            model=str(llm_data.get("model", "jiutian-think-v3")),
            api_key=_read_api_key(llm_data),
            base_url=llm_data.get("base_url"),
            image_asset_host=llm_data.get("image_asset_host"),
            image_understand_model=llm_data.get("image_understand_model"),
            image_generate_model=llm_data.get("image_generate_model"),
            image_understand_url=llm_data.get("image_understand_url"),
            image_generate_url=llm_data.get("image_generate_url"),
            temperature=float(llm_data.get("temperature", 0.2)),
            timeout_seconds=int(llm_data.get("timeout_seconds", 45)),
        ),
        wiki_strategy=_build_wiki_strategy(wiki_data),
        sync=SyncConfig(auto_on_startup=bool(sync_data.get("auto_on_startup", True))),
    )

    # runtime db path follows processed_path
    try:
        from src.utils.db_manager import configure_db_path

        configure_db_path(cfg.wiki_strategy.processed_path / "db.sqlite")
    except Exception:
        pass

    return cfg
