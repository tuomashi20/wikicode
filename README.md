# WikiCoder

支持：Wiki 优先检索、九天模型接入、CLI 交互、补丁建议/应用/回滚。

## 安装

```bash
pip install -r requirements.txt
pip install -e .
```

启动 REPL：

```bash
wikicoder
```

## 核心配置

编辑 `.wikicoder/config.yaml`（可参考 `.wikicoder/config.example.yaml`）：

```yaml
llm:
  provider: "jiutian"
  model: "jiutian-think-v3"
  api_key: "YOUR_JIUTIAN_API_KEY"
  base_url: null

wiki_strategy:
  vault_path: "D:/my-vault"
  raw_dir: "raw"
  wiki_dir: "wiki"
  processed_dir: "wiki_processed"

  # 自动创建细分子目录（存在则跳过）
  raw_subdirs: ["inbox", "drafts", "archive"]
  wiki_subdirs: ["entities", "concepts", "comparisons", "queries"]
  raw_to_wiki_map:
    "终端": "entities"
    "技术": "concepts"
    "项目": "comparisons"
    "学习": "queries"
  wiki_compile_on_sync: true

  synonyms_path: "./data/dictionaries/synonyms_zh.yaml"
  split_mode: "heading"
  heading_level: 2
```

> 设置 `vault_path` 后，程序会自动派生并创建：
> - `<vault>/raw`
> - `<vault>/wiki`
> - `<vault>/wiki_processed`
> 以及你配置的 `raw_subdirs/wiki_subdirs`。
> 同步时会自动在 `wiki/` 下生成可读页面、文件索引页、标签页与 `index.md`，便于 Obsidian 图谱查看。
> 若配置了 `raw_to_wiki_map`，会按 RAW 子目录自动映射到 wiki 分类目录。

## 常用命令

```bash
wikicoderctl vaultpath "D:/my-vault"
wikicoderctl sync
wikicoderctl chat --trace --stream
wikicoderctl ask "废旧终端如何定义？" --trace
wikicoderctl kbclear --yes
```

## REPL 命令

- `/help`
- `/sync`
- `/vaultpath <目录>`（统一设置知识库根目录）
- `/kbclear yes`（清空索引）
- `/ask <query>`
- `/review <file> :: <query>`
- `/patch <file> :: <query>`
- `/patchm <file1,file2> :: <query>`
- `/preview`
- `/apply yes`
- `/backups`
- `/undo [backup_id]`
- `/structure`
- `/trace on|off`
- `/stream on|off`
- `/exit`
