# LLMcheck 清洗与 LLM 审查收口重构 PRD

日期：2026-06-05  
状态：Draft / 可直接进入对照开发  
目标版本：下一轮主线重构版本

## 1. 文档目标

本 PRD 用于把 LLMcheck 从“LLM 主导全文纠错 + 分片验收”的模式，重构为“确定性清洗优先 + LLM 审稿优先 + 安全补丁写回 + 最终强门禁交付”的模式。

目标不是推翻 OCR、MinerU API、PPX 和现有输出目录体系，而是在现有工程骨架上解决两个核心问题：

1. **清洗阶段不能把真实内容处理坏。**  
2. **LLM 收口阶段要更像人类审稿，而不是整段改写器。**

该文档面向后续开发实施、测试设计、回归验收和与 `D:\pdf\output2` 在跑任务结果做对照分析。

---

## 2. 背景与问题定义

### 2.1 用户明确目标

当前项目中：

- OCR 与 MinerU API 阶段问题不大，不是主要瓶颈。
- 真正要优化的是 **文档清洗** 和 **LLM 最终收口**。
- 前段清洗需要“尽量不伤原文”。
- LLM 参与收口不一定要继续使用现在这套“全文纠错 + 返修”的模式。
- LLM 更适合以“人类阅读者/审稿员”的视角做最后一道质量识别：看有没有一眼可见的错误、结构崩坏、残留噪声、明显不可交付问题。

### 2.2 当前代码审计结论

基于当前代码实现，现状可归纳为：

#### 现有优势

1. **预处理骨架基本合理**  
   `llmcheck/preprocess.py` 已把 Markdown / PDF / 图片 / Office 统一到 Markdown 输入路径，且 PDF 已支持 MinerU 与 PPX 并行启动。

2. **批处理默认支持逐本执行**  
   `llmcheck/batch.py` 已具备 `book_concurrency=1` 的逐本处理能力，这与“先保证单本稳定，再扩大吞吐”方向一致。

3. **已有质量与 finalization 雏形**  
   `llmcheck/quality.py` 已有 deterministic 清洗、finalize_standard_document、final_acceptance_report 等基础能力。

4. **已有 LLM 熔断与重试能力**  
   `llmcheck/llm.py` 已提供 retry、timeout、circuit breaker 的基础机制。

5. **已有 process/ 与交付目录隔离思路**  
   `llmcheck/pipeline.py` 只在通过最终门禁后写入最终交付目录，方向是对的。

#### 关键问题

1. **LLM 当前仍承担“全文改写器”角色**  
   `build_correction_prompt()` 和 `correction_result_payload()` 仍要求模型返回完整 `corrected_text`。这意味着：
   - 每个 chunk 都会被 LLM 重写一遍；
   - 成本高；
   - 延迟高；
   - 更容易改坏真实内容；
   - 可审计性差。

2. **验收失败后的返修链条过长、过重**  
   当前流程是：
   - correction
   - acceptance
   - local repair
   - acceptance again
   - LLM repair rounds
   - acceptance again
   - finalization
   - final acceptance

   这会带来：
   - 单本调用次数膨胀；
   - 一处问题引发多轮 LLM 往返；
   - 修复与验收职责混杂；
   - 难以判断成本与收益是否匹配。

3. **清洗规则与 final gate 混在一个模块里**  
   `llmcheck/quality.py` 同时承载：
   - 文本清理
   - 结构修复
   - 规则检测
   - finalization
   - final acceptance

   结果是规则边界不清，后续难以做“只报告不写回”“低风险写回”“高风险人工复核”等分层。

4. **当前 final gate 仍偏“可见脏字符检测”，还不够像真实交付标准**  
   现有 `final_acceptance_report()` 重点阻断的是：
   - mojibake
   - replacement char
   - zero-width
   - forced line breaks
   - duplicate lines

   但尚未系统纳入：
   - 长文档标题缺失
   - 标题层级不合法
   - 章节结构不一致
   - Markdown / PDF 一致性
   - 规则清洗造成的内容改动风险
   - LLM patch 对数字、剂量、日期、实体词的误改风险

5. **CLI / GUI 参数还是围绕“并发纠错”设计，而不是“审稿 + 补丁”设计**  
   当前入口主要暴露：
   - `concurrency`
   - `llm_chunk_chars`
   - `acceptance_repair_rounds`

   但缺少：
   - `review_concurrency`
   - `patch_concurrency`
   - `max_calls_per_book`
   - `max_cost_per_book`
   - `idle_timeout`
   - `fallback policy`
   - `manual_review policy`

6. **README 与代码目录命名存在偏差**  
   README 里写的是 `文字版pdf/`，但 `pipeline.py` 里的 `TEXT_PDF_DIR_NAME` 当前实际是 `pdf`。这说明“产品口径”和“真实行为”已经出现漂移。

7. **一些规则仍保留明显中医语料偏置**  
   例如 `quality.py` 中的默认结构标签、表头识别和部分 heading 判断，仍带有较强中文医学/中医资料倾向。虽然 profile 已引入，但 deterministic 层还没有完全 profile 化。

---

## 3. 产品重构目标

### 3.1 总体目标

建立一条 **高保真、低误伤、强审计、低失控风险** 的文档清洗与收口流水线：

```text
输入文档
-> preprocess
-> deterministic cleaning
-> structural normalization
-> rule quality gate
-> llm review (只找问题)
-> patch planning
-> safe patch apply
-> final deterministic gate
-> optional final llm review
-> delivery
```

### 3.2 成功标准

重构后的系统应满足：

1. **默认情况下，LLM 不再重写整篇或整 chunk 正文。**
2. **大部分问题由 deterministic 规则解决。**
3. **LLM 的主职责是指出问题，不是改正文。**
4. **只有低风险、局部、可验证的补丁才允许自动写回。**
5. **任何高风险改动默认升级为人工复核。**
6. **最终交付目录只包含通过最终门禁的文件。**
7. **整本处理过程中，调用次数、成本、超时、停滞、fallback 都必须可观测。**

### 3.3 非目标

本轮不做：

- OCR 算法替换。
- MinerU API 服务端改造。
- PPX 核心能力重写。
- 通用事实校对。
- 基于领域知识的“智能补全缺文”。

---

## 4. 核心设计原则

### 4.1 保真优先于通顺

系统优先保证：

- 数字不被改错。
- 剂量不被改错。
- 日期不被改错。
- 人名、地名、专有名词不被改错。
- 原有章节顺序不被打乱。

即使某处仍不够通顺，只要无法安全确认，也应保留原文并输出审查问题，而不是让模型“猜一个更顺的版本”。

### 4.2 规则优先于模型

凡是可以用明确规则安全处理的问题，都不应交给 LLM：

- LaTeX 单位残留
- 标题空行
- 硬折行合并
- 零宽字符
- 替换符
- 重复页眉页脚
- Markdown 标题符号标准化

### 4.3 审查优先于改写

LLM 第一职责：**发现问题**。  
LLM 第二职责：**仅在局部上下文内给出低风险候选补丁**。  
LLM 不应再承担默认全文重写任务。

### 4.4 交付优先于“看起来修好了”

系统最终要回答的是：

- 这份 Markdown 能不能交付？
- 这份 PDF 是否与 Markdown 一致？
- 哪些问题阻断交付？
- 哪些问题只是提醒？

而不是“模型是否看起来做了一轮修正”。

---

## 5. 用户故事

1. **作为批处理操作者**，我希望整个目录默认逐本执行，单本失败不会拖垮整批。  
2. **作为质量负责人**，我希望每本书都有完整报告，知道失败在清洗、审查、补丁还是最终门禁。  
3. **作为成本负责人**，我希望模型调用次数、超时、fallback、预算都可硬限制。  
4. **作为开发者**，我希望 deterministic 规则、LLM review、patch apply、final gate 是明确分层的。  
5. **作为人工复核者**，我希望系统能告诉我“哪里不确定”“为什么不敢自动改”。

---

## 6. 目标流程设计

## 6.1 阶段定义

### Stage A: Preprocess

职责：

- 将输入统一转为 Markdown。
- 对 PDF 继续保留当前 MinerU 主文本 + PPX 审计旁路策略。
- 保留 source manifest、segment manifest、PPX 审计状态、MinerU status。

要求：

- 继续支持当前并行启动 MinerU 与 PPX 的模式。
- MinerU 结果仍作为正式文本主来源。
- PPX 输出作为审计/对照源，不直接参与自动写回。

### Stage B: Deterministic Cleaning

职责：

- 对 Markdown 做确定性清洗，不做语义改写。

输出：

- `cleaned.md`
- `cleaning_report.json`
- `rule_changes.json`

### Stage C: Structural Normalization

职责：

- 标准化标题、空行、列表、表格行、普通段落折行。
- 但结构推断要分级：
  - 低风险：可直接写回。
  - 中风险：默认只报告。
  - 高风险：阻断并人工复核。

### Stage D: Rule Quality Gate

职责：

- 在进入 LLM 前，先用 deterministic gate 挡掉明显不合格文本。
- 若 rule gate 已经足够通过，LLM review 可以只做质量背书。

### Stage E: LLM Review

职责：

- 以人类审稿视角识别显著问题。
- **不返回全文正文。**
- 只返回 issue list、严重程度、定位信息、建议动作。

### Stage F: Patch Planning

职责：

- 把 issue 分类为：
  - `rule_fix`
  - `safe_llm_patch`
  - `manual_review`

### Stage G: Safe Patch Apply

职责：

- 仅对允许写回的 issue 应用局部补丁。
- 所有写回都要通过 diff 安全校验。

### Stage H: Final Deterministic Gate

职责：

- 对 patch 后全文做最终门禁。
- 确认已经没有明显交付阻断项。

### Stage I: Optional Final LLM Review

职责：

- 只在 deterministic 已通过后执行。
- 用更轻量的 prompt 做最后“人眼可见问题”扫描。
- 不允许在这个阶段再做大规模正文改写。

### Stage J: Delivery

职责：

- 只把 `passed` 文档写入最终交付目录。
- PDF 只允许从 final Markdown 生成。

---

## 7. 详细功能需求

## 7.1 清洗规则系统重构

### 7.1.1 模块拆分

从当前 `llmcheck/quality.py` 中拆分为：

- `llmcheck/cleaning.py`：低风险确定性清洗
- `llmcheck/structure.py`：结构标准化
- `llmcheck/rules.py`：规则定义、风险等级、报告模型
- `llmcheck/final_gate.py`：最终门禁

`quality.py` 可保留为兼容 façade，但不应继续承载全部逻辑。

### 7.1.2 规则模型

每条规则必须具备：

- `rule_id`
- `description`
- `risk_level` (`low` / `medium` / `high`)
- `write_mode` (`auto_apply` / `report_only` / `block`)
- `match_count`
- `examples`
- `input_sha256`
- `output_sha256`

### 7.1.3 首批内置规则

必须实现：

1. `latex.strip_empty_math`
2. `latex.unit_math_to_text`
3. `latex.temperature_to_celsius`
4. `latex.lab_value_to_text`
5. `markdown.heading_spacing`
6. `markdown.heading_marker`
7. `paragraph.safe_line_join`
8. `artifact.zero_width_remove`
9. `artifact.bad_control_remove`
10. `artifact.mojibake_detect`
11. `artifact.replacement_char_detect`
12. `artifact.duplicate_running_header_detect`

### 7.1.4 profile 化要求

deterministic 层必须支持 profile 注入，不再把默认中医结构标签硬编码为全局默认行为。  
例如：

- `structure_labels`
- `table_headers`
- `protected_terms`
- `glue_markers`
- `heading_patterns`

必须从 profile 或其派生配置中读取。

---

## 7.2 标题与结构要求

最终 Markdown 必须满足：

1. 长文档不能是纯正文无标题。  
2. 标题必须满足 `#{1,6} + 空格 + 文本`。  
3. 标题前后必须有标准空行。  
4. 标题层级必须单调合理，不允许从 `#` 直接跳到 `####` 且无中间层说明。  
5. 目录型标题、章标题、节标题的判定规则必须可配置。  
6. 不确定是否为标题时，不自动提升，只输出结构风险问题。

新增阻断规则：

- `missing_primary_headings`
- `invalid_heading_syntax`
- `heading_level_jump`
- `headingless_long_document`

---

## 7.3 LLM Review 设计

### 7.3.1 角色定义

新的 LLM review prompt 必须把模型定义为：

- 文档质量审稿员
- 只做问题识别
- 不主动重写正文
- 不基于专业知识修正文义

### 7.3.2 输出契约

输出格式必须类似：

```json
{
  "status": "reviewed",
  "accepted": false,
  "summary": "仍存在标题层级缺失与 LaTeX 单位残留。",
  "issues": [
    {
      "id": "issue-001",
      "category": "latex_artifact",
      "severity": "blocking",
      "location_hint": "第 12 段",
      "excerpt": "瓜蒌 $9\\mathrm{g}",
      "reason": "残留 LaTeX 单位公式。",
      "suggested_action": "rule_fix",
      "safe_fix_type": "rule_fix"
    }
  ],
  "manual_review_notes": []
}
```

### 7.3.3 issue 分类

最少支持：

- `latex_artifact`
- `markdown_structure`
- `heading_hierarchy`
- `paragraph_break`
- `ocr_noise`
- `mojibake`
- `table_broken`
- `content_loss_risk`
- `pdf_md_mismatch`
- `model_uncertain`

### 7.3.4 严重级别

- `blocking`
- `major`
- `minor`

规则：

- `blocking`：不得交付。
- `major`：默认不交付，除非人工确认。
- `minor`：可交付但需记录。

---

## 7.4 Safe Patch 设计

### 7.4.1 允许自动补丁的前提

仅当同时满足以下条件时，允许补丁写回：

1. issue 可唯一定位。  
2. patch 范围小。  
3. 不改变数字、剂量、日期、金额、实体名。  
4. 不涉及大段内容重排。  
5. 通过 diff 风险检查。  
6. patch 来源被标记为 `rule_fix` 或 `safe_llm_patch`。

### 7.4.2 LLM patch 输出格式

```json
{
  "status": "patch_ready",
  "issue_id": "issue-001",
  "find": "中医\n\n学常把",
  "replace": "中医学常把",
  "confidence": 0.95,
  "risk": "low",
  "reason": "仅合并 OCR 物理折行。"
}
```

### 7.4.3 自动拒绝条件

以下情况必须拒绝自动写回：

- `find` 非唯一命中
- 编辑距离过大
- 数字变化
- 单位变化
- 日期变化
- 人名/地名/术语显著变化
- patch 影响段落过大
- patch 导致 Markdown 结构损坏

被拒绝后文档进入 `manual_review_required`，而不是继续盲修。

---

## 7.5 最终质量门禁

### 7.5.1 deterministic blocking 项

最终 gate 必须至少阻断以下问题：

- `mojibake`
- `replacement_characters`
- `zero_width_characters`
- `bad_control_characters`
- `abnormal_cjk_spaces`
- `forced_line_breaks`
- `duplicate_repeated_lines`
- `headingless_long_document`
- `invalid_heading_syntax`
- `heading_level_jump`
- `latex_artifacts`
- `pdf_md_mismatch`

### 7.5.2 最终 LLM gate 的角色

最终 LLM gate 不是“再改一遍”，而是：

- 对 deterministic 通过后的最终稿做最后人类视角扫描。
- 如果发现 `blocking` 问题，则阻断交付。
- 如果只发现 `minor`，则允许交付并记报告。

---

## 7.6 成本、超时与熔断保护

### 7.6.1 模型策略

默认模型：

- `deepseek-v4-pro`

默认禁止：

- 隐式 fallback 到高价模型
- 未明确授权时使用 `gpt-5.5`

### 7.6.2 必须新增的硬限制

- `--llm-max-calls-per-book`
- `--llm-max-cost-per-book`
- `--llm-stage-timeout-seconds`
- `--llm-call-timeout-seconds`
- `--llm-idle-timeout-seconds`
- `--allow-fallback-models`
- `--fallback-models`
- `--review-concurrency`
- `--patch-concurrency`

### 7.6.3 必须输出的成本观测

每本书都必须产出：

- `run_events.jsonl`
- `heartbeat.json`
- `cost_report.json`
- `stage_timings.json`
- `llm_calls.jsonl`

内容至少包括：

- 每阶段耗时
- 每阶段调用数
- 重试次数
- 熔断次数
- fallback 次数
- 估算 token / 字符数
- 估算成本
- 是否命中预算/超时/停滞上限

---

## 7.7 批处理与 D:\pdf\output2 对照要求

### 7.7.1 运行策略

- 默认逐本执行：`book_concurrency=1`
- 单本内部 review / patch 可并发，但必须独立超时和独立计数
- 单本失败后，整批继续下一本

### 7.7.2 对照分析要求

在后续开发落地时，必须选取 `D:\pdf\output2` 中至少三类样本做回归基线：

1. **清洗类问题样本**：LaTeX 单位、异常空格、硬折行  
2. **结构类问题样本**：无标题、标题层级乱、表格破坏  
3. **LLM 高风险样本**：涉及剂量、数字、日期、术语敏感变更风险

每类样本都要输出：

- 旧流程结果
- 新流程结果
- rule changes
- review issues
- final decision
- 是否进入 manual review

---

## 7.8 问题发现后的纠偏责任链

这一部分必须明确，否则系统即使能发现问题，也会卡在“谁来处理”上。

### 7.8.1 问题来源分三类

1. **规则/测试发现的问题**  
   来源包括：
   - 单元测试失败
   - 集成测试失败
   - 回归测试失败
   - deterministic final gate 阻断
   - MD/PDF 一致性检查失败

2. **LLM review 发现的问题**  
   来源包括：
   - `llm_review` 输出 `blocking` / `major` issue
   - optional final LLM review 输出阻断问题
   - LLM patch 被安全检查拒绝

3. **人工抽检发现的问题**  
   来源包括：
   - 交付前 spot check
   - 对照 `D:\pdf\output2` 旧结果时发现差异
   - 用户反馈“虽然 gate 通过，但看起来仍不对”

### 7.8.2 责任归属原则

#### A. 测试/规则发现的问题

默认由 **deterministic 规则层负责纠偏**，不是先丢给 LLM。

适用情形：

- LaTeX 残留
- 标题格式错误
- 标题层级缺失或跳变
- 零宽字符 / 替换符 / 乱码
- 重复页眉页脚
- 硬折行残留
- Markdown / PDF hash 不一致

处理责任：

- **第一责任人：规则引擎与 pipeline 实现**
- **处理方式：修改 deterministic rule / final gate / pipeline glue code**
- **禁止处理方式：为了让测试通过，直接让 LLM 重写全文**

结论：**凡是测试能稳定复现的问题，应优先视为工程规则缺陷，而不是模型能力问题。**

#### B. LLM review 发现的问题

默认由 **patch planner + safe patch apply + manual review 分流机制** 负责纠偏。

适用情形：

- 文本整体可读，但局部结构仍不自然
- 某一段存在明显断裂、粘连、局部格式错误
- 标题疑似缺失但规则无法安全判断
- 某局部内容“看起来不对”，但 deterministic 规则不能证明如何修

处理责任：

- **第一责任人：review issue 分流器**
- **如果是低风险局部问题**：进入 `safe_llm_patch`
- **如果是规则可处理问题**：回流 `rule_fix`
- **如果涉及事实、数字、剂量、日期、专有名词**：进入 `manual_review`

结论：**LLM 发现问题不等于 LLM 自动修问题。发现与修复必须拆开。**

#### C. 人工抽检发现的问题

默认由 **产品/规则策略层** 负责归因，再决定回流方向。

处理顺序：

1. 先判断是否可稳定复现。  
2. 能复现则补测试。  
3. 再判断问题属于：
   - rule gap
   - review schema gap
   - patch safety gap
   - gate threshold gap
   - 真正只能人工处理的内容

结论：**人工发现的问题最终必须沉淀回规则、测试或审查 schema，而不是只改一份样本。**

### 7.8.3 闭环决策矩阵

| 问题来源 | 问题类型 | 默认处理方 | 默认动作 |
| --- | --- | --- | --- |
| 单元/集成/回归测试 | 可规则化问题 | deterministic rule owner | 改规则 + 补测试 |
| final deterministic gate | 可规则化阻断 | final gate owner | 调整 gate / 清洗规则 |
| LLM review | 低风险局部格式问题 | patch planner | 进入 safe patch |
| LLM review | 高风险事实类问题 | manual review owner | 阻断交付，输出人工复核 |
| patch apply | diff 风险超限 | patch safety owner | 拒绝写回，转 manual review |
| 人工抽检 | 可复现共性问题 | engineering owner | 先补测试，再归类修复 |
| 人工抽检 | 不可稳定复现问题 | product/manual review | 标注例外，不自动化写回 |

### 7.8.4 Pipeline 必须体现的状态机

当问题被发现后，文档状态必须明确流转，不能只写一条 summary：

- `rule_fix_required`
- `llm_review_flagged`
- `patch_candidate`
- `patch_rejected_high_risk`
- `manual_review_required`
- `final_gate_failed`
- `passed`

要求：

- 每次状态流转都写入 `run_events.jsonl`
- 每个 issue 都要记录 `discovered_by`
- 每个 issue 都要记录 `resolved_by`
- 每个 issue 都要记录最终结论：
  - `fixed_by_rule`
  - `fixed_by_safe_patch`
  - `waived_as_minor`
  - `escalated_to_manual_review`
  - `delivery_blocked`

### 7.8.5 角色分工（产品级定义）

为避免后续开发时职责混乱，PRD 里明确以下“逻辑责任角色”：

#### 1. Rule Owner

负责：

- 清洗规则
- 结构规则
- final deterministic gate
- 可复现问题的自动修复能力

输入：测试失败、gate 阻断、人工复现样本。  
输出：规则修复、测试补齐、风险等级调整。

#### 2. Review Owner

负责：

- LLM review prompt
- issue schema
- blocking/major/minor 判定标准
- review-first 模式质量

输入：LLM 漏报、误报、分类不清。  
输出：prompt / schema / severity 规则修订。

#### 3. Patch Safety Owner

负责：

- patch planner
- diff 安全检查
- 敏感字段保护
- patch 自动拒绝策略

输入：补丁误改、误伤数字、误伤术语、改动范围失控。  
输出：patch validator 与阈值修订。

#### 4. Manual Review Owner

负责：

- 不能自动纠偏的样本
- 高风险 issue 的人工判定
- 例外场景沉淀

输入：所有 `manual_review_required` 文档。  
输出：人工结论，以及是否应下沉为后续自动规则。

### 7.8.6 开发与测试阶段的执行要求

后续对照开发时，必须遵守：

1. **测试发现的问题，先查规则层，不先怪模型。**
2. **LLM 发现的问题，先走分流，不允许直接全文重写覆盖。**
3. **人工发现的问题，必须回灌成测试或规则缺口。**
4. **任何纠偏动作都要能在报告中回答：是谁发现、谁处理、怎么处理、为什么允许交付。**

### 7.8.7 验收标准补充

PRD 验收时，除“问题能发现”外，还必须验证：

- 测试发现的问题能明确落到 `rule owner` 或 `final gate owner`
- LLM 发现的问题不会直接触发全文重写
- 高风险 issue 会进入 `manual_review_required`
- 每个 issue 都能追踪 `discovered_by` 与 `resolved_by`
- 输出报告能明确区分：
  - 规则修复
  - 安全补丁修复
  - 人工复核
  - 阻断交付

---

## 8. 输出目录与产物规范

目标目录：

```text
output/
  md/
    <book>.md
  pdf/
    <book>.pdf
  process/
    <book>/
      source_manifest.json
      preprocess/
      clean/
      review/
      patches/
      final/
      reports/
      run_events.jsonl
      heartbeat.json
      cost_report.json
      stage_timings.json
```

说明：

- 当前代码实际输出目录名是 `pdf/`，本 PRD 以真实代码行为为准。
- 如果后续产品层要恢复“文字版pdf”命名，需要单独立项处理，不应在本轮与清洗/审稿重构混改。

约束：

- `md/` 与 `pdf/` 只允许包含 `passed` 文档。
- 未通过文档只能保留在 `process/`。
- PDF 必须从 final Markdown 生成，并记录 hash 绑定关系。

---

## 9. CLI / GUI 产品需求

## 9.1 CLI

建议调整为：

```text
llmcheck run
  --input <path>
  --output-dir <path>
  --profile <id>
  --llm-model deepseek-v4-pro
  --llm-mode review-first
  --review-concurrency 4
  --patch-concurrency 2
  --allow-fallback-models false
  --llm-max-calls-per-book 300
  --llm-max-cost-per-book <value>
  --llm-call-timeout-seconds 120
  --llm-stage-timeout-seconds 1800
  --llm-idle-timeout-seconds 300
```

批处理：

```text
llmcheck batch
  --source-dir <path>
  --output-dir <path>
  --book-concurrency 1
  --resume
  --fail-fast false
```

新增状态类命令：

```text
llmcheck inspect --book-output-dir <path>
llmcheck cost-summary --output-dir <path>
llmcheck quality-report --book-output-dir <path>
llmcheck review-report --book-output-dir <path>
```

## 9.2 GUI

GUI 必须新增或重构以下显示项：

- 当前模式：`review-first` / `legacy-rewrite`
- review 并发
- patch 并发
- 单本最大调用数
- 单本最大成本
- 单请求超时
- 单阶段超时
- idle timeout
- fallback 开关与 fallback 模型列表
- 当前阶段
- 当前书进度
- 最近 heartbeat
- 当前书累计调用数
- 当前书估算成本
- blocking issue 列表
- manual review 原因

---

## 10. 数据结构需求

### 10.1 RuleChange

```json
{
  "rule_id": "latex.unit_math_to_text",
  "risk": "low",
  "count": 18,
  "examples": [
    {
      "before": "$9\\mathrm{g}$",
      "after": "9g"
    }
  ]
}
```

### 10.2 ReviewIssue

```json
{
  "id": "issue-001",
  "category": "heading_hierarchy",
  "severity": "blocking",
  "location_hint": "第 3 节附近",
  "excerpt": "临床经验",
  "reason": "长文档缺少一级标题结构。",
  "suggested_action": "manual_review",
  "safe_fix_type": "manual_review"
}
```

### 10.3 LlmCallRecord

```json
{
  "stage": "llm_review",
  "model": "deepseek-v4-pro",
  "request_id": "...",
  "started_at": "...",
  "finished_at": "...",
  "duration_seconds": 8.4,
  "status": "ok",
  "retry_count": 0,
  "input_chars": 2000,
  "output_chars": 1200,
  "estimated_cost": null
}
```

### 10.4 FinalDecision

```json
{
  "status": "manual_review_required",
  "accepted": false,
  "blocking_reasons": [
    "headingless_long_document",
    "content_loss_risk"
  ],
  "delivery_written": false
}
```

---

## 11. 验收标准

## 11.1 单元测试必须覆盖

1. `$9\mathrm{g}$` -> `9g`  
2. `$ \mathrm{ }` 被清理  
3. `$40.1^{\circ} \mathrm{C}$` -> `40.1℃`  
4. `TBIL $428\mu \mathrm{mol}/L$` -> `TBIL 428μmol/L`  
5. 长文档无标题时 final gate 阻断  
6. 标题格式错误时 final gate 阻断  
7. heading level jump 被阻断  
8. review 模式只返回 issues，不返回全文正文  
9. patch 不能修改数字、剂量、日期  
10. fallback 未开启时不能调用非默认模型  
11. 单本调用数超限时停止  
12. idle timeout 触发时停止当前书并写报告

## 11.2 集成测试必须覆盖

必须新增 fixture：

- `fixtures/103_no_headings.md`
- `fixtures/104_no_headings.md`
- `fixtures/latex_units.md`
- `fixtures/forced_line_breaks.md`
- `fixtures/md_pdf_consistency.md`
- `fixtures/risky_numeric_patch.md`

必须验证：

- 长文档标题缺失能被阻断或安全补出结构。  
- LaTeX 残留被规则清理或 gate 拦截。  
- review 能发现明显 Markdown 结构错误。  
- 失败文档不进入 `md/` / `pdf/`。  
- PDF 从 final Markdown 生成且 hash 可追溯。  
- review-first 模式下 LLM 调用次数显著低于 legacy-rewrite 模式。

## 11.3 回归测试必须覆盖

必须保留：

- PDF 仍能走 MinerU
- Markdown 仍能直接处理
- `process/` 中间产物保留
- `md/` / `pdf/` 只输出 passed
- profile 仍可注入领域保护规则

---

## 12. 分阶段实施方案

### Phase 1：观测与保护先行

目标：先把“失控风险”降下来。

范围：

- 增加 run events、heartbeat、cost report、stage timings
- 增加 max calls / timeout / idle timeout / fallback policy
- 让 CLI / GUI 先能展示真实运行状态

完成标准：

- 即使仍用旧流程，也能看清单本耗时、调用数、失败点。

### Phase 2：清洗规则分层

目标：把 deterministic 层从 `quality.py` 中独立出来。

范围：

- 建立 rule registry
- 拆 cleaning / structure / rules / final_gate
- 把低风险写回规则稳定下来

完成标准：

- 大部分 LaTeX、空格、硬折行、标题格式问题不再需要 LLM 参与。

### Phase 3：引入 review-first 模式

目标：让 LLM 从“全文改写”切换到“问题审查”。

范围：

- 新增 review prompt
- 新增 issue schema
- 让 pipeline 支持 `review-first`
- legacy correction 模式暂时保留，但不再默认

完成标准：

- 默认主流程不再要求 LLM 返回完整 corrected_text。

### Phase 4：安全补丁系统

目标：只允许低风险局部补丁写回。

范围：

- patch planner
- safe patch apply
- diff/risk validator
- manual review 输出

完成标准：

- patch 可审计，且不会误改关键数字和实体。

### Phase 5：最终门禁与交付一致性

目标：把交付标准变成机器可验证规则。

范围：

- heading / structure / md-pdf consistency gate
- final decision report
- delivery 写出条件统一

完成标准：

- “通过 / 不通过 / 人工复核”三种结论稳定可重复。

### Phase 6：用 D:\pdf\output2 做对照回归

目标：用真实在跑任务做效果评估。

范围：

- 选样本对照旧流程与新流程
- 比较调用次数、耗时、通过率、误改率、人工复核率

完成标准：

- 能回答“新方案到底更稳还是更贵、是否值得默认切换”。

---

## 13. 风险与对策

| 风险 | 对策 |
| --- | --- |
| 规则误清洗真实内容 | 规则分风险等级；高风险规则只报告不写回 |
| LLM 仍然幻觉补丁 | 默认 review-first；patch 受 diff 与敏感字段保护 |
| 成本再次失控 | 默认 deepseek-v4-pro；fallback 默认关闭；加预算/超时/熔断/停滞限制 |
| 新老流程切换风险高 | 保留 legacy-rewrite 兼容模式作对照，不直接硬切 |
| profile 化不完整导致领域文档回归 | deterministic 规则必须显式接受 profile 配置 |
| README 与真实行为继续漂移 | 所有产品文案和目录命名在 Phase 1 统一校对 |

---

## 14. 立项结论

当前项目不需要“推倒重来”，但确实需要一次**架构层面的清洗与收口重构**。  
核心转向应当是：

- **从 LLM 主导改写，转为规则主导清洗。**
- **从 LLM 直接产出全文，转为 LLM 只做审稿与局部补丁建议。**
- **从结果导向的“看起来修好了”，转为交付导向的“确定可以交付”。**

这份 PRD 可直接作为后续对照开发的实现依据。

