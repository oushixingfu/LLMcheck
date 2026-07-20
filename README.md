# LLMcheck

LLMcheck 是一个 profile 驱动的文档转换、清洗、收口和标准文档交付工具。它把 Markdown、PDF、图片和 Office 文件统一转成 Markdown，经过确定性清理、结构规范化、可选 LLM 审阅/返修和最终确定性门禁后，只把可交付结果写入 `md/` 和 `pdf/`。

- GitHub: https://github.com/oushixingfu/LLMcheck
- 本地项目: `/mnt/d/codex/LLMcheck`
- 当前版本: `0.1.1`
- GUI exe: `dist/LLMcheck-GUI-0.1.1.exe`
- Agent skill: `skill/`（`npx skills@latest add oushixingfu/LLMcheck`）
- Agent skill 包: `dist/llmcheck-skill-0.1.1.tar.gz`

## 安装为 Agent Skill（任意兼容智能体）

其他 agent（Claude Code / Codex / Cursor 等）可通过 skills CLI 安装本仓库内的 `skill/`：

```bash
npx skills@latest add oushixingfu/LLMcheck -g -y
```

安装后仍需本机 Python 运行时：

```bash
python3.12 -m pip install "git+https://github.com/oushixingfu/LLMcheck.git"
llmcheck agent profiles
```

智能体调用契约：

```bash
llmcheck agent convert --input /path/in --output-dir /path/out --llm-mode local-gate
llmcheck agent status --output-dir /path/out
llmcheck agent get-md --output-dir /path/out --document-id <id>
```

硬规则：只信任 `status=passed` 的 `md/`；禁止把 `process/drafts` 当交付物。详见 `skill/SKILL.md`。

打包 skill 发布物：

```bash
bash tools/package_skill.sh
# → dist/llmcheck-skill-<version>.tar.gz
```

## 适用场景

LLMcheck 不再把默认流程写死为中医资料。默认 profile 是 `general_standard_document`，适合书籍、报告、手册、扫描档案、教材、政策材料和普通长文档。领域规则通过 profile 注入：

| Profile | 用途 |
| --- | --- |
| `general_standard_document` | 通用标准文档，默认值 |
| `academic_paper` | 学术论文、研究报告、引用和公式密集材料 |
| `technical_manual` | 技术手册、API 文档、操作步骤、工程规范 |
| `legal_contract` | 合同、协议、制度、条款类文本 |
| `financial_report` | 财报、审计、预算、统计表和经营数据 |
| `medical_reference` | 医学教材、病例、处方、诊疗参考 |
| `chinese_medicine_reference` | 中医古籍、医案、方剂、针灸和理论材料 |

查看内置 profile：

```bash
llmcheck profiles
```

## 输入流程

- Markdown (`.md`)：直接进入清洗、纠错、验收和最终收口。
- PDF (`.pdf`)：默认只走 MinerU API VLM。本地 PPX 审计/回退为**可选**，需显式 `--enable-ppx`（否则不启动，避免卡死机器）。
- 图片：`.png`, `.jpg`, `.jpeg`, `.jp2`, `.webp`, `.gif`, `.bmp`，直接走 MinerU API VLM。
- Office：`.doc`, `.docx`, `.ppt`, `.pptx`, `.xls`, `.xlsx`，直接走 MinerU API VLM。

PDF 默认按 30 页切片并发提交 MinerU，可用 `--pdf-page-chunk-size` 调整。需要 PPX 时再加 `--enable-ppx`，并可配合 `--mineru-fallback ppx`。

## 快速使用

### Windows GUI exe

```powershell
dist\LLMcheck-GUI-0.1.1.exe
```

默认启动原生 Windows 桌面窗口，不需要浏览器。菜单包含 `文件`、`运行`、`Profile`、`报告`、`帮助`；主界面按最新流程分为 `任务`、`Profile`、`LLM`、`MinerU / PPX`、`批处理`、`运行与报告`。桌面版可以选择 profile、输入文件/文件夹、输出目录、LLM 参数、MinerU/PPX 转换参数、逐本批处理参数，并在窗口内查看进度和报告摘要。

### Python CLI

单文件或单目录：

```bash
llmcheck run \
  --profile general_standard_document \
  --input /path/to/file-or-directory \
  --output-dir /path/to/output \
  --llm-api-url http://127.0.0.1:3022 \
  --llm-api-key "$LLM_API_KEY" \
  --llm-model deepseek-v4-pro \
  --mineru-api-key "$MINERU_CLOUD_API_TOKEN" \
  --mineru-concurrency 12 \
  --pdf-page-chunk-size 30 \
  --llm-chunk-chars 2000 \
  --concurrency 10
```

逐本批处理目录：

```bash
llmcheck batch \
  --source-dir /mnt/d/pdf \
  --output-dir /mnt/d/pdf/output \
  --profile technical_manual \
  --llm-api-url http://127.0.0.1:3022 \
  --llm-api-key "$LLM_API_KEY" \
  --llm-model deepseek-v4-pro \
  --mineru-api-key "$MINERU_CLOUD_API_TOKEN"
```

进阶调参示例：

```bash
llmcheck batch \
  --source-dir /mnt/d/pdf \
  --output-dir /mnt/d/pdf/output \
  --profile technical_manual \
  --llm-api-url http://127.0.0.1:3022 \
  --llm-api-key "$LLM_API_KEY" \
  --llm-model deepseek-v4-pro \
  --mineru-api-key "$MINERU_CLOUD_API_TOKEN" \
  --start-index 1 \
  --limit 1 \
  --book-concurrency 1 \
  --mineru-concurrency 50 \
  --mineru-timeout-seconds 14400 \
  --pdf-page-chunk-size 30 \
  --llm-chunk-chars 2000 \
  --concurrency 32
```

常用参数：

- `--profile`: 文档处理 profile，默认 `general_standard_document`。
- `--llm-api-url`: OpenAI-compatible chat completions endpoint 或 base URL。
- `--llm-api-key`: LLM API key。
- `--llm-model`: LLM 模型名。
- `--llm-model-api-urls`: 按模型覆盖 LLM base URL，默认 `mimo-v2.5-pro=https://api.iaigc.fun/v1,gpt-5.5=http://127.0.0.1:3022`。
- `--mineru-api-key`: MinerU API token。也可用环境变量 `MINERU_CLOUD_API_TOKEN`。
- `--force`: batch 模式下强制重跑已通过书目。

进阶参数：

- `--concurrency`: LLM 分片纠错/验收并发。
- `--acceptance-repair-rounds`: LLM 验收失败后的返修轮数。
- `--llm-chunk-chars`: 分片大小。
- `--mineru-concurrency` / `--mineru-timeout-seconds` / `--pdf-page-chunk-size`: MinerU 与 PDF 切片调参。

## 输出目录

最终输出只保留三个顶层目录：

```text
output/
  md/           # 通过最终验收的 Markdown
  pdf/          # 通过最终验收的文字版 PDF
  process/      # 所有过程文件、草稿、预处理结果、JSON 报告和批处理状态
```

`md/` 和 `pdf/` 只写入状态为 `passed` 的文档。任何 correction、acceptance、repair、final acceptance 失败的文档只保留在 `process/`，不会污染交付目录。

## 质量门禁

主流程：

```text
preprocess
-> deterministic clean
-> review-first LLM correction draft
-> LLM review / local safe patch / LLM repair
-> whole-document finalization
-> final deterministic acceptance
-> md + pdf
```

最终验收会阻断以下明显交付问题：

- 乱码和替换字符：`锟斤拷`, `�`, 常见 Latin-1/UTF-8 解码残留。
- 零宽字符、控制字符、异常中文空格。
- 普通段落中的 OCR 物理折行和强制换行。
- 重复短行，常见于页眉、页脚或扫描噪声。
- 分片合并后仍残留的结构问题。

整篇 finalization 会保守处理标题间距、重复页眉页脚、段落空行等，不做摘要、不补写事实、不进行领域解释。

## 过程报告

重点报告都在 `process/reports/`：

- `*.quality.json`: 确定性清理质量错误和 hints。
- `*.llm_correction.json`: LLM 纠错汇总。
- `*.llm_correction_chunks/*.json`: 分片纠错缓存和审计。
- `*.llm_acceptance.json`: LLM 验收汇总。
- `*.llm_acceptance_chunks/*.json`: 分片验收缓存和审计。
- `*.local_repair.json`: 确定性局部返修记录。
- `*.llm_repair.json` / `*.llm_repair_chunks/*.json`: LLM 返修记录。
- `*.finalization.json`: 整篇收口修改记录。
- `*.final_acceptance.json`: 写入交付目录前的最终质量门禁。
- `llmcheck_manifest.jsonl`: 单任务文档结果行。
- `llmcheck_summary.json`: 单任务汇总。
- `llmcheck_batch_state.jsonl`: 批处理累计状态。
- `llmcheck_batch_summary.json`: 批处理汇总。

先看 summary，再看失败文档对应报告。只有 summary/document 状态为 `passed` 且 `*.final_acceptance.json` 的 `accepted=true`，才使用 `md/` 和 `pdf/`。

## Agent-callable（P0）

LLMcheck 可作为其他 agent 调用的文档处理过程：

**多格式输入 → 采集（MinerU API；可选本地 PPX 仅 `--enable-ppx`）→ cross 选择初始 MD → 确定性清洗 / 结构 / 门禁 → 仅在 `passed` 时交付 `md/`**

设计文档：[`docs/prd/2026-07-18-agent-callable-dual-engine-pipeline.md`](docs/prd/2026-07-18-agent-callable-dual-engine-pipeline.md)

### Agent CLI（P0 接口）

```bash
llmcheck agent convert --input /path/to/file-or-dir --output-dir /path/to/output
llmcheck agent status --output-dir /path/to/output
llmcheck agent get-md --output-dir /path/to/output --document-id <id>
llmcheck agent profiles
```

规则：

- 作业报告 `schema_version=1.0`，含 `job_id`、`status`、`documents[]`、`artifacts.{md_dir,process_dir,pdf_dir}`。
- 只有 `status=passed` 的文档可通过 `get-md` 暴露最终 Markdown；`process/drafts` 不是交付物。
- Agent 子命令退出码：`0=passed`，`1=review/fail`，`2=config/dependency error`。

### 给其他 agent 的入口

其他 agent 可以直接使用：

```text
Repo: https://github.com/oushixingfu/LLMcheck
Local root: /mnt/d/codex/LLMcheck
Skill source: /mnt/d/codex/LLMcheck/skill/SKILL.md
Skill package: /mnt/d/codex/LLMcheck/dist/llmcheck-skill-0.1.1.tar.gz
GUI exe: /mnt/d/codex/LLMcheck/dist/LLMcheck-GUI-0.1.1.exe
Design: /mnt/d/codex/LLMcheck/docs/prd/2026-07-18-agent-callable-dual-engine-pipeline.md
```

建议 agent 先运行：

```bash
llmcheck agent profiles
llmcheck agent convert --help
llmcheck profiles
llmcheck run --help
llmcheck batch --help
```

## 构建与验证

测试：

```bash
uv run pytest -q
```

重建 skill 包：

```bash
tar -czf dist/llmcheck-skill-0.1.1.tar.gz skill README.md pyproject.toml
```

重建 Windows GUI exe：

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_windows_gui_exe.ps1
```

## 项目结构

```text
llmcheck/
  profiles.py       # 文档 profile registry
  llm.py            # LLM prompt、JSON 调用和 payload
  cleaning.py       # 确定性文本清洗和本地安全返修
  final_gate.py     # 最终确定性质量门禁
  structure.py      # 标题、目录、前后置噪声等结构规范化
  quality.py        # 兼容入口，转发到 cleaning/final_gate/structure
  rules.py          # 清洗规则注册表和审计元数据
  run_guard.py      # 前台运行锁和心跳
  pipeline.py       # 单任务主流程
  preprocess.py     # MinerU/PPX/输入规范化
  batch.py          # 逐本批处理、续跑、summary/state
  cli.py            # CLI 入口
  desktop_gui.py    # 原生 Tk 桌面 GUI
  gui.py            # FastAPI Web GUI/server 入口，保留给本地调试和旧流程
  pdf.py            # 文字版 PDF 写出
skill/
  SKILL.md          # agent-facing skill
  README.md         # skill 包说明
tools/
  build_windows_gui_exe.ps1
  organize_pdf_output.py
tests/
  test_*.py
dist/
  LLMcheck-GUI-0.1.1.exe
  llmcheck-skill-0.1.1.tar.gz
```

## 验收记录

当前升级验证：

- 当前 local-gate 逐本迭代状态以 `docs/progress/local_gate_run_index.json` 为准。
- 压缩、中断或换 agent 后，先读 `AGENTS.md`，再读 `docs/progress/local_gate_run_index.json`。
- 聚焦验证优先跑 `python -m pytest tests/test_latex_unit_cleanup.py tests/test_quality_finalization.py -q`。
