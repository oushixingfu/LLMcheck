# Local-Gate Progress Notes

本目录保存本地逐本门禁任务的可恢复锚点。压缩、中断或换 agent 后，先读项目根目录 `AGENTS.md`，再读本目录的 `local_gate_run_index.json`。

## Source Of Truth

- `local_gate_run_index.json` 是当前逐本 local-gate 进度的项目内事实来源。
- `checkpoint.next_index` 是下一本要跑的批处理 index。
- `checkpoint.next_source_stem` 是下一本源文件数字 stem，用来避免误匹配 `12` 和 `120` 这类文件。
- `last_run` 只记录最近一次已验收通过的书，不能替代 `next_index`。

## Required Loop

1. 确认没有残留 `python.exe` / `pytest` / `llmcheck` 进程。
2. 使用 `next_command` 或同等参数，只跑一本，且 `--book-concurrency 1`。
3. 如果门禁失败，停止推进，修确定性清洗、结构规范化或最终门禁，并补聚焦测试。
4. 聚焦测试通过后，重跑同一本。
5. 单本验收通过后，更新 `local_gate_run_index.json`，再进入下一本。

## Worktree Hygiene

- 保留当前 local-gate 流程资产：`AGENTS.md`、`docs/prd/`、`docs/progress/`、`llmcheck/cleaning.py`、`llmcheck/final_gate.py`、`llmcheck/rules.py`、`llmcheck/structure.py`、`llmcheck/run_guard.py` 及对应测试。
- 清理本地运行缓存：`.pytest_cache/`、顶层 `llmcheck/__pycache__/`、`tests/__pycache__/` 和 `*.pyc`。
- `dist/` 下已跟踪 exe 只有在明确做 release build 时才应保留脏改动；local-gate 迭代期间不要让二进制构建产物混入规则/文档 diff。
- `.codegraph/` 和 `.workbuddy/` 是本地工具状态，已忽略；不要把它们当作 local-gate 验收证据。

## Acceptance Contract

- `quality.json`: `pre_llm_gate.accepted == true` 且 `errors == []`。
- `final_acceptance.json`: `accepted == true` 且 blocking errors 为空。
- 当前书所有 `llm_calls.jsonl` 为 0 字节。
- 最终 Markdown 第一非空行是标准 Markdown 标题。
- 典型残留、重复目录标题、非标准标题残留为 0。
- 结束后没有残留长运行 Python/pytest/llmcheck 进程。
