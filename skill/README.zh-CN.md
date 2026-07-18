# LLMcheck Agent Skill（中文）

把多格式文档转成**通过门禁的 Markdown**，供任意兼容 Agent Skills 的智能体安装调用。

仓库：https://github.com/oushixingfu/LLMcheck

## 安装 Skill

```bash
npx skills@latest add oushixingfu/LLMcheck -g -y
```

仅装到 Claude Code：

```bash
npx skills@latest add oushixingfu/LLMcheck -g -a claude-code -y
```

手动：

```bash
git clone https://github.com/oushixingfu/LLMcheck.git
cp -R LLMcheck/skill ~/.claude/skills/llmcheck
```

## 安装运行时（必须）

Skill 只是操作说明；真正执行靠 Python 包：

```bash
python3.12 -m pip install "git+https://github.com/oushixingfu/LLMcheck.git"
llmcheck agent profiles
```

环境变量（按需）：

```bash
export LLM_API_URL=...
export LLM_API_KEY=...
export LLM_MODEL=...
export MINERU_CLOUD_API_TOKEN=...   # PDF/图片/Office 需要
```

## 智能体怎么用

```bash
llmcheck agent convert --input /path/in --output-dir /path/out --llm-mode local-gate
llmcheck agent status --output-dir /path/out
llmcheck agent get-md --output-dir /path/out --document-id <id>
```

硬规则：

1. 只用 `llmcheck agent *` JSON 契约  
2. **只有** `status=passed` 的 `md/` 可当交付物  
3. 禁止把 `process/drafts` 当正文  
4. 密钥走环境变量  

## 流程一句话

MinerU API ∥ 本地 PPX → 交叉择优 initial.md → 清洗/结构/门禁 → 仅 passed 写入 md/

## 触发话术示例

> 用 llmcheck 把这份 PDF 转成通过门禁的 Markdown  
> /llmcheck convert this document
