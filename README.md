# WikiCoder

Wiki-first 知识问答/代码助手：先检索本地 Wiki，未命中再回退通用大模型。

## 🚀 快速安装

WikiCoder 现已支持 Windows 和 UOS/Linux 的一键安装。

### Windows (PowerShell)
在项目根目录下，以管理员权限或普通权限运行：
```powershell
.\install_win.bat
```
*注：脚本会自动配置虚拟环境、安装依赖并注册全局 `wikicoder` 命令。*

### UOS / Linux (Bash)
在终端执行以下命令：
```bash
bash install_uos.sh
```
*安装完成后，根据提示运行 `source ~/.bashrc` 使命令生效。*

### 启动项目
安装完成后，在任意目录下直接输入：
```bash
wikicoder
```
即可进入交互式问答模式。

## 核心配置

编辑 `.wikicoder/config.yaml`（可参考 `.wikicoder/config.example.yaml`）：

```yaml
llm:
  provider: "jiutian"
  model: "jiutian-think-v3"
  api_key: "YOUR_JIUTIAN_API_KEY"

wiki_strategy:
  vault_path: "D:/my-vault"
  raw_dir: "raw"
  wiki_dir: "wiki"
  processed_dir: "wiki_processed"

  raw_subdirs: ["终端", "技术", "项目", "学习"]
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

设置 `vault_path` 后，程序会自动创建：

- `<vault>/raw`
- `<vault>/wiki`
- `<vault>/wiki_processed`

并按配置创建子目录。

## 常用命令（CLI）

```bash
wikicoderctl vaultpath "D:/my-vault"
wikicoderctl sync
wikicoderctl chat --trace --stream
wikicoderctl ask "废旧终端如何定义" --trace
wikicoderctl kbclear --yes
wikicoderctl kbclear --all --yes
wikicoderctl eval-retrieval --cases data/eval/retrieval_cases_zh.jsonl --topk 8 --out data/eval/reports/latest.json
wikicoderctl regress --cases data/eval/retrieval_cases_zh.jsonl --topk 8 --out data/eval/reports/latest.json
wikicoderctl compare-eval --base data/eval/reports/baseline.json --current data/eval/reports/latest.json
wikicoderctl set-baseline --source data/eval/reports/latest.json --target data/eval/reports/baseline.json
```

## REPL 命令

- `/help`
- `/sync`
- `/vaultpath <目录>`
- `/kbclear yes`
- `/kbclear all yes`
- `/mode auto|wiki_only|general_only`
- `/eval <cases.jsonl> [topk] [out.json]`
- `/regress <cases.jsonl> [topk] [out.json]`
- `/compare <baseline.json> <latest.json>`
- `/baseline <report.json> [baseline.json]`
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
- `/reset`
- `/exit`

## 评测说明

- 评测样例：`data/eval/retrieval_cases.jsonl`、`data/eval/retrieval_cases_zh.jsonl`
- 指标：`recall@k`、`top1_accuracy`、`mrr`
- 报告包含 miss 查询与明细 rank，便于回归对比。
