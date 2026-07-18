# LLMcheck Project Instructions

## Interaction
- Use Chinese for clarification, progress updates, explanations, and final reports.
- For multi-step work, send one short status update before reading context or running tools.
- Evidence comes first: prefer current files, command output, tests, and logs over memory, summaries, or assumptions.

## Recovery Protocol
- After compaction, interruption, or resume, read this file first, then read `docs/progress/local_gate_run_index.json`.
- Treat `docs/progress/local_gate_run_index.json` as the project-local source of truth for the current local-gate book index.
- Also check `git status --short` and whether any `python.exe` process command line contains `llmcheck`, `D:\pdf\output2`, or `pytest` before continuing.
- Latest user instructions outrank this file; this file outranks old summaries and memory.

## Local-Gate Batch Work
- Run one book at a time in the foreground with `--book-concurrency 1`.
- Do not start unmonitored background test or batch processes.
- The current workflow goal is to strengthen deterministic pre-LLM cleaning and validation, so large-scale validation gates must not depend on LLM review.
- If a book exposes a real residue or gate issue, stop advancing, add or update focused tests, fix the deterministic cleaning/structure/final-gate path, rerun focused tests, then rerun that book.
- After each book is run and spot-checked, update `docs/progress/local_gate_run_index.json` before moving on.

## Spot-Check Contract
- `quality.json`: `pre_llm_gate.accepted == true` and `errors == []`.
- `final_acceptance.json`: `accepted == true` and `errors == []`.
- All `llm_calls.jsonl` files for the book are 0 bytes.
- Final Markdown has zero hits for the current typical residue pattern set.
- No residual long-running `python.exe` / `pytest` / `llmcheck` process remains.
