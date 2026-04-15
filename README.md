# WikiCoder

支持 Wiki 优先检索 + 九天模型（内置）+ CLI 对话 + 代码补丁建议/应用/回滚。

## 安装

```bash
pip install -r requirements.txt
pip install -e .
```

安装后可直接一条命令启动 REPL：

```bash
wikicoder
```

可选：使用管理命令入口（等价于 `python -m src.main ...`）：

```bash
wikicoderctl --help
```

## 配置（九天内置）

编辑 `.wikicoder/config.yaml`：

```yaml
llm:
  provider: "jiutian"
  model: "jiutian-think-v3"
  api_key: "YOUR_JIUTIAN_API_KEY"
  base_url: null  # 留空即可，内置默认 chat/completions 地址

  image_understand_model: null
  image_generate_model: null
  image_understand_url: null
  image_generate_url: null

  temperature: 0.2
  timeout_seconds: 45

wiki_strategy:
  raw_path: "./data/raw"
```

- 文本默认 POST 地址：
  `https://jiutian.10086.cn/largemodel/moma/api/v3/chat/completions`
- 同时支持 OpenAI 兼容方式（与九天官方示例一致）：
  `base_url = https://jiutian.10086.cn/largemodel/moma/api/v3`
- 只填 `api_key` 即可先使用文本对话。
- 图片理解/图片生成可按九天文档补充模型名与 URL（API Key 与文本共用）。

可选环境变量：

- `JIUTIAN_API_KEY`
- `OPENAI_API_KEY`
- `GOOGLE_API_KEY` / `GEMINI_API_KEY`

## 知识库路径

在 `.wikicoder/config.yaml` 修改：

```yaml
wiki_strategy:
  raw_path: "你的Markdown知识库目录"
```

然后执行同步：

```bash
python -m src.main sync
```

## 常用命令

```bash
python -m src.main chat --trace --stream
python -m src.main kbclear --yes
python -m src.main kbpath "D:/mywiki"
python -m src.main ask "你好" --trace

python -m src.main image-understand --image-url "https://example.com/a.png" --query "描述图片内容"
python -m src.main image-generate "一个蓝色机器人在写代码" --save-dir data/generated_images --prefix demo

python -m src.main review src/main.py "这个文件有哪些可改进点？" --trace
python -m src.main patch src/main.py "按PEP8重构并减少重复代码" --trace --apply --yes
python -m src.main patch-multi "src/main.py,src/core/agent.py" "统一日志与异常处理" --trace

python -m src.main backups
python -m src.main undo <backup_id>
```

### 图片生成结果保存行为

`image-generate` 会自动：
- 提取并显示返回中的图片 URL（若有）
- 提取 base64 图片并保存到本地 png（若有）
- 保存完整原始响应 JSON 到本地（便于排查）

## REPL 内命令

输入 `/` 会自动弹出可选命令补全列表（可上下键选择、Tab 确认）。

- `/help`
- `/sync`
- `/kbclear yes`（一键清空索引）
- `/kbpath <目录>`（设置知识库 RAW 路径）
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

## 行为说明

- Wiki 命中：按 Wiki 规范优先回答。
- Wiki 未命中：自动回退到通用大模型对话。
