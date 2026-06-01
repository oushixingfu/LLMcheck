# Domain-Neutral Document Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade LLMcheck from a Chinese-medicine-specific cleaner into a profile-driven document conversion, cleanup, finalization, and delivery pipeline for general standard documents and optional domain presets.

**Architecture:** Add a small immutable profile registry, route LLM prompts and deterministic quality checks through the selected profile, then add a conservative whole-document finalization and final acceptance gate before writing `md/` and `文字版pdf/`. Keep the existing preprocess, correction, repair, batch, and output-directory model intact.

**Tech Stack:** Python 3.12, dataclasses, argparse, FastAPI, pytest, uv, existing LLM JSON contracts, existing ReportLab text-PDF writer.

---

## File Structure

- Create `llmcheck/profiles.py`: built-in `DocumentProfile` registry, default profile id, validation helpers, CLI/GUI option payloads.
- Modify `llmcheck/llm.py`: prompt builders accept `DocumentProfile`; default prompt identity becomes domain-neutral; medical/Chinese-medicine details only come from the optional profile.
- Modify `llmcheck/quality.py`: add profile-aware deterministic checks, finalization helpers, and final acceptance report model while preserving existing local repair behavior.
- Modify `llmcheck/pipeline.py`: add `profile_id` to settings; resolve profile once; include profile in reports; run whole-document finalization and final acceptance before final output.
- Modify `llmcheck/cli.py`: add `--profile` to `run` and `batch`, add `profiles` command.
- Modify `llmcheck/gui.py`: add profile selector and include selected profile in API payload/settings.
- Modify `tests/test_llmcheck.py`: add focused red/green tests for profiles, prompts, quality gates, pipeline finalization, CLI, GUI HTML.
- Modify `README.md`, `skill/SKILL.md`, `skill/README.md`: document domain-neutral purpose, profiles, output contract, reports, and other-agent entry points.
- Rebuild `dist/llmcheck-skill-0.1.1.tar.gz` and `dist/LLMcheck-GUI-0.1.1.exe` after tests pass.

## Execution Model

Use subagent-driven development with disjoint ownership where possible:

- Worker A owns `llmcheck/profiles.py`, `llmcheck/llm.py`, prompt/profile tests.
- Worker B owns `llmcheck/quality.py`, finalization tests.
- Controller owns `llmcheck/pipeline.py` integration because it touches shared flow.
- Worker C owns `llmcheck/cli.py`, `llmcheck/gui.py`, interface tests.
- Worker D owns docs and packaging checks after code behavior is stable.

Workers must not revert edits made by others. Each worker edits only the assigned files unless the controller explicitly expands scope.

## Task 1: Profiles And Domain-Neutral Prompt Contract

**Files:**
- Create: `llmcheck/profiles.py`
- Modify: `llmcheck/llm.py`
- Test: `tests/test_llmcheck.py`

- [ ] **Step 1: Write failing profile registry tests**

Add imports:

```python
from llmcheck.llm import build_acceptance_prompt, build_correction_prompt
from llmcheck.profiles import DEFAULT_PROFILE_ID, get_profile, list_profiles
```

Add tests:

```python
def test_default_profile_is_general_standard_document() -> None:
    profile = get_profile(DEFAULT_PROFILE_ID)

    assert profile.id == "general_standard_document"
    assert "通用" in profile.label
    assert "中医" not in profile.description


def test_profile_registry_contains_domain_presets() -> None:
    ids = [profile["id"] for profile in list_profiles()]

    assert ids[0] == "general_standard_document"
    assert "technical_manual" in ids
    assert "legal_contract" in ids
    assert "financial_report" in ids
    assert "medical_reference" in ids
    assert "chinese_medicine_reference" in ids


def test_unknown_profile_raises_clear_error() -> None:
    try:
        get_profile("not-a-profile")
    except ValueError as error:
        assert "未知文档 profile" in str(error)
        assert "general_standard_document" in str(error)
    else:
        raise AssertionError("unknown profile should fail")
```

- [ ] **Step 2: Run profile tests to verify RED**

Run:

```bash
uv run pytest tests/test_llmcheck.py -q -k "profile_registry or default_profile or unknown_profile"
```

Expected: import failure for `llmcheck.profiles`.

- [ ] **Step 3: Implement `llmcheck/profiles.py`**

Create this module:

```python
from __future__ import annotations

from dataclasses import asdict, dataclass

DEFAULT_PROFILE_ID = "general_standard_document"


@dataclass(frozen=True)
class DocumentProfile:
    id: str
    label: str
    description: str
    language_hint: str
    preservation_rules: tuple[str, ...]
    structure_rules: tuple[str, ...]
    cleanup_rules: tuple[str, ...]
    forbidden_changes: tuple[str, ...]
    acceptance_checks: tuple[str, ...]
    protected_terms: tuple[str, ...] = ()
    glue_markers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


BUILTIN_PROFILES: tuple[DocumentProfile, ...] = (
    DocumentProfile(
        id="general_standard_document",
        label="通用标准文档",
        description="适用于书籍、报告、手册、扫描档案、教材、政策材料和普通长文档。",
        language_hint="以源文档语言为准；中文文档默认保留简体/繁体原貌。",
        preservation_rules=(
            "保留源文档出现的事实、数字、日期、人名、地名、术语和页码线索。",
            "保留标题、列表、表格、引用、脚注、公式、代码块和来源证据。",
            "无法确定的文字保留原状，并写入 unresolved_issues 或验收问题。",
        ),
        structure_rules=(
            "标题与正文之间使用空行分隔。",
            "普通段落按语义合并为自然段，不保留 OCR 物理折行。",
            "列表、步骤和表格保持可读的 Markdown 结构。",
        ),
        cleanup_rules=(
            "清理乱码、替换字符、异常空格、孤立标点、重复页眉页脚和扫描噪声。",
            "修复明显 OCR 错字、缺标点、断句错误和段落粘连。",
            "跨片段合并后再检查标题层级、段落连续性和重复内容。",
        ),
        forbidden_changes=(
            "不得摘要化、改写成说明文、补写源文档未出现的信息。",
            "不得凭领域知识解释、推断或替换原文事实。",
            "不得为了通顺删除不确定但可见的源文档内容。",
        ),
        acceptance_checks=(
            "最终文本可连续阅读，没有乱码、异常空格、强制换行或明显 OCR 残留。",
            "章节、列表、表格和段落顺序符合人类阅读习惯。",
            "源文档证据被保守保留，未出现无依据扩写。",
        ),
    ),
    DocumentProfile(
        id="academic_paper",
        label="学术论文",
        description="适用于论文、研究报告、引用密集文档和公式/图表材料。",
        language_hint="保留原文语言和学术符号。",
        preservation_rules=("保留摘要、关键词、图表编号、公式、引用、参考文献和 DOI。",),
        structure_rules=("摘要、正文、注释、参考文献层次必须清晰。",),
        cleanup_rules=("清理 OCR 噪声时不得破坏引用格式、公式编号和表格编号。",),
        forbidden_changes=("不得补造引用、不得重写结论、不得改变学术限定语。",),
        acceptance_checks=("引用、公式、图表、参考文献在最终文档中保持可追踪。",),
    ),
    DocumentProfile(
        id="technical_manual",
        label="技术手册",
        description="适用于操作手册、API 文档、故障排查说明和工程规范。",
        language_hint="保留命令、路径、参数名、代码和大小写。",
        preservation_rules=("保留命令行、代码块、配置键、错误码、警告和步骤编号。",),
        structure_rules=("步骤、注意事项、输入输出示例必须分层清楚。",),
        cleanup_rules=("清理换行时不得合并代码块、命令和表格。",),
        forbidden_changes=("不得猜测命令参数，不得改写错误码或配置键。",),
        acceptance_checks=("读者可以按最终文档执行步骤，命令和代码未被文本清洗破坏。",),
    ),
    DocumentProfile(
        id="legal_contract",
        label="法律合同",
        description="适用于合同、协议、条款、制度和法律文本。",
        language_hint="保留原文法律措辞。",
        preservation_rules=("保留条款编号、主体名称、日期、金额、义务、例外和引用条款。",),
        structure_rules=("条、款、项、目编号层级必须清晰。",),
        cleanup_rules=("清理 OCR 噪声时优先保护编号、金额和主体名称。",),
        forbidden_changes=("不得解释法律含义，不得现代化或弱化义务措辞。",),
        acceptance_checks=("条款连续、编号可追踪、金额日期和主体信息未被改动。",),
    ),
    DocumentProfile(
        id="financial_report",
        label="财务报告",
        description="适用于财报、审计报告、预算、经营数据和统计表。",
        language_hint="保留原文币种、单位和期间。",
        preservation_rules=("保留表格、币种、单位、期间、百分比、括号负数和注释。",),
        structure_rules=("表格列名、行名、注释和小计/合计关系必须可读。",),
        cleanup_rules=("清理空格和换行时不得改变数字、单位、正负号和列关系。",),
        forbidden_changes=("不得补齐缺失数字，不得推算合计，不得重排财务事实。",),
        acceptance_checks=("数字、单位、期间和表格关系在最终文档中保持可核对。",),
    ),
    DocumentProfile(
        id="medical_reference",
        label="医学参考资料",
        description="适用于医学教材、病例、处方、诊疗参考和健康资料。",
        language_hint="保留原文医学术语。",
        preservation_rules=("保留病例、诊断、剂量、检查结果、处方、治疗经过和禁忌说明。",),
        structure_rules=("病例、诊断、处方、按语和治疗结果尽量分层清楚。",),
        cleanup_rules=("清理 OCR 噪声时保护药名、剂量、单位和检查指标。",),
        forbidden_changes=("不得提供医学判断，不得凭医学常识补写或纠正实质内容。",),
        acceptance_checks=("医学事实忠实可读，不含无依据补写或解释。",),
    ),
    DocumentProfile(
        id="chinese_medicine_reference",
        label="中医参考资料",
        description="适用于中医古籍、医案、方剂、针灸、运气和理论材料。",
        language_hint="保留原文术语、古今字和书名号。",
        preservation_rules=("保留医案、方剂、剂量、穴位、诊断、按语、治疗结果和页码线索。",),
        structure_rules=("医案、处方、诊断、按语、治疗结果等结构应尽量清晰。",),
        cleanup_rules=("清理 OCR 噪声时保护药名、穴位、方名、剂量和古籍术语。",),
        forbidden_changes=("不得凭中医知识补写原书未出现内容，不得现代化改写。",),
        acceptance_checks=("中医术语和结构忠实可读，未出现实质信息删改。",),
        glue_markers=("头部：", "面部：", "颈部：", "胸胁部：", "腹部：", "腰背部：", "肩部：", "肘部：", "腕手部：", "髋部：", "膝部：", "踝部：", "足部："),
    ),
)

_PROFILE_BY_ID = {profile.id: profile for profile in BUILTIN_PROFILES}


def get_profile(profile_id: str | None = None) -> DocumentProfile:
    normalized = (profile_id or DEFAULT_PROFILE_ID).strip() or DEFAULT_PROFILE_ID
    profile = _PROFILE_BY_ID.get(normalized)
    if profile is None:
        available = ", ".join(sorted(_PROFILE_BY_ID))
        raise ValueError(f"未知文档 profile: {normalized}. 可用 profile: {available}")
    return profile


def list_profiles() -> list[dict[str, object]]:
    return [profile.to_dict() for profile in BUILTIN_PROFILES]
```

- [ ] **Step 4: Run profile tests to verify GREEN**

Run:

```bash
uv run pytest tests/test_llmcheck.py -q -k "profile_registry or default_profile or unknown_profile"
```

Expected: selected tests pass.

- [ ] **Step 5: Write failing domain-neutral prompt tests**

Add tests:

```python
def test_default_prompts_are_domain_neutral() -> None:
    prompt = build_correction_prompt(source_name="manual.md", text_path=Path("manual.md"), text="第一行\n第二行")
    acceptance = build_acceptance_prompt(source_name="manual.md", text_path=Path("manual.md"), text="第一行\n第二行")

    assert "文档清洗与结构规范化编辑" in prompt
    assert "最终验收员" in acceptance
    assert "中医 Markdown 文本" not in prompt
    assert "中医 Markdown 文本" not in acceptance
    assert "不得凭医学常识" not in prompt
    assert "医案、处方" not in acceptance
    assert "乱码" in acceptance
    assert "强制换行" in acceptance


def test_profile_specific_prompt_can_preserve_chinese_medicine_rules() -> None:
    profile = get_profile("chinese_medicine_reference")
    prompt = build_correction_prompt(source_name="book.md", text_path=Path("book.md"), text="患者头痛。", profile=profile)

    assert "中医参考资料" in prompt
    assert "不得凭中医知识补写" in prompt
    assert "医案、方剂、剂量" in prompt
```

- [ ] **Step 6: Run prompt tests to verify RED**

Run:

```bash
uv run pytest tests/test_llmcheck.py -q -k "default_prompts_are_domain_neutral or profile_specific_prompt"
```

Expected: default prompt still contains old hard-coded Chinese-medicine role, and function signature lacks `profile`.

- [ ] **Step 7: Refactor `llmcheck/llm.py` prompt builders**

Implement:

```python
from llmcheck.profiles import DocumentProfile, get_profile
```

Add helper:

```python
def _profile_prompt_block(profile: DocumentProfile) -> str:
    def lines(title: str, values: tuple[str, ...]) -> str:
        if not values:
            return f"{title}\n- 无\n"
        return title + "\n" + "\n".join(f"- {value}" for value in values) + "\n"

    return (
        f"profile_id: {profile.id}\n"
        f"profile_label: {profile.label}\n"
        f"profile_description: {profile.description}\n"
        f"language_hint: {profile.language_hint}\n"
        + lines("preservation_rules:", profile.preservation_rules)
        + lines("structure_rules:", profile.structure_rules)
        + lines("cleanup_rules:", profile.cleanup_rules)
        + lines("forbidden_changes:", profile.forbidden_changes)
        + lines("acceptance_checks:", profile.acceptance_checks)
        + lines("protected_terms:", profile.protected_terms)
    )
```

Change prompt signatures:

```python
def build_correction_prompt(*, source_name: str, text_path: Path, text: str, profile: DocumentProfile | None = None) -> str:
    document_profile = profile or get_profile()
```

```python
def build_acceptance_prompt(*, source_name: str, text_path: Path, text: str, profile: DocumentProfile | None = None) -> str:
    document_profile = profile or get_profile()
```

Prompt role text must say:

```python
"你是文档清洗与结构规范化编辑。任务：通读本次输入文本，修正 OCR/清洗残留造成的错别字、缺标点、异常分段、正文粘连、乱码、异常空格和强制换行。\n"
```

Acceptance role text must say:

```python
"你是标准文档最终验收员。请通读本次输入文本，判断该文本是否可以交付给后续知识抽取和人工阅读；如果源文件名显示为第 N/M 片段，只验收该片段，不要因为片段边界判定为截断。\n"
```

Both prompts must include `_profile_prompt_block(document_profile)` and quality hints.

- [ ] **Step 8: Route payload builders through profile**

Change these signatures and calls in `llmcheck/llm.py`:

```python
def correction_result_payload(..., profile: DocumentProfile | None = None) -> dict[str, Any]:
    document_profile = profile or get_profile()
    prompt = build_correction_prompt(..., profile=document_profile)
    ...
    "profile_id": document_profile.id,
```

```python
def acceptance_result_payload(..., profile: DocumentProfile | None = None) -> dict[str, Any]:
    document_profile = profile or get_profile()
    prompt = build_acceptance_prompt(..., profile=document_profile)
    ...
    "profile_id": document_profile.id,
```

```python
def build_repair_prompt(..., profile: DocumentProfile | None = None) -> str:
    document_profile = profile or get_profile()
```

```python
def repair_result_payload(..., profile: DocumentProfile | None = None) -> dict[str, Any]:
    document_profile = profile or get_profile()
    prompt = build_repair_prompt(..., profile=document_profile)
    ...
    "profile_id": document_profile.id,
```

- [ ] **Step 9: Run prompt/profile tests**

Run:

```bash
uv run pytest tests/test_llmcheck.py -q -k "profile or prompt"
```

Expected: selected tests pass.

## Task 2: Deterministic Quality Gates And Finalization

**Files:**
- Modify: `llmcheck/quality.py`
- Test: `tests/test_llmcheck.py`

- [ ] **Step 1: Write failing quality-gate tests**

Add imports:

```python
from llmcheck.quality import final_acceptance_report, finalize_standard_document
```

Add tests:

```python
def test_quality_errors_blocks_mojibake_and_replacement_characters() -> None:
    errors = quality_errors("这是锟斤拷文本，含有�替换符。\n")

    assert "mojibake" in errors
    assert "replacement_characters" in errors


def test_quality_errors_blocks_forced_line_break_fragments() -> None:
    text = "这是一个普通段落的第一部分\n第二部分仍然是同一个句子\n第三部分才结束。\n"

    assert "forced_line_breaks" in quality_errors(text)


def test_finalize_standard_document_removes_duplicate_running_headers_and_reports_changes() -> None:
    text = "# 标题\n\n扫描页眉\n正文第一段。\n\n扫描页眉\n正文第二段。\n"

    result = finalize_standard_document(text)

    assert result["finalized"] is True
    assert result["text"].count("扫描页眉") == 0
    assert any(change["kind"] == "removed_repeated_lines" for change in result["changes"])


def test_final_acceptance_report_blocks_visible_artifacts() -> None:
    report = final_acceptance_report("# 标题\n\n这是锟斤拷\n")

    assert report["accepted"] is False
    assert report["blocking_errors"]
    assert "mojibake" in report["blocking_errors"]
```

- [ ] **Step 2: Run quality tests to verify RED**

Run:

```bash
uv run pytest tests/test_llmcheck.py -q -k "mojibake or forced_line_break_fragments or finalize_standard_document or final_acceptance_report"
```

Expected: missing functions and/or missing quality codes.

- [ ] **Step 3: Extend `quality_errors` and `quality_hints`**

Add deterministic patterns:

```python
MOJIBAKE_PATTERNS = ("锟斤拷", "Ã", "Â", "â€™", "â€œ", "â€�")
ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")
FORCED_LINE_BREAK_RE = re.compile(r"(?<![。！？.!?：:；;，,、])\n(?!\n|#{1,6}\s|[-*+]\s|\d+[.)、]\s|\|)")
```

Extend `quality_errors(text: str) -> list[str]`:

```python
if any(pattern in text for pattern in MOJIBAKE_PATTERNS):
    errors.append("mojibake")
if text.count("�") >= 1:
    errors.append("replacement_characters")
if ZERO_WIDTH_RE.search(text):
    errors.append("zero_width_characters")
if re.search(r"[\u4e00-\u9fff] {2,}[\u4e00-\u9fff]", text):
    errors.append("abnormal_cjk_spaces")
if FORCED_LINE_BREAK_RE.search(text):
    errors.append("forced_line_breaks")
if _has_repeated_short_lines(text):
    errors.append("duplicate_repeated_lines")
```

Add helper:

```python
def _has_repeated_short_lines(text: str) -> bool:
    counts: dict[str, int] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if 3 <= len(line) <= 40 and not line.startswith(("#", "|")):
            counts[line] = counts.get(line, 0) + 1
    return any(count >= 3 for count in counts.values())
```

Extend `quality_hints` to include readable messages for the new error codes.

- [ ] **Step 4: Add finalization helpers**

Implement:

```python
def finalize_standard_document(text: str) -> dict[str, object]:
    before = clean_markdown_text(text)
    without_repeated, removed_lines = _remove_repeated_running_lines(before)
    finalized = clean_markdown_text(_normalize_heading_spacing(without_repeated))
    changes: list[dict[str, object]] = []
    if removed_lines:
        changes.append({"kind": "removed_repeated_lines", "lines": removed_lines})
    if finalized != before:
        changes.append({"kind": "normalized_spacing"})
    return {
        "status": "finalized",
        "finalized": finalized != before,
        "input_sha256": _text_sha256(before),
        "output_sha256": _text_sha256(finalized),
        "changes": changes,
        "text": finalized,
    }
```

```python
def final_acceptance_report(text: str) -> dict[str, object]:
    errors = quality_errors(text)
    blocking = [
        error for error in errors
        if error in {
            "bad_control_characters",
            "mojibake",
            "replacement_characters",
            "zero_width_characters",
            "abnormal_cjk_spaces",
            "forced_line_breaks",
            "duplicate_repeated_lines",
        }
    ]
    return {
        "status": "passed" if not blocking else "needs_revision",
        "accepted": not blocking,
        "blocking_errors": blocking,
        "warnings": [error for error in errors if error not in blocking],
        "hints": quality_hints(text),
    }
```

Add helpers:

```python
def _remove_repeated_running_lines(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    counts: dict[str, int] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if 3 <= len(line) <= 40 and not line.startswith(("#", "|", "-", "*")):
            counts[line] = counts.get(line, 0) + 1
    repeated = {line for line, count in counts.items() if count >= 2}
    output = [raw_line for raw_line in lines if raw_line.strip() not in repeated]
    return "\n".join(output) + ("\n" if output else ""), sorted(repeated)


def _normalize_heading_spacing(text: str) -> str:
    normalized = re.sub(r"\n(#{1,6}\s+[^\n]+)\n(?!\n)", r"\n\1\n\n", text)
    normalized = re.sub(r"(?<!\n)\n(#{1,6}\s+[^\n]+)", r"\n\n\1", normalized)
    return normalized


def _text_sha256(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
```

- [ ] **Step 5: Run quality tests to verify GREEN**

Run:

```bash
uv run pytest tests/test_llmcheck.py -q -k "quality_errors or finalize_standard_document or final_acceptance_report"
```

Expected: selected tests pass, existing quality tests stay green.

## Task 3: Pipeline Profile Integration And Whole-Document Final Gate

**Files:**
- Modify: `llmcheck/pipeline.py`
- Test: `tests/test_llmcheck.py`

- [ ] **Step 1: Write failing pipeline tests**

Add test:

```python
def test_process_documents_records_profile_and_final_reports(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "input"
    source.mkdir()
    (source / "manual.md").write_text("# 标题\n\n正文第一段。\n", encoding="utf-8")

    def fake_write_text_pdf(path: Path, *, title: str, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr("llmcheck.pipeline.write_text_pdf", fake_write_text_pdf)

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="model",
            profile_id="technical_manual",
        ),
        client=FakeClient(),
    )

    row = report["documents"][0]
    assert report["profile_id"] == "technical_manual"
    assert row["profile_id"] == "technical_manual"
    assert Path(row["finalization_report_path"]).exists()
    assert Path(row["final_acceptance_report_path"]).exists()
    assert json.loads(Path(row["final_acceptance_report_path"]).read_text(encoding="utf-8"))["accepted"] is True
```

Add test:

```python
def test_process_documents_blocks_final_output_when_final_acceptance_fails(tmp_path: Path) -> None:
    source = tmp_path / "input"
    source.mkdir()
    (source / "bad.md").write_text("这是锟斤拷文本。\n", encoding="utf-8")

    class BadFinalClient(FakeClient):
        def complete_json(self, prompt: str) -> dict[str, object]:
            if '"corrected_text"' in prompt:
                return {
                    "status": "draft_ready",
                    "confidence": 0.9,
                    "summary": "返回含乱码文本",
                    "corrected_text": "这是锟斤拷文本。\n",
                    "changes": [],
                    "unresolved_issues": [],
                }
            return {
                "status": "passed",
                "confidence": 0.9,
                "summary": "片段可交付",
                "blocking_issues": [],
                "non_blocking_notes": [],
            }

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        client=BadFinalClient(),
    )

    row = report["documents"][0]
    assert row["status"] == "final_acceptance_failed"
    assert row["final_markdown_path"] == ""
    assert not list((tmp_path / "out" / "md").glob("*.md"))
```

- [ ] **Step 2: Run pipeline tests to verify RED**

Run:

```bash
uv run pytest tests/test_llmcheck.py -q -k "records_profile_and_final_reports or final_acceptance_fails"
```

Expected: `LlmCheckSettings` lacks `profile_id`, `DocumentResult` lacks final report fields, and final gate is absent.

- [ ] **Step 3: Extend settings and result dataclasses**

In `LlmCheckSettings`, add:

```python
profile_id: str = DEFAULT_PROFILE_ID
```

Import:

```python
from llmcheck.profiles import DEFAULT_PROFILE_ID, get_profile
from llmcheck.quality import final_acceptance_report, finalize_standard_document
```

In `DocumentResult`, add:

```python
profile_id: str = ""
finalization_report_path: str = ""
final_acceptance_report_path: str = ""
```

- [ ] **Step 4: Pass profile through correction, acceptance, and repair**

In `process_one_document`, resolve:

```python
profile = get_profile(settings.profile_id)
```

Pass `profile=profile` to `correct_text_concurrently`, `accept_text_concurrently`, and `repair_text_concurrently`. Update these helper signatures so each worker call forwards the profile to `correction_result_payload`, `acceptance_result_payload`, and `repair_result_payload`.

- [ ] **Step 5: Add whole-document finalization before final writes**

After the accepted `corrected_text` is stable and before writing `final_path`, add:

```python
finalization = finalize_standard_document(corrected_text)
_write_json(finalization_report, finalization)
final_text = str(finalization.get("text") or corrected_text)
final_acceptance = final_acceptance_report(final_text)
_write_json(final_acceptance_report_path, final_acceptance)
if final_acceptance.get("accepted") is not True:
    draft_path.write_text(final_text, encoding="utf-8")
    return DocumentResult(
        document_id=document_id,
        source_path=str(path),
        status="final_acceptance_failed",
        draft_path=str(draft_path),
        correction_report_path=str(correction_report),
        acceptance_report_path=str(acceptance_report),
        repair_report_path=str(repair_report) if repair_report.exists() else "",
        profile_id=profile.id,
        finalization_report_path=str(finalization_report),
        final_acceptance_report_path=str(final_acceptance_report_path),
        error="whole-document final acceptance failed",
    )
corrected_text = final_text
```

Use report paths:

```python
finalization_report = reports_dir / f"{document_id}.finalization.json"
final_acceptance_report_path = reports_dir / f"{document_id}.final_acceptance.json"
```

- [ ] **Step 6: Include profile in summary**

In `process_documents` summary:

```python
"profile_id": get_profile(settings.profile_id).id,
```

- [ ] **Step 7: Run pipeline tests to verify GREEN**

Run:

```bash
uv run pytest tests/test_llmcheck.py -q -k "process_documents"
```

Expected: process-document tests pass.

## Task 4: CLI And GUI Profile Selection

**Files:**
- Modify: `llmcheck/cli.py`
- Modify: `llmcheck/gui.py`
- Test: `tests/test_llmcheck.py`

- [ ] **Step 1: Write failing CLI/GUI tests**

Add tests:

```python
def test_cli_profiles_command_lists_builtin_profiles(capsys) -> None:
    code = main(["profiles"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["default_profile_id"] == "general_standard_document"
    assert any(profile["id"] == "technical_manual" for profile in payload["profiles"])
```

```python
def test_cli_run_accepts_profile_argument(tmp_path: Path, monkeypatch, capsys) -> None:
    source = tmp_path / "input.md"
    source.write_text("# 标题\n\n正文。\n", encoding="utf-8")
    seen: dict[str, str] = {}

    def fake_process_documents(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings):
        seen["profile_id"] = settings.profile_id
        return {"status": "passed", "documents": [], "profile_id": settings.profile_id}

    monkeypatch.setattr("llmcheck.cli.process_documents", fake_process_documents)

    code = main([
        "run",
        "--input", str(source),
        "--output-dir", str(tmp_path / "out"),
        "--llm-api-url", "http://llm.test",
        "--llm-api-key", "key",
        "--llm-model", "model",
        "--profile", "technical_manual",
    ])

    assert code == 0
    assert seen["profile_id"] == "technical_manual"
```

```python
def test_gui_html_contains_profile_selector() -> None:
    html = render_index_html()

    assert 'name="profile"' in html
    assert "general_standard_document" in html
    assert "technical_manual" in html
```

- [ ] **Step 2: Run CLI/GUI tests to verify RED**

Run:

```bash
uv run pytest tests/test_llmcheck.py -q -k "profiles_command or run_accepts_profile or profile_selector"
```

Expected: command/argument/HTML selector are absent.

- [ ] **Step 3: Add CLI profile support**

Import:

```python
from llmcheck.profiles import DEFAULT_PROFILE_ID, get_profile, list_profiles
```

Add command:

```python
profiles = subparsers.add_parser("profiles", help="List built-in document profiles.")
```

Add to `run` and `batch` parsers:

```python
run.add_argument("--profile", default=DEFAULT_PROFILE_ID, choices=[profile["id"] for profile in list_profiles()])
batch.add_argument("--profile", default=DEFAULT_PROFILE_ID, choices=[profile["id"] for profile in list_profiles()])
```

Handle command:

```python
if args.command == "profiles":
    print(json.dumps({"default_profile_id": DEFAULT_PROFILE_ID, "profiles": list_profiles()}, ensure_ascii=False, indent=2))
    return 0
```

Set `profile_id=get_profile(args.profile).id` in both `run` settings and `_settings_from_args`.

- [ ] **Step 4: Add GUI profile selector**

Import:

```python
from llmcheck.profiles import DEFAULT_PROFILE_ID, get_profile, list_profiles
```

In `create_job`, set:

```python
profile_id=get_profile(str(payload.get("profile") or DEFAULT_PROFILE_ID)).id,
```

In `render_index_html`, add a `<select name="profile" id="profile">` populated from `list_profiles()` with labels. Include `profile: form.profile.value` in the JavaScript payload.

- [ ] **Step 5: Run CLI/GUI tests to verify GREEN**

Run:

```bash
uv run pytest tests/test_llmcheck.py -q -k "profiles_command or run_accepts_profile or profile_selector"
```

Expected: selected tests pass.

## Task 5: Documentation, Skill Package, GUI Exe, And Verification

**Files:**
- Modify: `README.md`
- Modify: `skill/SKILL.md`
- Modify: `skill/README.md`
- Modify generated artifacts: `dist/llmcheck-skill-0.1.1.tar.gz`, `dist/LLMcheck-GUI-0.1.1.exe`
- Test: `tests/test_llmcheck.py`

- [ ] **Step 1: Update README**

README must describe:

- LLMcheck as a domain-neutral document conversion, cleanup, finalization, and standard-document delivery tool.
- Supported profiles: `general_standard_document`, `academic_paper`, `technical_manual`, `legal_contract`, `financial_report`, `medical_reference`, `chinese_medicine_reference`.
- Output folders: `md/`, `文字版pdf/`, `process/`.
- Process reports: `*.quality.json`, `*.llm_correction.json`, `*.llm_acceptance.json`, `*.local_repair.json`, `*.llm_repair*.json`, `*.finalization.json`, `*.final_acceptance.json`, `llmcheck_summary.json`, `llmcheck_manifest.jsonl`.
- CLI examples with `--profile`.
- GUI exe and skill package locations.
- GitHub URL: `https://github.com/oushixingfu/LLMcheck`.
- Other-agent entry point: repo URL, local root, skill package path, and command examples.

- [ ] **Step 2: Update skill docs**

`skill/SKILL.md` and `skill/README.md` must describe the agent workflow in profile-neutral language:

```bash
llmcheck profiles
llmcheck run --profile general_standard_document --input <file-or-dir> --output-dir <output> ...
llmcheck batch --profile technical_manual --source-dir <books> --output-dir <output> ...
```

They must mention final output is valid only when summary status is `passed` and final acceptance report is accepted.

- [ ] **Step 3: Run full focused test suite**

Run:

```bash
uv run pytest tests/test_llmcheck.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Rebuild skill package**

Run the existing package command or a deterministic archive command that includes `skill/SKILL.md`, `skill/README.md`, and project usage docs. Verify:

```bash
python - <<'PY'
import tarfile
from pathlib import Path
path = Path("dist/llmcheck-skill-0.1.1.tar.gz")
with tarfile.open(path, "r:gz") as tar:
    names = tar.getnames()
print(path, path.stat().st_size, any(name.endswith("SKILL.md") for name in names), any(name.endswith("README.md") for name in names))
PY
```

Expected: archive exists, size is nonzero, both `SKILL.md` and `README.md` are present.

- [ ] **Step 5: Rebuild GUI exe**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_windows_gui_exe.ps1
```

Expected: `dist/LLMcheck-GUI-0.1.1.exe` exists and `dist/LLMcheck-GUI-0.1.1.exe --help` prints CLI help.

- [ ] **Step 6: Smoke-test package behavior**

Run:

```bash
uv run python -m llmcheck.cli profiles
uv run python -m llmcheck.cli run --help
uv run python -m llmcheck.cli batch --help
```

Expected: commands show profile support and exit successfully.

- [ ] **Step 7: Final repository review**

Run:

```bash
git status --short
git diff --check
```

Expected: no whitespace errors. Review changed files for unrelated edits.

- [ ] **Step 8: Git checkpoint gated by user rule**

Because the project AGENTS.md requires explicit confirmation for git commits and pushes, do not run `git commit` or `git push` unless the user has explicitly confirmed this exact checkpoint. If confirmed, use:

```bash
git add llmcheck tests README.md skill docs dist
git commit -m "feat: generalize document profiles and final quality gates"
git push origin main
```

Expected: GitHub repo `https://github.com/oushixingfu/LLMcheck` contains the new implementation.

## Self-Review

- Spec coverage: profile-driven generalization is covered by Tasks 1, 3, and 4; human-readable cleanup and final artifact gates are covered by Tasks 2 and 3; CLI/GUI/docs/package delivery is covered by Tasks 4 and 5.
- Placeholder scan: no `TBD`, `TODO`, or undefined future work remains in task instructions.
- Type consistency: `profile_id`, `DocumentProfile`, `DEFAULT_PROFILE_ID`, `get_profile`, `list_profiles`, `finalize_standard_document`, and `final_acceptance_report` are consistently named across tests and implementation steps.
