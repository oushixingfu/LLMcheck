# LLMcheck

LLMcheck 是一个面向中文医籍资料的文档清洗、校正、验收和交付工具。它支持 Markdown、PDF、图片和 Office 文件，将输入规范化为 Markdown 后，执行确定性清理、分块并发 LLM 校正、分块并发 LLM 验收、失败项局部修复和必要的 LLM 修复，最终产出可交付的 Markdown 与文字版 PDF。

当前仓库同时提供两个交付版本：

- GUI exe 版：`dist/LLMcheck-GUI-0.1.1.exe`
- Agent skill 版：`dist/llmcheck-skill-0.1.1.tar.gz`

GitHub 仓库地址创建后为：

```text
https://github.com/oushixingfu/LLMcheck
```

## 适用场景

- 将扫描版 PDF、图片、Office 文件或 Markdown 转成可读、可验收的 Markdown。
- 对中文医案、方剂、针灸、中医教材等文本做保守校正，减少 OCR 噪声和异常断行。
- 批量逐本处理 `/mnt/d/pdf` 一类书目目录，并保留可续跑的状态。
- 给其他 agent 提供可复用 skill，让 agent 按固定流程处理同类文档。
- 给非开发用户提供可双击启动的 Windows GUI。

LLMcheck 不做 Stage1 closure、difference-page rule convergence，也不做 MinerU/PPX 投票。PDF 的正式文本来源是 MinerU API `vlm`；本地 PPX 只作为 PDF 审计和修复参考。

## 输入流程

- `.md`：直接进入文本清理、LLM 校正和 LLM 验收。
- `.pdf`：并行启动本地 PPX 审计和 MinerU API。PDF 超过 `--pdf-page-chunk-size` 时按页切分，默认 30 页一段；MinerU 分段结果按页序合并后进入正式 LLM 流程。
- 图片：`.png`、`.jpg`、`.jpeg`、`.jp2`、`.webp`、`.gif`、`.bmp` 直接走 MinerU API。
- Office：`.doc`、`.docx`、`.ppt`、`.pptx`、`.xls`、`.xlsx` 直接走 MinerU API。

确定性清理包含：

- 将 MinerU 表格图片附近的 `flowchart` 详情转写为 Markdown 表格。
- 拆分常见结构粘连，例如 `颈部：...肩部：...肘部：...`。
- 拆分 `年版` 后的参考文献粘连。
- 验收发现 `layout`、`ocr_noise`、`punctuation` 或 `missing_text` 阻断项时，先做局部确定性修复，再决定是否升级到 LLM 修复。

## 快速使用

### Windows GUI exe

双击或从命令行启动：

```powershell
dist\LLMcheck-GUI-0.1.1.exe
```

默认会启动本地服务并打开浏览器。也可以显式指定端口：

```powershell
dist\LLMcheck-GUI-0.1.1.exe --host 127.0.0.1 --port 8766
```

无浏览器自动打开模式：

```powershell
dist\LLMcheck-GUI-0.1.1.exe --host 127.0.0.1 --port 8766 --no-browser
```

打开：

```text
http://127.0.0.1:8766
```

GUI 支持文件上传、文件夹上传、本机路径、输出目录、LLM API、MinerU API、PDF 拆分页数、MinerU 批量文件数、PPX 路径、本地 OCR 组合和各阶段并发上限。

### Agent skill

skill 压缩包：

```text
dist/llmcheck-skill-0.1.1.tar.gz
```

包内结构：

```text
skill/
  README.md
  SKILL.md
```

其他 agent 可以解压后将 `skill/` 安装到自己的 skills 目录，或直接读取 `skill/SKILL.md` 执行 LLMcheck 固定流程。

### Python CLI

本地开发环境运行：

```bash
uv run python -m llmcheck.cli --help
```

安装为命令后：

```bash
llmcheck --help
```

单文件或单目录处理：

```bash
llmcheck run \
  --input /path/to/file-or-directory \
  --output-dir /path/to/output \
  --llm-api-url http://127.0.0.1:3022 \
  --llm-api-key "$LLM_API_KEY" \
  --llm-model gpt-5.5 \
  --mineru-api-key "$MINERU_CLOUD_API_TOKEN" \
  --mineru-concurrency 12 \
  --mineru-batch-size 50 \
  --pdf-page-chunk-size 30 \
  --ppx-ocr auto \
  --llm-chunk-chars 2000 \
  --concurrency 10
```

逐本批处理目录：

```bash
llmcheck batch \
  --source-dir /mnt/d/pdf \
  --output-dir /mnt/d/pdf/output \
  --start-index 1 \
  --limit 1 \
  --book-concurrency 1 \
  --llm-api-url http://127.0.0.1:3022 \
  --llm-api-key "$LLM_API_KEY" \
  --llm-model gpt-5.5 \
  --mineru-api-key "$MINERU_CLOUD_API_TOKEN" \
  --mineru-concurrency 50 \
  --mineru-batch-size 50 \
  --mineru-timeout-seconds 14400 \
  --pdf-page-chunk-size 30 \
  --ppx-ocr auto \
  --llm-chunk-chars 2000 \
  --concurrency 32
```

查看单本 MinerU 进度：

```bash
llmcheck mineru-status \
  --book-output-dir /mnt/d/pdf/output/process/books/0001_示例书 \
  --book-name 示例书.pdf
```

## 输出目录

`--output-dir` 下固定只保留交付目录和过程目录：

```text
output/
  md/
  文字版pdf/
  process/
```

含义：

- `md/*.md`：通过验收的最终 Markdown。
- `文字版pdf/*.pdf`：通过验收的文字版 PDF。
- `process/drafts/*.md`：校正草稿，包含未通过最终验收的候选文本。
- `process/preprocess/*`：MinerU 正式 Markdown、PPX 审计 Markdown、PDF 分段、zip 和预处理 manifest。
- `process/reports/*.quality.json`：确定性质量报告。
- `process/reports/*.llm_correction.json`：LLM 校正报告。
- `process/reports/*.llm_acceptance.json`：LLM 验收报告。
- `process/reports/*.local_repair.json`：本地修复报告。
- `process/reports/*.llm_repair.json` 与 `process/reports/*.llm_repair_chunks/*.json`：LLM 修复报告。
- `process/reports/llmcheck_manifest.jsonl`：单任务逐文档结果。
- `process/reports/llmcheck_summary.json`：单任务汇总。
- `process/books/<index>_<book>/*`：批处理时每本书的完整过程目录。
- `process/llmcheck_batch_state.jsonl`：批处理增量状态。
- `process/llmcheck_batch_summary.json`：批处理汇总。

## 批处理续跑语义

- `--start-index` 保留原始书号；例如 `--start-index 6 --limit 1` 会按第 6 本汇报。
- 已通过书目只有在 summary、最终 Markdown、文字版 PDF 都能验收时才会跳过。
- 空目录或超出范围的 `--start-index` 不会被算作通过。
- 多次 `--limit 1` 逐本运行会追加 `process/llmcheck_batch_state.jsonl`，并在 `process/llmcheck_batch_summary.json` 中按书目合并最新状态。
- 单本开始时会先写入 `status: "in_progress"`，长时间等待 MinerU 云端 pending 时也能从 state/summary 看出当前书名和输出目录。
- `llmcheck batch` 会把逐本进度事件以 JSONL 写到 stderr；最终汇总 JSON 写到 stdout。

## 环境要求

Python CLI 和源码运行：

- Python 3.12+
- `fastapi`
- `uvicorn[standard]`
- OpenAI-compatible LLM API endpoint、API key、model
- MinerU API token，用于 PDF、图片和 Office 输入
- 本地 PPX，仅用于 PDF 审计
- `pdfinfo`，用于 PDF 页数统计
- `qpdf`，用于大 PDF 分段

Windows GUI exe：

- Windows 10/11
- 不需要预装 Python
- 仍需要可访问的 LLM API、MinerU API 和本地 PPX 路径

敏感信息不要写入仓库。`.env` 和 `.env.*` 已被 `.gitignore` 排除。

## 构建与复现

运行测试：

```bash
uv run pytest tests/test_llmcheck.py -q
```

重新打包 skill：

```bash
tar -czf dist/llmcheck-skill-0.1.1.tar.gz skill
```

在 Windows 侧重新构建 GUI exe：

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_windows_gui_exe.ps1
```

整理历史输出目录：

```bash
uv run python tools/organize_pdf_output.py --root /mnt/d/pdf/output
uv run python tools/organize_pdf_output.py --root /mnt/d/pdf/output --apply
```

第一条是 dry-run，第二条才实际整理。

## 项目结构

```text
llmcheck/
  batch.py          # 逐本批处理、续跑、批处理 summary/state
  cli.py            # CLI 入口
  diagnostics.py    # 单本输出和 MinerU 状态诊断
  gui.py            # FastAPI GUI
  gui_exe.py        # Windows exe 启动入口
  llm.py            # LLM prompt 和调用
  pdf.py            # 文字版 PDF 写出
  pipeline.py       # 单任务主流程
  preprocess.py     # MinerU/PPX/输入规范化
  quality.py        # 确定性清理和本地修复
skill/
  SKILL.md          # agent-facing skill
  README.md         # skill 包说明
tools/
  build_windows_gui_exe.ps1
  organize_pdf_output.py
tests/
  test_llmcheck.py
dist/
  LLMcheck-GUI-0.1.1.exe
  llmcheck-skill-0.1.1.tar.gz
```

## 验收记录

当前交付验收已完成：

- `uv run pytest tests/test_llmcheck.py -q`：72 passed
- `dist/LLMcheck-GUI-0.1.1.exe --help`：正常输出帮助
- GUI exe 启动后请求 `http://127.0.0.1:8769/`：HTTP 200，页面包含 `LLMcheck`
- `dist/llmcheck-skill-0.1.1.tar.gz`：包含 `skill/README.md` 和 `skill/SKILL.md`

## 给其他 agent 的入口

优先使用 GitHub 仓库：

```text
https://github.com/oushixingfu/LLMcheck
```

如果 agent 在同一台机器上，也可以直接使用本地路径：

```text
/mnt/d/codex/LLMcheck
```

推荐读取顺序：

1. `README.md`
2. `skill/SKILL.md`
3. `llmcheck/cli.py`
4. `tests/test_llmcheck.py`
