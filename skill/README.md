# LLMcheck Agent Skill

Publishable [Agent Skill](https://github.com/vercel-labs/agent-skills) package for **LLMcheck** — convert multi-format documents into gate-checked Markdown that other agents can call.

Repository: https://github.com/oushixingfu/LLMcheck

## Install the skill (any Agent Skills–compatible host)

```bash
npx skills@latest add oushixingfu/LLMcheck -g -y
```

Scope to Claude Code only:

```bash
npx skills@latest add oushixingfu/LLMcheck -g -a claude-code -y
```

Manual:

```bash
git clone https://github.com/oushixingfu/LLMcheck.git
cp -R LLMcheck/skill ~/.claude/skills/llmcheck
# Codex / shared agents dir:
# cp -R LLMcheck/skill ~/.agents/skills/llmcheck
```

## Install the runtime (required)

The skill is the operating procedure. The converter is the Python package:

```bash
python3.12 -m pip install "git+https://github.com/oushixingfu/LLMcheck.git"
llmcheck agent profiles
```

Optional env:

```bash
export LLM_API_URL=...
export LLM_API_KEY=...
export LLM_MODEL=...
export MINERU_CLOUD_API_TOKEN=...   # PDF / image / Office
```

## Skill contents

| File | Purpose |
|------|---------|
| `SKILL.md` | Agent-facing procedure + JSON contract |
| `README.md` | This packaging / install note |
| `README.zh-CN.md` | Chinese install note |
| `LICENSE` | MIT |
| `agents/openai.yaml` | Codex / OpenAI agent display metadata |

Do **not** ship credentials, `.venv`, `process/` outputs, or user uploads inside the skill package.

## Agent CLI

```bash
llmcheck agent profiles
llmcheck agent convert --input /path/in --output-dir /path/out --llm-mode local-gate
llmcheck agent status --output-dir /path/out
llmcheck agent get-md --output-dir /path/out --document-id <id>
```

Exit codes: `0=passed`, `1=review/fail/non-passed`, `2=config/input error`.

## Delivery policy

- **Valid:** `md/` files for rows with `status == "passed"`.
- **Invalid:** `process/drafts/**`, review_required/failed rows.
- `get-md` never returns a body for non-passed documents.

## Trigger

After install, ask the agent:

> Use llmcheck to convert this document to passed Markdown.

or:

> /llmcheck convert this PDF

## Version

Aligned with package version in repo `pyproject.toml` (agent API schema `1.0`).
