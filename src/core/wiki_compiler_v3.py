import os
from pathlib import Path
from typing import Any, Dict, List
from src.utils.config import AppConfig
from src.core.knowledge_extractor import KnowledgeExtractor
from src.core.wiki_generator import WikiGenerator
import json
import hashlib
from src.utils.db_manager import get_conn

class WikiCompilerV3:
    """原子模块 3：协调器 - 负责全量编译流水线调度"""

    def __init__(self, config: AppConfig, llm_client: Any = None, on_status: Any = None):
        self.config = config
        
        # [WikiCoder 路径锁] 严格引用配置对象中已解析的绝对路径
        ws = getattr(config, "wiki_strategy", config)
        
        # 核心路径直连：不再手动拼接，防止因属性缺失导致路径坍缩
        self.vault_path = Path(getattr(ws, "vault_path", "."))
        self.wiki_dir = getattr(ws, "wiki_path", self.vault_path / "wiki")
        self.processed_path = getattr(ws, "processed_path", self.vault_path / "wiki_processed")
        
        # 额外的安全审计路径（防止 .wikicoder 被误伤）
        self.meta_path = self.vault_path / ".wikicoder"
        
        self.on_status = on_status
        
        # 组装原子组件
        self.extractor = KnowledgeExtractor(llm_client)
        self.generator = WikiGenerator(self.wiki_dir)

    def compile_all(self):
        """[指令落地] 执行全量重建与编译 (5路并发版)"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        chunks = self._fetch_all_chunks()
        total = len(chunks)
        if not chunks:
            msg = "[WikiCoder] 警告：数据库中无切片，请先运行 sync"
            if self.on_status: self.on_status(msg)
            print(msg)
            return

        # 知识聚合池
        knowledge_pool = {}
        
        # [存档点] 建立提炼结果缓存目录
        cache_dir = self.processed_path / "cache" / "facts"
        cache_dir.mkdir(parents=True, exist_ok=True)

        def _process_chunk(idx, row):
            """单切片提炼原子任务"""
            cid = row['chunk_id']
            content = row['content_text'] or ""
            content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:12]
            cache_file = cache_dir / f"{cid}_{content_hash}.json"

            # 1. 尝试缓存命中
            if cache_file.exists():
                try:
                    return json.loads(cache_file.read_text(encoding="utf-8"))
                except Exception: pass

            # 2. 缓存缺失，请求 AI
            facts = self.extractor.extract(content)
            
            # 3. 写入缓存并注入溯源信息
            for f in facts:
                f["source"] = row["parent_file"]
                f["anchor"] = row["breadcrumb"]
            
            try:
                cache_file.write_text(json.dumps(facts, ensure_ascii=False), encoding="utf-8")
            except Exception: pass
            
            return facts

        # [核心] 启动 8 路并发执行器
        max_workers = 8
        if self.on_status: self.on_status(f"🚀 启动 WikiCoder 多车道加速模式 (并发数: {max_workers})")
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # [修正] 将 future 映射到对应的 row 对象，确保溯源准确
            future_to_chunk = {executor.submit(_process_chunk, i, row): row for i, row in enumerate(chunks)}
            
            completed_count = 0
            for future in as_completed(future_to_chunk):
                row = future_to_chunk[future]
                completed_count += 1
                try:
                    facts = future.result()
                    # 汇总结果
                    for fact in facts:
                        # [WikiCoder 标准化] 统一字段名与补全元数据
                        s_fact = self._standardize_fact(fact)
                        s_fact["source"] = row["parent_file"]
                        s_fact["anchor"] = row["breadcrumb"]

                        category = s_fact.get("category", "concepts")
                        subject = s_fact.get("name")
                        
                        key = (category, subject)
                        if key not in knowledge_pool:
                            knowledge_pool[key] = {"definitions": [], "responsibilities": [], "inferences": []}
                        knowledge_pool[key]["definitions"].append(s_fact)
                    
                    # 实时进度反馈
                    if completed_count % max_workers == 0 or completed_count == total:
                        prog_msg = f"[WikiCoder] 加速同步中: [{completed_count}/{total}] (并发状态正常)"
                        if self.on_status: self.on_status(prog_msg)
                        print(prog_msg)
                except Exception as e:
                    print(f"  [Error] 切片提炼失败: {str(e)}")

        # 3. 页面生成
        if self.on_status: self.on_status(f"[WikiCoder] 提炼完成，正在生成 Wiki 页面...")
        for (cat, name), data in knowledge_pool.items():
            self.generator.render_page(cat, name, data)

        final_msg = f"✅ WikiCoder 编译完成！共提炼 {total} 个切片，生成 {len(knowledge_pool)} 个知识资产页。"
        if self.on_status: self.on_status(final_msg)
        print(final_msg)

    def _clean_rebuild(self):
        """[加固版] 清理 Wiki 编译产物，严禁触碰 Raw 数据与元数据"""
        import shutil
        from src.utils.db_manager import clear_index_store
        
        # 1. 安全校验：严禁删除根目录
        if self.wiki_dir == self.vault_path or self.wiki_dir == self.vault_path.parent:
            if self.on_status: self.on_status("[❌] 安全熔断：检测到清理路径与根目录重合，已拦截删除动作！")
            return

        # 2. 深度清理索引库 (DB + Chunks + SyncState)
        proc_path = self.processed_path
        if self.on_status: self.on_status("正在执行数据库与索引全量静默清理...")
        clear_index_store(processed_path=proc_path)

        # 3. 仅清理 Wiki 页面输出目录 (熟食区)
        if self.wiki_dir.exists():
            if self.on_status: self.on_status(f"正在清理 Wiki 页面输出目录: {self.wiki_dir}")
            shutil.rmtree(self.wiki_dir, ignore_errors=True)
        self.wiki_dir.mkdir(parents=True, exist_ok=True)

    def _standardize_fact(self, fact: Any) -> Dict[str, Any]:
        """[WikiCoder 复刻] 事实标准化器 - 负责别名映射与字段对齐"""
        if not isinstance(fact, dict):
            return {}
        
        # 1. 名称/主体标准化 (entity -> name)
        name = fact.get("name") or fact.get("entity") or fact.get("subject") or "Unknown"
        
        # 2. 描述/上下文标准化 (context/content -> summary)
        summary = fact.get("summary") or fact.get("context") or fact.get("content") or ""
        
        # 3. 动作/关系标准化 (predicate -> action)
        action = fact.get("action") or fact.get("predicate") or ""

        # 4. 类型对齐
        f_type = fact.get("type") or fact.get("category") or "concepts"

        return {
            "name": name,
            "subject": name,  # 兼容职责矩阵
            "summary": summary,
            "content": summary, # 兼容推理模块
            "action": action,
            "category": f_type,
            "source": fact.get("source", "Unknown"),
            "anchor": fact.get("anchor", "")
        }

    def _fetch_all_chunks(self):
        with get_conn() as conn:
            return conn.execute(
                "SELECT chunk_id, title, content_text, parent_file, breadcrumb FROM chunks"
            ).fetchall()

