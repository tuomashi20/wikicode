WikiCoder CLI 详细设计方案 (Vibe Coding 最终版)
1. 项目核心愿景
构建一个类似 Claude Code 的开发工具，通过“原子化知识检索”而非“语义 RAG”，使 LLM 能够精准遵循企业私有规范、Wiki 文档进行代码编写与协同。

2. 项目路径规划 (Path Architecture)
Plaintext
wikicoder/
├── .wikicoder/              # 全局配置层
│   └── config.yaml          # LLM 参数、RAW 路径、原子化策略
├── src/                     # 源代码层
│   ├── main.py              # CLI 入口 (Typer) & REPL 交互循环
│   ├── core/
│   │   ├── agent.py         # ReAct 决策引擎 (Tool Use 调度)
│   │   └── atomizer.py      # RAW -> WIKI 处理器 (原子化逻辑)
│   ├── skills/              # Hermes-Agent 原子化工具集
│   │   ├── wiki_tools.py    # search_wiki, read_chunk, list_categories
│   │   └── code_tools.py    # read_file, write_file, patch_apply
│   └── utils/
│       ├── db_manager.py    # SQLite 索引维护 (确定性检索)
│       └── logger.py        # 日志系统
├── data/                    # 数据持久化层
│   ├── raw/                 # [RAW 层] 指向用户原始 Markdown 仓库的软链接
│   └── wiki_processed/      # [WIKI 层]
│       ├── db.sqlite        # 元数据索引 (非向量库)
│       └── chunks/          # 原子化切片目录 (.md)
└── logs/                    # 运维层
    ├── sync.log             # 原子化同步日志
    └── session.log          # LLM 思考与 Tool 调用轨迹日志
3. 核心功能设计
3.1 配置文件 (config.yaml)
YAML
llm:
  provider: "google_api_studio"  # 或 openai, ollama
  model: "gemini-2.0-flash"
  api_key: "YOUR_KEY"

wiki_strategy:
  raw_path: "~/MyWork/Wiki"      # 原始文档存放处
  split_mode: "heading"          # 按标题拆分
  heading_level: 2               # 拆分深度
  style_guidelines:              # 注入 LLM 的感知规则
    priority: ["MUST", "REQUIRED"]
    language: "Chinese"

sync:
  auto_on_startup: true          # 每次启动 chat 是否自动同步
3.2 RAW -> WIKI 原子化流水线
RAW 层：保留用户的原始写作习惯，支持长文档、双链。

处理过程：atomizer.py 扫描 RAW，按二级标题切分，剔除多余媒体元素，保留文本。

WIKI 层：

chunks/：存放如 wc_auth_logic_01.md 的短小精悍的规范片段。

db.sqlite：存储 chunk_id, title, parent_file, tags, last_modified。

3.3 交互对话模式 (Claude-like REPL)
技术实现：使用 python-prompt-toolkit 维持长连接对话。

沉浸体验：

支持 /sync 触发原子化更新。

支持 /ask 强制进入 Wiki 检索模式。

流式渲染：使用 rich.live 实现代码块高亮实时输出。

4. Hermes-Agent 原子化工具 (The Skills)
为了让大模型能像操作文件一样操作 Wiki，需定义以下工具：

wiki_search(query):

逻辑：在 db.sqlite 中执行 SELECT ... WHERE title LIKE %query%。

返回：匹配的原子块 ID 和标题。

wiki_read_chunk(chunk_id):

逻辑：直接从 chunks/ 目录读取对应的 .md 文件。

返回：完整的 Markdown 规范文本。

wiki_list_structure():

逻辑：根据 SQLite 中的 raw_file_path 进行分组。

返回：Wiki 的目录大纲，让模型了解有哪些规范分类。

5. 通信工作流 (Protocol)
每次用户在窗口输入提示词，Agent 的内部逻辑如下：

分析意图：判断是否涉及技术实现或业务规范。

强制检索：即使意图不明显，System Prompt 也会驱动 Agent 先执行 wiki_search。

阅读规范：从 WIKI 层加载原子片段。

对比执行：对比当前工程目录下的代码（code_tools）与 WIKI 规范，生成 Diff 或建议。

记录轨迹：将整个 Thought -> Action 过程记录在 logs/session.log。

6. Vibe Coding 开发指令 (Prompt Sequence)
你可以将以下指令依次喂给你的 AI 编辑器：

Step 1 (Infrastructure): "实现 .wikicoder/config.yaml 的加载逻辑，并建立 logs/ 和 data/ 的初始化脚本。"

Step 2 (Atomizer & DB): "编写 atomizer.py。要求：读取 raw_path 下的 Markdown，按二级标题拆分并存入 data/wiki_processed/chunks，同时将元数据存入 db.sqlite。记录同步日志到 logs/sync.log。"

Step 3 (Tools & Agent): "使用 LangChain 封装 wiki_tools.py。实现 wiki_search (SQL 检索) 和 wiki_read_chunk。在 System Prompt 中加入 Hermes-Agent 风格的‘Wiki 优先’指令。"

Step 4 (REPL): "使用 python-prompt-toolkit 和 rich 实现 main.py 的对话循环。支持流式输出和斜杠命令 /sync。"

设计亮点总结：
非向量化：通过 SQLite 关键词和层级实现 100% 精准的“翻书式”检索。

原子化缓存：WIKI 层极大减小了 LLM 的上下文压力，提高了响应速度。

完全透明：通过 logs/ 随时审计 LLM 到底看了哪条规范。