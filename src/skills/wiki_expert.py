from datetime import datetime
from typing import List, Any, Dict, Optional, Callable
from pathlib import Path
import json

class WikiExpert:
    """[WikiCoder 业务专家] 负责规约对齐、合规审计与结果合成"""
    
    def __init__(self, config: Any, llm_client: Any):
        self.config = config
        self.llm = llm_client
        
        # [WikiCoder 路径纠正] 兼容对象与字典，严格拼接
        strategy = getattr(config, "wiki_strategy", None)
        if strategy is None:
            if hasattr(config, "get"):
                strategy = config.get("wiki_strategy", {})
            else:
                strategy = {}
        
        v_path = getattr(strategy, "vault_path", "wiki") if hasattr(strategy, "vault_path") else (strategy.get("vault_path", "wiki") if isinstance(strategy, dict) else "wiki")
        w_dir = getattr(strategy, "wiki_dir", "") if hasattr(strategy, "wiki_dir") else (strategy.get("wiki_dir", "") if isinstance(strategy, dict) else "")
        self.wiki_path = Path(v_path) / w_dir
        
        self.raw_path = Path(getattr(strategy, "raw_dir", "raw")) if hasattr(strategy, "raw_dir") else Path(strategy.get("raw_dir", "raw") if isinstance(strategy, dict) else "raw")
        
        # --- [关键修复]：强行对齐底层数据库路径 ---
        processed_dir_name = getattr(strategy, "processed_dir", "wiki_processed") if hasattr(strategy, "processed_dir") else (strategy.get("processed_dir", "wiki_processed") if isinstance(strategy, dict) else "wiki_processed")
        db_file_path = self.wiki_path / processed_dir_name / "db.sqlite"
        try:
            from src.utils.db_manager import configure_db_path
            configure_db_path(db_file_path)
            # print(f"[DEBUG] 数据库路径已重定向至: {db_file_path}")
        except Exception as e:
            print(f"[!] 重定向数据库路径失败: {e}")
        # ----------------------------------------

        self.meta_path = self.wiki_path / ".wikicoder"
        self.meta_path.mkdir(parents=True, exist_ok=True)
        
        self.schema_path = self.meta_path / "schema.md"
        self.index_path = self.meta_path / "index.md"
        self.log_path = self.meta_path / "wiki.log"
        self.profile_path = self.wiki_path / "personal_profile.md"
        self._ensure_infrastructure()

    def _ensure_infrastructure(self):
        """初始化 Wiki 治理基础设施"""
        if not self.schema_path.exists():
            self.schema_path.write_text("# Wiki Schema\n\n## Domain\n安徽移动业务审计\n\n## Conventions\n- 结算标准必须包含具体金额、有效期。\n", encoding="utf-8")
        if not self.index_path.exists():
            self.index_path.write_text("# Wiki Index\n", encoding="utf-8")
        if not self.log_path.exists():
            self.log_path.write_text("# Wiki Log\n\n", encoding="utf-8")
        
        # [V2.7 强化] 确保个人配置文件存在，作为业务补丁的最高优先级来源
        if not self.profile_path.exists():
            self.profile_path.write_text(
                "# Personal Profile & Business Logic Cache\n\n"
                "## 1. 用户自定义规则 (Override Rules)\n"
                "<!-- 此处记录的规则优先级高于任何正式规约文件。例如：'老板说XX标准改为3年' -->\n\n"
                "## 2. 跨领域黑话与专业术语 (Glossary)\n"
                "<!-- 记录您的习惯用语，如：'宽带猫' -> '家庭业务光猫终端' -->\n\n"
                "## 3. 审计偏好与特定记忆 (Audit Preferences)\n"
                "<!-- 记录您在历次会话中要求 AI 记住的特定结论 -->\n", 
                encoding="utf-8"
            )

    def sync(self, on_status=None) -> dict:
        """[运维] 同步 Wiki 知识库并执行 WikiCoder 编译"""
        # [安检] 确保知识库路径已显式配置
        if not self.config.wiki_strategy.vault_path:
            return {"error": "❌ 知识库路径未设置！请先使用指令: /kbpath <路径>"}

        try:
            from src.core.atomizer import Atomizer
            from src.core.wiki_compiler_v3 import WikiCompilerV3
            
            # 0. [核心修正] 在一切开始前执行“强力扫除”
            if on_status: on_status("正在执行全库深度大扫除...")
            compiler = WikiCompilerV3(self.config, self.llm, on_status)
            compiler._clean_rebuild()

            # 1. 基础同步 (Raw -> Chunks)
            if on_status: on_status("正在重新执行原子切片 (Atomizing)...")
            atomizer = Atomizer(self.config)
            result = atomizer.sync()
            
            # 2. 深度编译 (Chunks -> Wiki)
            compiler.compile_all()
            
            self.auto_index()
            return result
        except Exception as e: 
            return {"error": str(e)}

    def auto_index(self) -> str:
        """扫描并构建索引视图"""
        return "索引已更新。"

    def orient(self) -> str:
        """快速导航摘要"""
        schema = self.schema_path.read_text(encoding="utf-8")[:4000]
        return f"【Wiki 核心规约】\n{schema}\n"

    def _load_report_template(self, query: str, override_template: str = None) -> tuple[Optional[str], str, str]:
        """多路径尝试加载模板，支持实时配置热加载与参数透传覆盖"""
        # 1. 默认无模板 (自由发挥模式)
        template_name = None
        
        # 2. 优先从配置文件加载基准配置
        try:
            from src.utils.config import load_config
            dynamic_config = load_config()
            if hasattr(dynamic_config, "wiki_strategy"):
                ws = dynamic_config.wiki_strategy
                template_name = getattr(ws, "report_template", None)
            else:
                ws = dynamic_config.get("wiki_strategy", {})
                template_name = ws.get("report_template", None)
        except Exception as e:
            pass # 允许无配置运行

        # 3. 决定最终使用的模板名称 (优先级：参数 > 配置)
        if override_template:
            template_name = override_template

        # 4. 如果最终没有确定模板，则直接返回 None 启用自由发挥模式
        if not template_name:
            return None, "Free Style", "No template configured"

        # 5. 执行文件加载
        try:
            # 锁定在代码库路径：src/templates/reports/
            project_root = Path(__file__).parent.parent.parent
            tp = project_root / "src" / "templates" / "reports" / template_name
            
            if tp.exists():
                content = tp.read_text(encoding="utf-8")
                now = datetime.now()
                content = content.replace("{query}", query)
                content = content.replace("{timestamp}", now.strftime("%Y-%m-%d %H:%M:%S"))
                content = content.replace("{timestamp:%Y}", now.strftime("%Y"))
                content = content.replace("{timestamp:%m}", now.strftime("%m"))
                content = content.replace("{timestamp:%d}", now.strftime("%d"))
                content = content.replace("{timestamp:%Y年%m月%d日}", now.strftime("%Y年%m月%d日"))
                return content, template_name, str(tp)
            
            return None, template_name, f"未在代码库中找到模板文件: {tp}"
        except Exception as e:
            return None, template_name or "Error", f"加载异常: {str(e)}"

    def synthesize(self, query: str, observations: List[str], mode: str = "plan", on_token=None, **kwargs) -> str:
        """[WikiCoder 级结果合成器] 对接标准化事实，生成高可靠性报告开"""
        from src.utils.config import load_config
        config = load_config()
        
        report_template = kwargs.get("report_template")
        template_content, t_name, d_path = self._load_report_template(query, override_template=report_template)
        
        agent_context = kwargs.get("context", "")
        
        # [WikiCoder 动态优化] 从配置中读取合成指令
        base_sys_prompt = ""
        try:
            strategy = getattr(config, "wiki_strategy", None)
            if strategy:
                prompts = getattr(strategy, "prompts", {})
                if isinstance(prompts, dict):
                    base_sys_prompt = prompts.get("report_synthesis", "")
                else:
                    base_sys_prompt = getattr(prompts, "report_synthesis", "")
            
            # 兼容性备选：如果还是空的，尝试字典方式
            if not base_sys_prompt and hasattr(config, "get"):
                base_sys_prompt = config.get("wiki_strategy", {}).get("prompts", {}).get("report_synthesis", "")
        except:
            pass

        if not base_sys_prompt:
            base_sys_prompt = "你是一位专业的业务专家。请基于检索事实给出自然、客观的回答。"

        if template_content:
            # [WikiCoder 智能分流] 判断是否为非业务审计类的通用问题（如身份确认、常识、问候）
            # 如果是此类问题，强制卸载重型模板，防止输出“空壳模板”
            common_keywords = ["我是谁", "身份", "profile", "你好", "谁是", "介绍一下"]
            is_common_query = any(k in query.lower() for k in common_keywords)
            
            if is_common_query:
                template_content = None # 卸载模板，进入自由发挥
                t_name = "自由发挥 (通用问题识别)"
                
        if template_content:
            # [核心增强] 如果 Agent 在 summarize 时提供了具体草稿 (answer/code)，将其作为强制约束注入
            agent_draft = ""
            if "answer" in kwargs:
                agent_draft += f"\n--- Agent 预撰写的答案/脚本 ---\n{kwargs['answer']}\n"
            if "code" in kwargs:
                agent_draft += f"\n--- Agent 生成的代码片段 ---\n{kwargs['code']}\n"
                
            sys_prompt = (
                f"{base_sys_prompt}\n\n"
                f"### 必须遵循的报告模板 ###\n{template_content}\n"
                f"### Agent 提供的原始草稿 (请务必将其中的代码脚本完整填入模板对应区块) ###\n{agent_draft}\n"
            )
        else:
            sys_prompt = base_sys_prompt

        # 构造用户侧输入
        user_input = []
        if agent_context:
            user_input.append(f"【审计思维路径】:\n{agent_context}")
        
        user_input.append("【检索到的业务事实（WikiCoder 聚合）】:")
        user_input.extend(observations)
        
        user_prompt = "\n\n".join(user_input)

        if on_token:
            status = f"🚀 WikiCoder 引擎已加载模板: {t_name}" if template_content else "✨ 自由发挥模式"
            on_token(f"> [系统消息] {status}\n\n")
        
        # [核心修复] 使用正确的 (system, user) 分发逻辑
        full_res = ""
        try:
            for token in self.llm.generate_stream(sys_prompt, user_prompt):
                full_res += token
                if on_token: on_token(token)
        except Exception as e:
            err_msg = f"\n\n❌ 报告合成失败: {str(e)}"
            if on_token: on_token(err_msg)
            full_res += err_msg
            
        return full_res

    def _log_action(self, action: str, detail: str):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(f"## [{now_str}] {action} | {detail}\n")
