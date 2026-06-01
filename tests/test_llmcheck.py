from __future__ import annotations

from pathlib import Path
import http.client
import json
import os
import subprocess
import threading
import urllib.error
import zipfile

from llmcheck.batch import discover_batch_sources, run_batch
from llmcheck import gui_exe
from llmcheck.cli import _print_progress_event, main
from llmcheck.llm import LlmClient, LlmConfig
from llmcheck.gui import JobStore, _read_mineru_segment_status, _save_uploaded_files, _with_live_progress, render_index_html
from llmcheck.pipeline import LlmCheckSettings, correct_text_concurrently, process_documents
from llmcheck.pipeline import split_text_chunks
from llmcheck.preprocess import (
    MinerUClient,
    MinerUTransientError,
    PreprocessSettings,
    SourceKind,
    _run_one_mineru_file,
    _write_mineru_segment_status,
    discover_source_files,
    page_segments,
    prepare_markdown_inputs,
    run_mineru_vlm,
    run_mineru_vlm_for_pdf_chunks,
    run_ppx,
)
from llmcheck.quality import clean_markdown_text, repair_acceptance_locally, quality_errors


class FakeClient:
    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.calls: list[str] = []

    def complete_json(self, prompt: str) -> dict[str, object]:
        self.calls.append(prompt)
        if '"corrected_text"' in prompt:
            return {
                "status": "draft_ready",
                "confidence": 0.9,
                "summary": "已完成纠错",
                "corrected_text": "# 医案\n\n患者头痛，处方桂枝汤。\n",
                "changes": [],
                "unresolved_issues": [],
            }
        return {
            "status": "passed" if self.accept else "needs_revision",
            "confidence": 0.9,
            "summary": "可交付" if self.accept else "仍需返修",
            "blocking_issues": [] if self.accept else [{"category": "layout", "reason": "分段异常"}],
            "non_blocking_notes": [],
        }


class EchoChunkClient:
    def __init__(self) -> None:
        self.correction_calls = 0
        self.acceptance_calls = 0

    def complete_json(self, prompt: str) -> dict[str, object]:
        if '"corrected_text"' in prompt:
            self.correction_calls += 1
            text = prompt.split("<TEXT_BEGIN>\n", 1)[1].split("\n<TEXT_END>", 1)[0]
            return {
                "status": "draft_ready",
                "confidence": 0.8,
                "summary": "片段已纠错",
                "corrected_text": text.replace("错字", "正字"),
                "changes": [],
                "unresolved_issues": [],
            }
        self.acceptance_calls += 1
        return {
            "status": "passed",
            "confidence": 0.8,
            "summary": "片段可交付",
            "blocking_issues": [],
            "non_blocking_notes": [],
        }


class ExplodingClient:
    def complete_json(self, prompt: str) -> dict[str, object]:
        raise AssertionError("cached chunks should not call LLM")


class ReviewButCorrectedClient:
    def complete_json(self, prompt: str) -> dict[str, object]:
        if '"corrected_text"' in prompt:
            text = prompt.split("<TEXT_BEGIN>\n", 1)[1].split("\n<TEXT_END>", 1)[0]
            return {
                "status": "needs_manual_review",
                "confidence": 0.8,
                "summary": "有疑点但已给出完整纠正文",
                "corrected_text": text.replace("错字", "正字"),
                "changes": [],
                "unresolved_issues": [{"location_hint": "片段", "excerpt": "疑点", "reason": "保留给验收"}],
            }
        return {
            "status": "passed",
            "confidence": 0.8,
            "summary": "可交付",
            "blocking_issues": [],
            "non_blocking_notes": [],
        }


class EmptyMarkupResidueClient:
    def complete_json(self, prompt: str) -> dict[str, object]:
        text = prompt.split("<TEXT_BEGIN>\n", 1)[1].split("\n<TEXT_END>", 1)[0]
        corrected = "" if text.strip() == "<td></td></tr></table>" else text.replace("错字", "正字")
        return {
            "status": "draft_ready",
            "confidence": 0.9,
            "summary": "已清理片段",
            "corrected_text": corrected,
            "changes": [],
            "unresolved_issues": [],
        }


def test_llm_client_retries_invalid_json_message_content(monkeypatch) -> None:
    responses = [
        json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"status":"draft_ready","confidence":0.9,"summary":"坏 JSON","corrected_text":"缺个引号}'
                        }
                    }
                ]
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "status": "draft_ready",
                                    "confidence": 0.9,
                                    "summary": "第二次成功",
                                    "corrected_text": "修正后的文本",
                                    "changes": [],
                                    "unresolved_issues": [],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
            ensure_ascii=False,
        ),
    ]
    calls = 0

    class FakeResponse:
        def __init__(self, body: str) -> None:
            self.body = body

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return self.body.encode("utf-8")

    def fake_urlopen(*args: object, **kwargs: object) -> FakeResponse:
        nonlocal calls
        body = responses[min(calls, len(responses) - 1)]
        calls += 1
        return FakeResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("llmcheck.llm.time.sleep", lambda *_args, **_kwargs: None)

    client = LlmClient(LlmConfig(api_url="http://llm.test", api_key="key", model="model", timeout_seconds=1))
    result = client.complete_json("请返回 JSON")

    assert result["status"] == "draft_ready"
    assert result["corrected_text"] == "修正后的文本"
    assert calls == 2


class RepairingClient:
    def complete_json(self, prompt: str) -> dict[str, object]:
        if '"corrected_text"' in prompt:
            text = prompt.split("<TEXT_BEGIN>\n", 1)[1].split("\n<TEXT_END>", 1)[0]
            return {
                "status": "draft_ready",
                "confidence": 0.9,
                "summary": "先返回待验收文本",
                "corrected_text": text,
                "changes": [],
                "unresolved_issues": [],
            }
        if '"repaired_text"' in prompt:
            text = prompt.split("<TEXT_BEGIN>\n", 1)[1].split("\n<TEXT_END>", 1)[0]
            return {
                "status": "repaired",
                "confidence": 0.9,
                "summary": "已补齐固定配属缺项",
                "repaired_text": text.replace("少阳主相火之气，太阳主寒水之气", "少阳主相火之气，阳明主燥金之气，太阳主寒水之气"),
                "changes": [{"location_hint": "客气六步", "before": "少阳主相火之气，太阳主寒水之气", "after": "少阳主相火之气，阳明主燥金之气，太阳主寒水之气", "reason": "上下文列出五阳明"}],
                "unresolved_issues": [],
            }
        text = prompt.split("<TEXT_BEGIN>\n", 1)[1].split("\n<TEXT_END>", 1)[0]
        if "阳明主燥金之气" in text:
            return {
                "status": "passed",
                "confidence": 0.9,
                "summary": "可交付",
                "blocking_issues": [],
                "non_blocking_notes": [],
            }
        return {
            "status": "needs_revision",
            "confidence": 0.9,
            "summary": "缺少阳明燥金配属",
            "blocking_issues": [{"category": "missing_text", "severity": "medium", "location_hint": "客气六步", "excerpt": "少阳主相火之气，太阳主寒水之气", "reason": "缺少阳明主燥金之气", "suggested_action": "补齐固定配属"}],
            "non_blocking_notes": [],
        }


class TwoRoundRepairClient:
    def __init__(self) -> None:
        self.repair_calls = 0

    def complete_json(self, prompt: str) -> dict[str, object]:
        text = prompt.split("<TEXT_BEGIN>\n", 1)[1].split("\n<TEXT_END>", 1)[0]
        if '"corrected_text"' in prompt:
            return {
                "status": "draft_ready",
                "confidence": 0.9,
                "summary": "返回待验收文本",
                "corrected_text": text,
                "changes": [],
                "unresolved_issues": [],
            }
        if '"repaired_text"' in prompt:
            self.repair_calls += 1
            repaired_text = text.replace("甲。", "甲，乙。") if self.repair_calls == 1 else text.replace("甲，乙。", "甲，乙，丙。")
            return {
                "status": "repaired",
                "confidence": 0.9,
                "summary": "递进返修",
                "repaired_text": repaired_text,
                "changes": [],
                "unresolved_issues": [],
            }
        if "甲，乙，丙" in text:
            return {
                "status": "passed",
                "confidence": 0.9,
                "summary": "可交付",
                "blocking_issues": [],
                "non_blocking_notes": [],
            }
        return {
            "status": "needs_revision",
            "confidence": 0.9,
            "summary": "仍缺少后续文本",
            "blocking_issues": [{"category": "missing_text", "severity": "medium", "location_hint": "正文", "excerpt": text, "reason": "缺少丙", "suggested_action": "继续补齐"}],
            "non_blocking_notes": [],
        }


def test_clean_markdown_text_merges_forced_line_breaks() -> None:
    text = "患者头痛发热脉浮\n处方桂枝汤加减治疗。\n"

    cleaned = clean_markdown_text(text)

    assert "脉浮处方" in cleaned
    assert "bad_control_characters" not in quality_errors(cleaned)


def test_clean_markdown_text_removes_mineru_generated_details_noise() -> None:
    text = "正文。\n\n<details>\n<summary>line</summary>\n\n| x | y |\n|---|---|\n| 0 | 1 |\n</details>\n\n<details>\n<summary>flowchart</summary>\nA --> B\n</details>\n\n<details>\n<summary>natural_image</summary>\nEnglish image description.\n</details>\n\n<details>\n<summary>radar</summary>\nNoise chart text.\n</details>\n"

    cleaned = clean_markdown_text(text)

    assert "<summary>line</summary>" not in cleaned
    assert "| x | y |" not in cleaned
    assert "<summary>flowchart</summary>" not in cleaned
    assert "<summary>natural_image</summary>" not in cleaned
    assert "<summary>radar</summary>" not in cleaned
    assert "English image description" not in cleaned


def test_clean_markdown_text_transcribes_table_image_flowchart_details() -> None:
    text = """表2 常用方剂表

![表2](images/table2.png)

<details>
<summary>flowchart</summary>

```mermaid
flowchart TD
    A["方名"] --> B["出处"]
    A --> C["组成"]
    D["桂枝汤"] --> E["《伤寒论》"]
```

</details>
"""

    cleaned = clean_markdown_text(text)

    assert "![表2](images/table2.png)" in cleaned
    assert "<summary>flowchart</summary>" not in cleaned
    assert "表格结构转写（由 MinerU flowchart 提取）" in cleaned
    assert "| 方名 | 出处 |" in cleaned
    assert "| 桂枝汤 | 《伤寒论》 |" in cleaned


def test_clean_markdown_text_converts_html_table_residue() -> None:
    text = "前文\n<tr><td>茅根</td><td>凉血止血</td><td>9～18克</td></tr>\n血止血。</td><td>血热出血证</td><td>3～9克</td></tr>\n后文"

    cleaned = clean_markdown_text(text)

    assert "<td>" not in cleaned
    assert "</tr>" not in cleaned
    assert "| 茅根 | 凉血止血 | 9～18克 |" in cleaned
    assert "血热出血证 | 3～9克 |" in cleaned


def test_clean_markdown_text_normalizes_broken_markdown_tables() -> None:
    text = "| 方名 | 出处 | 组成 | 功效 | 主治 | 用法 |\n\n| 少腹逐瘀汤 | 《医林改错》 | 小茴香。 | 活血祛瘀。 | 少腹肿\n\n痛。 | 水煎服。 |\n| 跌打丸 | 《处方集》 | 当归。 | 活血。 | 方名 | 出处 | 组成 | 功效 | 主治 | 用法 |\n| 跌打丸 | 《处方集》 | 当归。 | 活血。 | 跌打损伤。 | 蜜丸。 |\n"

    cleaned = clean_markdown_text(text)

    assert "| 方名 | 出处 | 组成 | 功效 | 主治 | 用法 |\n|---|---|---|---|---|---|" in cleaned
    assert "| 少腹逐瘀汤 | 《医林改错》 | 小茴香。 | 活血祛瘀。 | 少腹肿 痛。 | 水煎服。 |" in cleaned
    assert "| 跌打丸 | 《处方集》 | 当归。 | 活血。 | 方名 | 出处 | 组成 | 功效 | 主治 | 用法 |" not in cleaned
    assert "| 跌打丸 | 《处方集》 | 当归。 | 活血。 | 跌打损伤。 | 蜜丸。 |" in cleaned


def test_clean_markdown_text_merges_table_continuation_rows() -> None:
    text = "| 分类 | 药名 | 功效 | 主治 | 用量 |\n| 强筋壮骨药 | 虎骨 |  |  |  |\n| 散风寒、健筋骨。 | 关节走注疼痛。 | 3～9克 |\n"

    cleaned = clean_markdown_text(text)

    assert "| 强筋壮骨药 | 虎骨 | 散风寒、健筋骨。 | 关节走注疼痛。 | 3～9克 |" in cleaned
    assert "| 散风寒、健筋骨。 | 关节走注疼痛。 | 3～9克 |" not in cleaned.splitlines()


def test_clean_markdown_text_drops_short_table_fragments() -> None:
    text = "| 方名 | 出处 | 组成 | 功效 | 主治 | 用法 |\n| 热熨药 | 《正骨经验》 | 当归。 | 活血。 | 骨折、脱位。 | 敷患处1小时。 |\n| 骨折、脱位。 | 敷患处1小时。 |\n"

    cleaned = clean_markdown_text(text)

    assert "| 热熨药 | 《正骨经验》 | 当归。 | 活血。 | 骨折、脱位。 | 敷患处1小时。 |" in cleaned
    assert "| 骨折、脱位。 | 敷患处1小时。 |" not in cleaned.splitlines()


def test_clean_markdown_text_resets_table_state_between_sections() -> None:
    text = "| 方名 | 出处 | 组成 | 功效 | 主治 | 用法 |\n| 跌打丸 | 《处方集》 | 当归。 | 活血。 | 跌打损伤。 | 蜜丸。 |\n\n# 髋脱位症状表\n\n<table><tr><td>部位项目</td><td>外形</td><td>肢体长度</td><td>股骨头</td><td>髂坐线</td></tr><tr><td>后脱位</td><td>髋关节内收</td><td>短缩</td><td>臀部摸到</td><td>大粗隆越过此线</td></tr></table>\n"

    cleaned = clean_markdown_text(text)

    assert "| 部位项目 | 外形 | 肢体长度 | 股骨头 | 髂坐线 |" in cleaned
    assert "| 后脱位 | 髋关节内收 | 短缩 | 臀部摸到 | 大粗隆越过此线 |" in cleaned


def test_clean_markdown_text_splits_local_structure_glue() -> None:
    text = "颈部：风池 天柱 大杼 后溪肩部：肩髃 肩井肘部：曲池 手三里腕部：阳池 外关"

    cleaned = clean_markdown_text(text)

    assert "后溪\n肩部：肩髃" in cleaned
    assert "肩井\n肘部：曲池" in cleaned
    assert "手三里\n腕部：阳池" in cleaned


def test_clean_markdown_text_splits_bibliography_glue() -> None:
    text = "《叶熙春医案》，人民卫生出版社1965年版赵守真《治验回忆录》，人民卫生出版社1962年版《中医杂志》"

    cleaned = clean_markdown_text(text)

    assert "1965年版\n赵守真《治验回忆录》" in cleaned
    assert "1962年版\n《中医杂志》" in cleaned


def test_repair_acceptance_locally_targets_failed_layout_excerpt() -> None:
    text = "颈部：风池 天柱 大杼 后溪肩部：肩髃 肩井肘部：曲池 手三里腕部：阳池 外关\n"
    acceptance = {
        "accepted": False,
        "chunks": [
            {
                "accepted": False,
                "llm_result": {
                    "blocking_issues": [
                        {
                            "category": "layout",
                            "location_hint": "取穴表",
                            "excerpt": text.strip(),
                            "reason": "局部结构粘连，部位标签没有断开",
                        }
                    ]
                },
            }
        ],
    }

    repair = repair_acceptance_locally(text, acceptance)

    assert repair["repaired"] is True
    assert repair["issue_count"] == 1
    assert "后溪\n肩部：肩髃" in repair["repaired_text"]


def test_process_documents_writes_final_markdown_pdf_and_reports(tmp_path: Path) -> None:
    source = tmp_path / "input"
    source.mkdir()
    (source / "book.md").write_text("患者头痛发热脉浮\n处方桂枝汤加减治疗。", encoding="utf-8")

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="model",
            concurrency=2,
        ),
        client=FakeClient(),
    )

    assert report["status"] == "passed"
    row = report["documents"][0]
    final_md = Path(row["final_markdown_path"])
    text_pdf = Path(row["text_pdf_path"])
    assert final_md == tmp_path / "out" / "md" / "book.md"
    assert text_pdf == tmp_path / "out" / "文字版pdf" / "book.pdf"
    assert final_md.read_text(encoding="utf-8").startswith("# 医案")
    assert text_pdf.read_bytes().startswith(b"%PDF-")
    assert Path(row["correction_report_path"]).exists()
    assert Path(row["acceptance_report_path"]).exists()
    summary = json.loads((tmp_path / "out" / "process" / "reports" / "llmcheck_summary.json").read_text(encoding="utf-8"))
    assert summary["passed_count"] == 1
    assert (tmp_path / "out" / "process" / "drafts" / "book.md").exists()
    assert not (tmp_path / "out" / "final_markdown").exists()
    assert not (tmp_path / "out" / "text_pdfs").exists()


def test_process_documents_keeps_draft_but_not_final_when_acceptance_fails(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    source.write_text("正文。", encoding="utf-8")

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="model",
        ),
        client=FakeClient(accept=False),
    )

    row = report["documents"][0]
    assert report["status"] == "review_required"
    assert row["status"] == "acceptance_failed"
    assert Path(row["draft_path"]).exists()
    assert row["final_markdown_path"] == ""
    assert not list((tmp_path / "out" / "md").glob("*.md"))


def test_process_documents_uses_preprocessed_markdown_inputs(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    generated = tmp_path / "generated.md"
    generated.write_text("患者头痛发热脉浮\n处方桂枝汤加减治疗。", encoding="utf-8")
    pdf_titles: list[str] = []

    def fake_write_text_pdf(path: Path, *, title: str, text: str) -> None:
        pdf_titles.append(title)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr("llmcheck.pipeline.write_text_pdf", fake_write_text_pdf)

    def fake_preprocess(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> list[Path]:
        assert input_path == source
        assert settings.mineru_model == "vlm"
        assert settings.pdf_page_chunk_size == 30
        assert settings.mineru_request_timeout_seconds == 60
        return [generated]

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="model",
            mineru_api_key="mineru-key",
        ),
        client=FakeClient(),
        preprocess_runner=fake_preprocess,
    )

    assert report["status"] == "passed"
    assert report["documents"][0]["source_path"] == str(generated)
    assert pdf_titles == ["source"]


def test_process_documents_chunks_llm_correction_and_acceptance(tmp_path: Path) -> None:
    paragraphs = [f"第{index}段：" + "患者头痛错字，需要调整标点和分段。" * 80 for index in range(1, 7)]
    source = tmp_path / "long.md"
    source.write_text("\n\n".join(paragraphs), encoding="utf-8")
    client = EchoChunkClient()

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="model",
            concurrency=3,
            llm_chunk_chars=2000,
        ),
        client=client,
    )

    row = report["documents"][0]
    final_text = Path(row["final_markdown_path"]).read_text(encoding="utf-8")
    correction = json.loads(Path(row["correction_report_path"]).read_text(encoding="utf-8"))
    acceptance = json.loads(Path(row["acceptance_report_path"]).read_text(encoding="utf-8"))
    correction_chunks = list((tmp_path / "out" / "process" / "reports" / "long.llm_correction_chunks").glob("*.json"))
    acceptance_chunks = list((tmp_path / "out" / "process" / "reports" / "long.llm_acceptance_chunks").glob("*.json"))
    assert report["status"] == "passed"
    assert client.correction_calls > 1
    assert client.acceptance_calls > 1
    assert "正字" in final_text
    assert "错字" not in final_text
    assert correction["chunk_count"] == client.correction_calls
    assert acceptance["chunk_count"] == client.acceptance_calls
    assert len(correction_chunks) == client.correction_calls
    assert len(acceptance_chunks) == client.acceptance_calls


def test_process_documents_allows_review_correction_when_text_is_returned(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    source.write_text("患者头痛错字。", encoding="utf-8")

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        client=ReviewButCorrectedClient(),
    )

    row = report["documents"][0]
    correction = json.loads(Path(row["correction_report_path"]).read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert correction["chunks"][0]["requires_review"] is True
    assert "正字" in Path(row["final_markdown_path"]).read_text(encoding="utf-8")


def test_correct_text_concurrently_allows_empty_markup_residue_chunk(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    text = "\n\n".join(["前文错字。" * 400, "<td></td></tr></table>", "后文错字。" * 400])
    source.write_text(text, encoding="utf-8")

    correction = correct_text_concurrently(
        source_name=source.name,
        text_path=source,
        text=text,
        client=EmptyMarkupResidueClient(),
        model="model",
        concurrency=2,
        max_chars=40,
        chunk_report_dir=tmp_path / "chunks",
    )

    empty_chunk = next(chunk for chunk in correction["chunks"] if chunk["input_chars"] == len("<td></td></tr></table>"))
    assert correction["draft_ready"] is True
    assert correction["status"] == "draft_ready"
    assert empty_chunk["draft_ready"] is True
    assert empty_chunk["empty_corrected_text_allowed"] is True
    assert "<td>" not in correction["corrected_text"]
    assert "前文正字" in correction["corrected_text"]
    assert "后文正字" in correction["corrected_text"]


def test_process_documents_repairs_failed_acceptance_chunk_then_rechecks(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    source.write_text("一厥阴，二少阴，三太阴，四少阳，五阳明，六太阳。厥阴主风木之气，少阴主君火之气，太阴主湿土之气，少阳主相火之气，太阳主寒水之气。", encoding="utf-8")

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        client=RepairingClient(),
    )

    row = report["documents"][0]
    final_text = Path(row["final_markdown_path"]).read_text(encoding="utf-8")
    repair = json.loads(Path(row["repair_report_path"]).read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert "阳明主燥金之气" in final_text
    assert repair["repaired"] is True


def test_process_documents_applies_multiple_acceptance_repair_rounds(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    source.write_text("甲。", encoding="utf-8")
    client = TwoRoundRepairClient()

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="model",
            acceptance_repair_rounds=2,
        ),
        client=client,
    )

    row = report["documents"][0]
    final_text = Path(row["final_markdown_path"]).read_text(encoding="utf-8")
    repair = json.loads(Path(row["repair_report_path"]).read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert final_text.strip() == "甲，乙，丙。"
    assert client.repair_calls == 2
    assert repair["round"] == 2


def test_process_documents_reuses_successful_chunk_reports(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    source.write_text("患者头痛错字。", encoding="utf-8")
    output = tmp_path / "out"
    first = process_documents(
        input_path=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model", llm_chunk_chars=2000),
        client=EchoChunkClient(),
    )
    second = process_documents(
        input_path=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model", llm_chunk_chars=2000),
        client=ExplodingClient(),
    )

    assert first["status"] == "passed"
    assert second["status"] == "passed"


def test_llm_client_retries_http_503(monkeypatch) -> None:
    calls = 0

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"{\\"status\\":\\"passed\\"}"}}]}'

    def fake_urlopen(*args: object, **kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError("https://llm.test", 503, "Service Unavailable", {}, None)
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    client = LlmClient(LlmConfig(api_url="https://llm.test", api_key="key", model="model", timeout_seconds=1))

    assert client.complete_json("prompt")["status"] == "passed"
    assert calls == 2


def test_llm_client_retries_remote_disconnect(monkeypatch) -> None:
    calls = 0

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"{\\"status\\":\\"passed\\"}"}}]}'

    def fake_urlopen(*args: object, **kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    client = LlmClient(LlmConfig(api_url="https://llm.test", api_key="key", model="model", timeout_seconds=1))

    assert client.complete_json("prompt")["status"] == "passed"
    assert calls == 2


def test_llm_client_curl_transport_enforces_timeout_and_retries(monkeypatch) -> None:
    calls = 0

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.TimeoutExpired(cmd="curl", timeout=1)
        return subprocess.CompletedProcess(
            args=["curl"],
            returncode=0,
            stdout='{"choices":[{"message":{"content":"{\\"status\\":\\"passed\\"}"}}]}\nHTTP_STATUS:200',
            stderr="",
        )

    monkeypatch.setenv("LLMCHECK_LLM_TRANSPORT", "curl")
    monkeypatch.setattr("llmcheck.llm.subprocess.run", fake_run)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    client = LlmClient(LlmConfig(api_url="https://llm.test", api_key="key", model="model", timeout_seconds=1))

    assert client.complete_json("prompt")["status"] == "passed"
    assert calls == 2


def test_llm_client_respects_configured_retry_count(monkeypatch) -> None:
    calls = 0

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired(cmd="curl", timeout=1)

    monkeypatch.setenv("LLMCHECK_LLM_TRANSPORT", "curl")
    monkeypatch.setenv("LLMCHECK_LLM_RETRIES", "1")
    monkeypatch.setattr("llmcheck.llm.subprocess.run", fake_run)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    client = LlmClient(LlmConfig(api_url="https://llm.test", api_key="key", model="model", timeout_seconds=1))

    result = client.complete_json("prompt")

    assert result["status"] == "error"
    assert calls == 1


def test_discover_source_files_accepts_markdown_pdf_images_and_office(tmp_path: Path) -> None:
    for name in ["a.md", "b.pdf", "c.png", "d.docx", "e.xlsx", "skip.txt"]:
        (tmp_path / name).write_text("x", encoding="utf-8")

    rows = discover_source_files(tmp_path)

    assert [row.path.name for row in rows] == ["a.md", "b.pdf", "c.png", "d.docx", "e.xlsx"]
    assert [row.kind for row in rows] == [
        SourceKind.MARKDOWN,
        SourceKind.PDF,
        SourceKind.MINERU_ONLY,
        SourceKind.MINERU_ONLY,
        SourceKind.MINERU_ONLY,
    ]


def test_page_segments_split_large_pdf_into_200_page_chunks() -> None:
    assert page_segments(401, max_pages=200) == [(1, 200), (201, 400), (401, 401)]
    assert page_segments(200, max_pages=200) == [(1, 200)]


def test_split_text_chunks_preserves_order() -> None:
    text = "\n\n".join([f"第{index}段 " + ("内容。" * 300) for index in range(1, 5)])

    chunks = split_text_chunks(text, max_chars=2000)

    assert len(chunks) > 1
    assert [chunk.index for chunk in chunks] == list(range(1, len(chunks) + 1))
    assert [chunk.total for chunk in chunks] == [len(chunks)] * len(chunks)
    assert "第1段" in chunks[0].text
    assert "第4段" in chunks[-1].text


def test_split_text_chunks_allows_one_thousand_char_chunks() -> None:
    chunks = split_text_chunks("字" * 1500, max_chars=1000)

    assert len(chunks) == 2
    assert [len(chunk.text) for chunk in chunks] == [1000, 500]


def test_split_text_chunks_keeps_details_blocks_intact() -> None:
    details = "<details>\n<summary>flowchart</summary>\n\n" + ("A --> B\n" * 400) + "\n</details>"
    text = "前文。\n\n" + details + "\n\n后文。"

    chunks = split_text_chunks(text, max_chars=2000)

    details_chunks = [chunk for chunk in chunks if "<details>" in chunk.text or "</details>" in chunk.text]
    assert len(details_chunks) == 1
    assert "<details>" in details_chunks[0].text
    assert "</details>" in details_chunks[0].text


def test_prepare_markdown_inputs_runs_pdf_ppx_and_mineru_vlm(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    calls: list[tuple[str, object]] = []

    def fake_page_count(path: Path) -> int:
        assert path == pdf
        return 401

    def fake_split(path: Path, *, output_dir: Path, max_pages: int) -> list[Path]:
        calls.append(("split", max_pages))
        paths = [output_dir / "seg-001.pdf", output_dir / "seg-002.pdf", output_dir / "seg-003.pdf"]
        for item in paths:
            item.parent.mkdir(parents=True, exist_ok=True)
            item.write_bytes(b"%PDF-1.4\n")
        return paths

    def fake_ppx(path: Path, *, output_dir: Path, settings: PreprocessSettings) -> Path:
        calls.append(("ppx", path.name))
        out = output_dir / "ppx.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("ppx text", encoding="utf-8")
        return out

    def fake_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        calls.append(("mineru_model", settings.mineru_model))
        calls.append(("mineru_files", [path.name for path in files]))
        out = output_dir / "mineru_vlm.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("mineru text", encoding="utf-8")
        return out

    rows = prepare_markdown_inputs(
        input_path=pdf,
        output_dir=tmp_path / "out",
        settings=PreprocessSettings(mineru_api_key="token"),
        page_count_reader=fake_page_count,
        pdf_splitter=fake_split,
        ppx_runner=fake_ppx,
        mineru_runner=fake_mineru,
    )

    assert rows == [tmp_path / "out" / "preprocess" / "book" / "mineru" / "mineru_vlm.md"]
    assert ("split", 30) in calls
    assert ("ppx", "book.pdf") in calls
    assert ("mineru_model", "vlm") in calls
    assert ("mineru_files", ["seg-001.pdf", "seg-002.pdf", "seg-003.pdf"]) in calls


def test_prepare_markdown_inputs_starts_pdf_ppx_and_mineru_together(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    ppx_started = threading.Event()
    mineru_started = threading.Event()

    def fake_page_count(path: Path) -> int:
        assert path == pdf
        return 1

    def fake_ppx(path: Path, *, output_dir: Path, settings: PreprocessSettings) -> Path:
        ppx_started.set()
        assert mineru_started.wait(timeout=1), "MinerU should start before PPX finishes"
        out = output_dir / "ppx.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("ppx text", encoding="utf-8")
        return out

    def fake_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        assert ppx_started.wait(timeout=1), "PPX should start before MinerU records work"
        mineru_started.set()
        out = output_dir / "mineru_vlm.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("mineru text", encoding="utf-8")
        return out

    rows = prepare_markdown_inputs(
        input_path=pdf,
        output_dir=tmp_path / "out",
        settings=PreprocessSettings(mineru_api_key="token"),
        page_count_reader=fake_page_count,
        ppx_runner=fake_ppx,
        mineru_runner=fake_mineru,
    )

    assert rows == [tmp_path / "out" / "preprocess" / "book" / "mineru" / "mineru_vlm.md"]


def test_prepare_markdown_inputs_returns_after_mineru_without_waiting_for_ppx(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    ppx_started = threading.Event()
    release_ppx = threading.Event()
    ppx_finished = threading.Event()
    returned = threading.Event()
    errors: list[BaseException] = []
    rows: list[Path] = []

    def fake_page_count(path: Path) -> int:
        assert path == pdf
        return 1

    def fake_ppx(path: Path, *, output_dir: Path, settings: PreprocessSettings) -> Path:
        ppx_started.set()
        assert release_ppx.wait(timeout=2), "test should release PPX after prepare returns"
        out = output_dir / "clean" / "ppx.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("ppx text", encoding="utf-8")
        ppx_finished.set()
        return out

    def fake_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        assert ppx_started.wait(timeout=1), "PPX audit should start before MinerU returns"
        out = output_dir / "mineru_vlm.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("mineru text", encoding="utf-8")
        return out

    def run_prepare() -> None:
        try:
            rows.extend(
                prepare_markdown_inputs(
                    input_path=pdf,
                    output_dir=tmp_path / "out",
                    settings=PreprocessSettings(mineru_api_key="token"),
                    page_count_reader=fake_page_count,
                    ppx_runner=fake_ppx,
                    mineru_runner=fake_mineru,
                )
            )
            returned.set()
        except BaseException as error:  # noqa: BLE001 - preserve assertion from worker thread for the main test.
            errors.append(error)
            returned.set()

    prepare_thread = threading.Thread(target=run_prepare)
    prepare_thread.start()
    try:
        assert returned.wait(timeout=1), "prepare should return after MinerU without waiting for PPX"
        assert not errors
        expected_ppx = tmp_path / "out" / "preprocess" / "book" / "ppx" / "clean" / "ppx.md"
        assert rows == [tmp_path / "out" / "preprocess" / "book" / "mineru" / "mineru_vlm.md"]
        assert not expected_ppx.exists()
        status = json.loads((tmp_path / "out" / "preprocess" / "book" / "ppx" / "ppx_audit_status.json").read_text(encoding="utf-8"))
        assert status["status"] == "running"
        release_ppx.set()
        assert ppx_finished.wait(timeout=1)
        assert expected_ppx.read_text(encoding="utf-8") == "ppx text"
    finally:
        release_ppx.set()
        prepare_thread.join(timeout=1)


def test_run_ppx_reuses_existing_clean_markdown(tmp_path: Path) -> None:
    cached = tmp_path / "ppx" / "clean" / "ppx.md"
    cached.parent.mkdir(parents=True)
    cached.write_text("cached ppx text", encoding="utf-8")

    result = run_ppx(
        tmp_path / "book.pdf",
        output_dir=tmp_path / "ppx",
        settings=PreprocessSettings(ppx_command="/missing/ppx"),
    )

    assert result == cached
    assert result.read_text(encoding="utf-8") == "cached ppx text"


def test_run_ppx_uses_configured_local_ocr_flags(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF")
    ppx_command = tmp_path / "ppx"
    ppx_command.write_text("#!/bin/sh\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        raw_dir = Path(command[command.index("--out-dir") + 1])
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "book.md").write_text("ppx text", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("llmcheck.preprocess.subprocess.run", fake_run)

    result = run_ppx(
        source,
        output_dir=tmp_path / "ppx-out",
        settings=PreprocessSettings(
            ppx_command=str(ppx_command),
            ppx_backend="default",
            ppx_ocr="yes",
            ppx_formula="auto",
        ),
    )

    assert result.read_text(encoding="utf-8") == "ppx text"
    command = captured["command"]
    assert isinstance(command, list)
    assert command[command.index("--backend") + 1] == "default"
    assert command[command.index("--ocr") + 1] == "yes"
    assert command[command.index("--formula") + 1] == "auto"


def test_prepare_markdown_inputs_requires_mineru_key_for_pdf_without_cache(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF")
    split_calls: list[int] = []
    mineru_calls: list[list[str]] = []
    ppx_calls = 0

    def fake_page_count(path: Path) -> int:
        assert path == pdf
        return 61

    def fake_split(path: Path, *, output_dir: Path, max_pages: int) -> list[Path]:
        split_calls.append(max_pages)
        raise AssertionError("missing MinerU key should avoid PDF splitting")

    def fake_ppx(path: Path, *, output_dir: Path, settings: PreprocessSettings) -> Path:
        nonlocal ppx_calls
        ppx_calls += 1
        assert path == pdf
        out = output_dir / "clean" / "ppx.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("ppx formal text", encoding="utf-8")
        return out

    def fail_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        mineru_calls.append([path.name for path in files])
        raise AssertionError("missing MinerU key should avoid MinerU conversion")

    try:
        prepare_markdown_inputs(
            input_path=pdf,
            output_dir=tmp_path / "out",
            settings=PreprocessSettings(mineru_api_key="", pdf_page_chunk_size=30),
            page_count_reader=fake_page_count,
            pdf_splitter=fake_split,
            ppx_runner=fake_ppx,
            mineru_runner=fail_mineru,
        )
    except RuntimeError as error:
        assert "MinerU API key" in str(error)
    else:
        raise AssertionError("missing MinerU key should fail PDF standard flow")

    assert split_calls == []
    assert mineru_calls == []
    assert ppx_calls == 0


def test_run_mineru_vlm_reuses_matching_manifest(tmp_path: Path) -> None:
    source = tmp_path / "seg.pdf"
    source.write_bytes(b"%PDF")
    output = tmp_path / "mineru"
    segment_md = output / "segment_001" / "full.md"
    segment_md.parent.mkdir(parents=True)
    segment_md.write_text("cached mineru", encoding="utf-8")
    merged = output / "mineru_vlm.md"
    merged.write_text("cached mineru", encoding="utf-8")
    (output / "mineru_manifest.json").write_text(
        json.dumps(
            {
                "status": "converted",
                "source_files": [str(source)],
                "segment_markdowns": [str(segment_md)],
                "merged_markdown": str(merged),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = run_mineru_vlm([source], output_dir=output, settings=PreprocessSettings(mineru_api_key="token"))

    assert result == merged
    assert result.read_text(encoding="utf-8") == "cached mineru"


def test_run_mineru_vlm_for_pdf_chunks_reuses_matching_manifest_without_api_key(tmp_path: Path) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF")
    segment_dir = tmp_path / "segments"
    output = tmp_path / "mineru"
    segment_path = segment_dir / "book__pages_0001_0002.pdf"
    segment_md = output / "segment_001" / "full.md"
    merged = output / "mineru_vlm.md"
    segment_path.parent.mkdir(parents=True)
    segment_path.write_bytes(b"%PDF")
    segment_md.parent.mkdir(parents=True)
    segment_md.write_text("cached chunk", encoding="utf-8")
    merged.write_text("cached chunk", encoding="utf-8")
    (output / "mineru_manifest.json").write_text(
        json.dumps(
            {
                "status": "converted",
                "source_files": [str(segment_path)],
                "segment_markdowns": [str(segment_md)],
                "merged_markdown": str(merged),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = run_mineru_vlm_for_pdf_chunks(
        source,
        page_count=2,
        segment_dir=segment_dir,
        output_dir=output,
        settings=PreprocessSettings(mineru_api_key="", pdf_page_chunk_size=30),
    )

    assert result == merged
    assert result.read_text(encoding="utf-8") == "cached chunk"


def test_run_mineru_vlm_submits_multiple_files_in_one_batch(tmp_path: Path, monkeypatch) -> None:
    files = [tmp_path / "seg1.pdf", tmp_path / "seg2.pdf"]
    for file in files:
        file.write_bytes(b"%PDF")
    output = tmp_path / "mineru"
    created_batches: list[list[str]] = []

    class FakeMinerUClient:
        def create_upload_batch(self, *, files: list[object], model_version: str) -> tuple[str, list[str]]:
            created_batches.append([getattr(file, "name") for file in files])
            return "batch-1", [f"https://upload.test/{index}" for index, _ in enumerate(files)]

        def upload_files(self, *, files: list[object], upload_urls: list[str]) -> None:
            assert [getattr(file, "name") for file in files] == ["seg1.pdf", "seg2.pdf"]
            assert len(upload_urls) == 2

        def poll_batch_results(
            self,
            *,
            batch_id: str,
            poll_interval_seconds: int,
            timeout_seconds: int,
            on_poll=None,
        ) -> list[dict[str, object]]:
            rows = [
                {"state": "done", "file_name": "seg1.pdf", "data_id": "llmcheck_seg1_001", "full_zip_url": "https://download.test/seg1.zip"},
                {"state": "done", "file_name": "seg2.pdf", "data_id": "llmcheck_seg2_002", "full_zip_url": "https://download.test/seg2.zip"},
            ]
            if on_poll is not None:
                on_poll(rows)
            return rows

        def download_result_zip(self, *, full_zip_url: str, output_path: Path) -> Path:
            with zipfile.ZipFile(output_path, "w") as archive:
                archive.writestr("full.md", f"markdown from {full_zip_url}")
            return output_path

    monkeypatch.setattr("llmcheck.preprocess._mineru_client", lambda settings: FakeMinerUClient())

    result = run_mineru_vlm(files, output_dir=output, settings=PreprocessSettings(mineru_api_key="token", mineru_batch_size=50))

    assert created_batches == [["seg1.pdf", "seg2.pdf"]]
    text = result.read_text(encoding="utf-8")
    assert "markdown from https://download.test/seg1.zip" in text
    assert "markdown from https://download.test/seg2.zip" in text


def test_run_mineru_vlm_resumes_multiple_files_from_one_batch(tmp_path: Path, monkeypatch) -> None:
    files = [tmp_path / "seg1.pdf", tmp_path / "seg2.pdf"]
    for file in files:
        file.write_bytes(b"%PDF")
    output = tmp_path / "mineru"
    for index, file in enumerate(files, start=1):
        segment_dir = output / f"segment_{index:03d}"
        segment_dir.mkdir(parents=True)
        (segment_dir / "status.json").write_text(
            json.dumps(
                {
                    "status": "polling",
                    "source": str(file),
                    "index": index,
                    "file_name": file.name,
                    "data_id": f"llmcheck_seg{index}_{index:03d}",
                    "batch_id": "batch-old",
                    "batch_size": 2,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    calls: list[str] = []

    class FakeMinerUClient:
        def create_upload_batch(self, *, files: list[object], model_version: str) -> tuple[str, list[str]]:
            raise AssertionError("should reuse existing MinerU batch")

        def upload_files(self, *, files: list[object], upload_urls: list[str]) -> None:
            raise AssertionError("should not upload again")

        def poll_batch_results(
            self,
            *,
            batch_id: str,
            poll_interval_seconds: int,
            timeout_seconds: int,
            on_poll=None,
        ) -> list[dict[str, object]]:
            calls.append(f"poll:{batch_id}")
            rows = [
                {"state": "done", "file_name": "seg2.pdf", "data_id": "llmcheck_seg2_002", "full_zip_url": "https://download.test/seg2.zip"},
                {"state": "done", "file_name": "seg1.pdf", "data_id": "llmcheck_seg1_001", "full_zip_url": "https://download.test/seg1.zip"},
            ]
            if on_poll is not None:
                on_poll(rows)
            return rows

        def download_result_zip(self, *, full_zip_url: str, output_path: Path) -> Path:
            calls.append(f"download:{full_zip_url}")
            with zipfile.ZipFile(output_path, "w") as archive:
                archive.writestr("full.md", f"markdown from {full_zip_url}")
            return output_path

    monkeypatch.setattr("llmcheck.preprocess._mineru_client", lambda settings: FakeMinerUClient())

    result = run_mineru_vlm(files, output_dir=output, settings=PreprocessSettings(mineru_api_key="token", mineru_batch_size=50))

    text = result.read_text(encoding="utf-8")
    assert calls == [
        "poll:batch-old",
        "download:https://download.test/seg1.zip",
        "download:https://download.test/seg2.zip",
    ]
    assert text.index("markdown from https://download.test/seg1.zip") < text.index("markdown from https://download.test/seg2.zip")
    assert json.loads((output / "segment_001" / "status.json").read_text(encoding="utf-8"))["status"] == "done"
    assert json.loads((output / "segment_002" / "status.json").read_text(encoding="utf-8"))["status"] == "done"


def test_run_one_mineru_file_writes_segment_status(tmp_path: Path) -> None:
    source = tmp_path / "seg.pdf"
    source.write_bytes(b"%PDF")
    output = tmp_path / "mineru" / "segment_001"
    calls: list[str] = []

    class FakeMinerUClient:
        def create_upload_batch(self, *, files: list[object], model_version: str) -> tuple[str, list[str]]:
            calls.append(f"create:{model_version}:{len(files)}")
            return "batch-1", ["https://upload.test/1"]

        def upload_files(self, *, files: list[object], upload_urls: list[str]) -> None:
            calls.append(f"upload:{len(files)}:{len(upload_urls)}")

        def poll_batch_results(
            self,
            *,
            batch_id: str,
            poll_interval_seconds: int,
            timeout_seconds: int,
            on_poll: object = None,
        ) -> list[dict[str, str]]:
            calls.append(f"poll:{batch_id}:{poll_interval_seconds}:{timeout_seconds}")
            return [{"state": "done", "full_zip_url": "https://download.test/result.zip"}]

        def download_result_zip(self, *, full_zip_url: str, output_path: Path) -> Path:
            calls.append(f"download:{full_zip_url}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(output_path, "w") as archive:
                archive.writestr("full.md", "mineru text")
            return output_path

    result = _run_one_mineru_file(
        source,
        output_dir=output,
        settings=PreprocessSettings(mineru_api_key="token", mineru_poll_interval_seconds=7, mineru_timeout_seconds=99),
        client=FakeMinerUClient(),  # type: ignore[arg-type]
        index=1,
    )

    status = json.loads((output / "status.json").read_text(encoding="utf-8"))
    assert result == output / "full.md"
    assert result.read_text(encoding="utf-8") == "mineru text"
    assert calls == ["create:vlm:1", "upload:1:1", "poll:batch-1:7:99", "download:https://download.test/result.zip"]
    assert status["status"] == "done"
    assert status["batch_id"] == "batch-1"
    assert status["markdown"] == str(output / "full.md")
    assert status["source"] == str(source)


def test_run_one_mineru_file_resumes_existing_polling_batch(tmp_path: Path) -> None:
    source = tmp_path / "seg.pdf"
    source.write_bytes(b"%PDF")
    output = tmp_path / "mineru" / "segment_001"
    output.mkdir(parents=True)
    (output / "status.json").write_text(
        json.dumps({"status": "polling", "source": str(source), "batch_id": "batch-old"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    class FakeMinerUClient:
        def create_upload_batch(self, *, files: list[object], model_version: str) -> tuple[str, list[str]]:
            raise AssertionError("should reuse existing MinerU batch")

        def upload_files(self, *, files: list[object], upload_urls: list[str]) -> None:
            raise AssertionError("should not upload again")

        def poll_batch_results(
            self,
            *,
            batch_id: str,
            poll_interval_seconds: int,
            timeout_seconds: int,
            on_poll: object = None,
        ) -> list[dict[str, str]]:
            calls.append(f"poll:{batch_id}:{poll_interval_seconds}:{timeout_seconds}")
            return [{"state": "done", "full_zip_url": "https://download.test/result.zip"}]

        def download_result_zip(self, *, full_zip_url: str, output_path: Path) -> Path:
            calls.append(f"download:{full_zip_url}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(output_path, "w") as archive:
                archive.writestr("full.md", "resumed mineru text")
            return output_path

    result = _run_one_mineru_file(
        source,
        output_dir=output,
        settings=PreprocessSettings(mineru_api_key="token", mineru_poll_interval_seconds=5, mineru_timeout_seconds=88),
        client=FakeMinerUClient(),  # type: ignore[arg-type]
        index=1,
    )

    status = json.loads((output / "status.json").read_text(encoding="utf-8"))
    assert result.read_text(encoding="utf-8") == "resumed mineru text"
    assert calls == ["poll:batch-old:5:88", "download:https://download.test/result.zip"]
    assert status["status"] == "done"
    assert status["batch_id"] == "batch-old"


def test_run_one_mineru_file_resumes_timeout_error_batch(tmp_path: Path) -> None:
    source = tmp_path / "seg.pdf"
    source.write_bytes(b"%PDF")
    output = tmp_path / "mineru" / "segment_001"
    output.mkdir(parents=True)
    (output / "status.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "source": str(source),
                "error": "MinerU batch 980a789c-b0ac-4bd5-8df6-01ef41269e94 超时，最后状态：pending",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    class FakeMinerUClient:
        def create_upload_batch(self, *, files: list[object], model_version: str) -> tuple[str, list[str]]:
            raise AssertionError("should recover batch id from failed timeout status")

        def upload_files(self, *, files: list[object], upload_urls: list[str]) -> None:
            raise AssertionError("should not upload again")

        def poll_batch_results(
            self,
            *,
            batch_id: str,
            poll_interval_seconds: int,
            timeout_seconds: int,
            on_poll: object = None,
        ) -> list[dict[str, str]]:
            calls.append(f"poll:{batch_id}")
            return [{"state": "done", "full_zip_url": "https://download.test/result.zip"}]

        def download_result_zip(self, *, full_zip_url: str, output_path: Path) -> Path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(output_path, "w") as archive:
                archive.writestr("full.md", "timeout resumed mineru text")
            return output_path

    result = _run_one_mineru_file(
        source,
        output_dir=output,
        settings=PreprocessSettings(mineru_api_key="token"),
        client=FakeMinerUClient(),  # type: ignore[arg-type]
        index=1,
    )

    status = json.loads((output / "status.json").read_text(encoding="utf-8"))
    assert result.read_text(encoding="utf-8") == "timeout resumed mineru text"
    assert calls == ["poll:980a789c-b0ac-4bd5-8df6-01ef41269e94"]
    assert status["batch_id"] == "980a789c-b0ac-4bd5-8df6-01ef41269e94"


def test_prepare_markdown_inputs_sends_images_and_office_to_mineru_without_ppx(tmp_path: Path) -> None:
    image = tmp_path / "page.png"
    image.write_bytes(b"png")
    calls: list[str] = []

    def fail_ppx(*args: object, **kwargs: object) -> Path:
        raise AssertionError("images and office files must not run PPX")

    def fake_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        calls.extend(path.name for path in files)
        out = output_dir / "page.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("mineru image text", encoding="utf-8")
        return out

    rows = prepare_markdown_inputs(
        input_path=image,
        output_dir=tmp_path / "out",
        settings=PreprocessSettings(mineru_api_key="token"),
        ppx_runner=fail_ppx,
        mineru_runner=fake_mineru,
    )

    assert rows[0].read_text(encoding="utf-8") == "mineru image text"
    assert calls == ["page.png"]


def test_mineru_client_retries_transient_request_errors(monkeypatch) -> None:
    calls = 0

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"code": 0, "data": {"ok": true}}'

    def fake_urlopen(*args: object, **kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("timed out")
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = MinerUClient(token="token", base_url="https://mineru.test", timeout_seconds=1, max_retries=2, retry_backoff_seconds=0)

    assert client._request_json("GET", "/status")["data"] == {"ok": True}
    assert calls == 2


def test_mineru_poll_continues_after_transient_status_error(monkeypatch) -> None:
    client = MinerUClient(token="token", base_url="https://mineru.test", timeout_seconds=1, max_retries=1, retry_backoff_seconds=0)
    calls = 0

    def fake_request_json(method: str, api_path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MinerUTransientError("temporary timeout")
        return {"code": 0, "data": {"extract_result": {"state": "done", "full_zip_url": "https://example.test/result.zip"}}}

    monkeypatch.setattr(client, "_request_json", fake_request_json)

    rows = client.poll_batch_results(batch_id="batch", poll_interval_seconds=0, timeout_seconds=10)

    assert rows[0]["state"] == "done"
    assert calls == 2


def test_batch_discovery_excludes_output_and_generated_files(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    output = source / "output"
    output.mkdir(parents=True)
    for name in ["0中医概念入门.pdf", "book.md", "0中医概念入门__myocr_text.pdf", "notes.txt"]:
        (source / name).write_text("x", encoding="utf-8")
    (output / "generated.md").write_text("x", encoding="utf-8")

    rows = discover_batch_sources(source_dir=source, output_dir=output)

    assert [path.name for path in rows] == ["0中医概念入门.pdf", "book.md"]


def test_batch_discovery_prunes_output_subtree(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "pdf"
    output = source / "output"
    nested_output = output / "0001_book" / "reports"
    nested_source = source / "nested"
    nested_output.mkdir(parents=True)
    nested_source.mkdir(parents=True)
    (source / "book.md").write_text("book", encoding="utf-8")
    (nested_source / "chapter.md").write_text("chapter", encoding="utf-8")
    (nested_output / "generated.md").write_text("generated", encoding="utf-8")
    visited: list[Path] = []
    real_walk = os.walk

    def tracking_walk(*args: object, **kwargs: object):
        for dirpath, dirnames, filenames in real_walk(*args, **kwargs):
            visited.append(Path(dirpath).resolve())
            yield dirpath, dirnames, filenames

    monkeypatch.setattr("llmcheck.batch.os.walk", tracking_walk)

    rows = discover_batch_sources(source_dir=source, output_dir=output)

    assert [path.name for path in rows] == ["book.md", "chapter.md"]
    assert output.resolve() not in visited


def test_run_batch_writes_per_book_outputs_and_state(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    (source / "b.md").write_text("b", encoding="utf-8")
    output = source / "output"

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        report_dir = output_dir / "process" / "reports"
        report_dir.mkdir(parents=True)
        (report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed", "documents": [{"source_path": str(input_path)}]}

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
    )

    assert summary["status"] == "passed"
    assert summary["total"] == 2
    assert (output / "process" / "llmcheck_batch_state.jsonl").exists()
    assert (output / "process" / "llmcheck_batch_summary.json").exists()
    assert sorted(path.name for path in output.glob("0*_*.md")) == []
    assert len(list((output / "process" / "books").glob("0*/process/reports/llmcheck_summary.json"))) == 2


def test_cli_batch_accepts_acceptance_repair_rounds(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, LlmCheckSettings] = {}

    def fake_run_batch(**kwargs: object) -> dict[str, object]:
        captured["settings"] = kwargs["settings"]  # type: ignore[assignment]
        return {"status": "passed"}

    monkeypatch.setattr("llmcheck.cli.run_batch", fake_run_batch)

    status = main(
        [
            "batch",
            "--source-dir",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--llm-api-url",
            "http://llm.test",
            "--llm-api-key",
            "key",
            "--llm-model",
            "model",
            "--acceptance-repair-rounds",
            "3",
        ]
    )

    assert status == 0
    assert captured["settings"].acceptance_repair_rounds == 3


def test_run_batch_processes_pdf_through_mineru_ppx_llm_and_final_outputs(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    pdf = source / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    output = source / "output"
    calls: list[tuple[str, object]] = []
    ppx_finished = threading.Event()
    client = EchoChunkClient()

    def fake_page_count(path: Path) -> int:
        assert path == pdf
        return 61

    def fake_split(path: Path, *, output_dir: Path, max_pages: int) -> list[Path]:
        calls.append(("split", max_pages))
        paths = [output_dir / "seg-001.pdf", output_dir / "seg-002.pdf", output_dir / "seg-003.pdf"]
        for item in paths:
            item.parent.mkdir(parents=True, exist_ok=True)
            item.write_bytes(b"%PDF-1.4\n")
        return paths

    def fake_ppx(path: Path, *, output_dir: Path, settings: PreprocessSettings) -> Path:
        calls.append(("ppx", path.name))
        out = output_dir / "clean" / "ppx.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("PPX 审计文本", encoding="utf-8")
        ppx_finished.set()
        return out

    def fake_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        calls.append(("mineru_model", settings.mineru_model))
        calls.append(("mineru_files", [path.name for path in files]))
        out = output_dir / "mineru_vlm.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# 本草\n\nMinerU 正式文本含错字。\n", encoding="utf-8")
        return out

    def preprocess_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> list[Path]:
        return prepare_markdown_inputs(
            input_path=input_path,
            output_dir=output_dir,
            settings=PreprocessSettings(
                mineru_api_url=settings.mineru_api_url,
                mineru_api_key=settings.mineru_api_key,
                mineru_model=settings.mineru_model,
                mineru_concurrency=settings.mineru_concurrency,
                mineru_timeout_seconds=settings.mineru_timeout_seconds,
                mineru_request_timeout_seconds=settings.mineru_request_timeout_seconds,
                mineru_max_retries=settings.mineru_max_retries,
                mineru_retry_backoff_seconds=settings.mineru_retry_backoff_seconds,
                pdf_page_chunk_size=settings.pdf_page_chunk_size,
                ppx_command=settings.ppx_command,
                ppx_cwd=settings.ppx_cwd,
                ppx_timeout_seconds=settings.ppx_timeout_seconds,
            ),
            page_count_reader=fake_page_count,
            pdf_splitter=fake_split,
            ppx_runner=fake_ppx,
            mineru_runner=fake_mineru,
        )

    def runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        return process_documents(
            input_path=input_path,
            output_dir=output_dir,
            settings=settings,
            client=client,
            preprocess_runner=preprocess_runner,
        )

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="model",
            concurrency=4,
            mineru_api_key="token",
            mineru_concurrency=3,
            pdf_page_chunk_size=30,
        ),
        runner=runner,
    )

    book_output = output / "process" / "books" / "0001_book"
    book_summary = json.loads((book_output / "process" / "reports" / "llmcheck_summary.json").read_text(encoding="utf-8"))
    document = book_summary["documents"][0]
    final_md = Path(document["final_markdown_path"])
    final_pdf = Path(document["text_pdf_path"])
    manifest = json.loads((book_output / "process" / "preprocess" / "book" / "preprocess_manifest.json").read_text(encoding="utf-8"))
    ppx_status = json.loads(Path(manifest["ppx_audit_status"]).read_text(encoding="utf-8"))

    assert summary["status"] == "passed"
    assert summary["documents"][0]["output_dir"] == str(book_output)
    assert book_summary["status"] == "passed"
    assert document["status"] == "passed"
    assert final_md.exists()
    assert "正字" in final_md.read_text(encoding="utf-8")
    assert final_pdf.read_bytes().startswith(b"%PDF-")
    assert manifest["formal_markdown"] == manifest["mineru_markdown"]
    assert Path(manifest["formal_markdown"]).name == "mineru_vlm.md"
    assert ppx_finished.wait(timeout=1)
    assert ppx_status["status"] == "completed"
    assert ("split", 30) in calls
    assert ("mineru_model", "vlm") in calls
    assert ("mineru_files", ["seg-001.pdf", "seg-002.pdf", "seg-003.pdf"]) in calls
    assert client.correction_calls >= 1
    assert client.acceptance_calls >= 1


def test_run_batch_treats_skipped_passed_books_as_success(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    source_file = source / "a.md"
    source_file.write_text("a", encoding="utf-8")
    output = source / "output"
    book_output = output / "0001_a"
    report_dir = book_output / "reports"
    report_dir.mkdir(parents=True)
    final_md = book_output / "final_markdown" / "a.md"
    final_pdf = book_output / "text_pdfs" / "a.pdf"
    final_md.parent.mkdir(parents=True)
    final_pdf.parent.mkdir(parents=True)
    final_md.write_text("accepted", encoding="utf-8")
    final_pdf.write_bytes(b"%PDF-1.4\n")
    (report_dir / "llmcheck_summary.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "input_path": str(source_file.resolve()),
                "documents": [
                    {
                        "status": "passed",
                        "final_markdown_path": str(final_md),
                        "text_pdf_path": str(final_pdf),
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    calls = 0

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "passed"}

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
    )

    assert calls == 0
    assert summary["status"] == "passed"
    assert summary["passed"] == 1
    assert summary["skipped"] == 1
    assert summary["documents"][0]["status"] == "skipped"


def test_run_batch_reruns_when_passed_summary_lacks_final_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    output = source / "output"
    report_dir = output / "0001_a" / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "llmcheck_summary.json").write_text(
        json.dumps({"status": "passed", "input_path": str((source / "a.md").resolve()), "documents": [{"status": "passed"}]}) + "\n",
        encoding="utf-8",
    )
    calls = 0

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        nonlocal calls
        calls += 1
        new_report_dir = output_dir / "reports"
        new_report_dir.mkdir(parents=True, exist_ok=True)
        (new_report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed"}

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
    )

    assert calls == 1
    assert summary["status"] == "passed"
    assert summary["documents"][0]["status"] == "passed"


def test_run_batch_preserves_original_index_with_start_index(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    (source / "b.md").write_text("b", encoding="utf-8")
    output = source / "output"

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        report_dir = output_dir / "process" / "reports"
        report_dir.mkdir(parents=True)
        (report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed"}

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        start_index=2,
        limit=1,
        runner=fake_runner,
    )

    assert summary["status"] == "passed"
    assert summary["documents"][0]["index"] == 2
    assert Path(summary["documents"][0]["output_dir"]).name == "0002_b"
    assert (output / "process" / "books" / "0002_b" / "process" / "reports" / "llmcheck_summary.json").exists()


def test_run_batch_empty_selection_requires_review(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    output = source / "output"

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
    )

    assert summary["status"] == "review_required"
    assert summary["total"] == 0
    assert "未发现可处理输入" in summary["error"]


def test_run_batch_summary_merges_incremental_state(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    (source / "b.md").write_text("b", encoding="utf-8")
    output = source / "output"

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        report_dir = output_dir / "process" / "reports"
        report_dir.mkdir(parents=True)
        (report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed"}

    run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        start_index=1,
        limit=1,
        runner=fake_runner,
    )
    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        start_index=2,
        limit=1,
        runner=fake_runner,
    )

    assert summary["status"] == "passed"
    assert summary["selected_total"] == 1
    assert summary["total"] == 2
    assert [Path(row["output_dir"]).name for row in summary["documents"]] == ["0001_a", "0002_b"]


def test_run_batch_out_of_range_ignores_historical_state(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    output = source / "output"

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        report_dir = output_dir / "reports"
        report_dir.mkdir(parents=True)
        (report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed"}

    run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        start_index=1,
        limit=1,
        runner=fake_runner,
    )
    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        start_index=99,
        limit=1,
        runner=fake_runner,
    )

    assert summary["status"] == "review_required"
    assert summary["selected_total"] == 0
    assert summary["total"] == 0
    assert summary["documents"] == []


def test_run_batch_emits_progress_events(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    output = source / "output"
    events: list[dict[str, object]] = []

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        report_dir = output_dir / "reports"
        report_dir.mkdir(parents=True)
        (report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed"}

    run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
        progress_callback=events.append,
    )

    assert [event["event"] for event in events] == ["batch_started", "book_started", "book_finished"]
    assert events[1]["book_name"] == "a.md"
    assert events[2]["status"] == "passed"


def test_run_batch_progress_callback_error_does_not_fail_book(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    output = source / "output"

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        report_dir = output_dir / "reports"
        report_dir.mkdir(parents=True)
        (report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed"}

    def broken_progress(event: dict[str, object]) -> None:
        raise RuntimeError(f"progress sink failed: {event['event']}")

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
        progress_callback=broken_progress,
    )

    assert summary["status"] == "passed"
    assert summary["documents"][0]["status"] == "passed"


def test_run_batch_writes_in_progress_state_before_runner_finishes(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    output = source / "output"
    observed: dict[str, str] = {}

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        report_dir = output_dir / "reports"
        report_dir.mkdir(parents=True)
        (report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed"}

    def on_progress(event: dict[str, object]) -> None:
        if event["event"] != "book_started":
            return
        state_rows = (output / "process" / "llmcheck_batch_state.jsonl").read_text(encoding="utf-8").splitlines()
        summary = json.loads((output / "process" / "llmcheck_batch_summary.json").read_text(encoding="utf-8"))
        observed["state_status"] = str(json.loads(state_rows[-1])["status"])
        observed["summary_status"] = str(summary["documents"][0]["status"])

    run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
        progress_callback=on_progress,
    )

    assert observed == {"state_status": "in_progress", "summary_status": "in_progress"}


def test_cli_progress_event_prints_jsonl_to_stderr(capsys) -> None:
    _print_progress_event({"event": "book_started", "index": 6, "total": 24, "book_name": "14本草备要讲解.pdf"})

    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["type"] == "progress"
    assert payload["event"] == "book_started"
    assert payload["book_name"] == "14本草备要讲解.pdf"


def test_cli_mineru_status_summarizes_book_output(tmp_path: Path, capsys) -> None:
    mineru_dir = tmp_path / "process" / "preprocess" / "14本草备要讲解" / "mineru"
    (mineru_dir / "segment_001").mkdir(parents=True)
    (mineru_dir / "segment_002").mkdir(parents=True)
    (mineru_dir / "segment_001" / "status.json").write_text('{"status":"cached"}\n', encoding="utf-8")
    (mineru_dir / "segment_002" / "status.json").write_text(
        '{"status":"polling","state_counts":{"pending":1}}\n',
        encoding="utf-8",
    )
    ppx_markdown = tmp_path / "process" / "preprocess" / "14本草备要讲解" / "ppx" / "clean" / "ppx.md"
    ppx_markdown.parent.mkdir(parents=True)
    ppx_markdown.write_text("ppx audit text", encoding="utf-8")

    exit_code = main(["mineru-status", "--book-output-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["book_name"] == "14本草备要讲解"
    assert payload["stage"] == "mineru_pending"
    assert payload["mineru_segments"]["total"] == 2
    assert payload["mineru_segments"]["status_counts"] == {"cached": 1, "polling": 1}
    assert payload["mineru_segments"]["cloud_state_counts"] == {"pending": 1}
    assert payload["mineru_segments"]["updated_age_seconds"] >= 0
    assert payload["artifacts"]["ppx_audit_size"] == len("ppx audit text".encode())
    assert payload["artifacts"]["llm_correction_report_count"] == 0
    assert payload["artifacts"]["llm_acceptance_report_count"] == 0


def test_llmcheck_settings_and_cli_default_to_ten_llm_workers(monkeypatch, tmp_path: Path) -> None:
    assert LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model").concurrency == 10
    captured: dict[str, object] = {}

    def fake_process_documents(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        captured["settings"] = settings
        return {
            "status": "passed",
            "input_path": str(input_path),
            "output_dir": str(output_dir),
            "document_count": 0,
            "documents": [],
        }

    source = tmp_path / "source.md"
    source.write_text("text", encoding="utf-8")
    monkeypatch.setattr("llmcheck.cli.process_documents", fake_process_documents)

    exit_code = main(
        [
            "run",
            "--input",
            str(source),
            "--output-dir",
            str(tmp_path / "out"),
            "--llm-api-url",
            "http://llm.test",
            "--llm-api-key",
            "key",
            "--llm-model",
            "model",
        ]
    )

    assert exit_code == 0
    settings = captured["settings"]
    assert isinstance(settings, LlmCheckSettings)
    assert settings.concurrency == 10


def test_cli_allows_one_thousand_char_llm_chunks(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_process_documents(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        captured["settings"] = settings
        return {
            "status": "passed",
            "input_path": str(input_path),
            "output_dir": str(output_dir),
            "document_count": 0,
            "documents": [],
        }

    source = tmp_path / "source.md"
    source.write_text("text", encoding="utf-8")
    monkeypatch.setattr("llmcheck.cli.process_documents", fake_process_documents)

    exit_code = main(
        [
            "run",
            "--input",
            str(source),
            "--output-dir",
            str(tmp_path / "out"),
            "--llm-api-url",
            "http://llm.test",
            "--llm-api-key",
            "key",
            "--llm-model",
            "model",
            "--llm-chunk-chars",
            "1000",
        ]
    )

    assert exit_code == 0
    settings = captured["settings"]
    assert isinstance(settings, LlmCheckSettings)
    assert settings.llm_chunk_chars == 1000


def test_cli_reads_mineru_api_key_from_dotenv(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_process_documents(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        captured["settings"] = settings
        return {
            "status": "passed",
            "input_path": str(input_path),
            "output_dir": str(output_dir),
            "document_count": 0,
            "documents": [],
        }

    source = tmp_path / "source.md"
    source.write_text("text", encoding="utf-8")
    (tmp_path / ".env").write_text('MINERU_CLOUD_API_TOKEN="dotenv-token"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINERU_CLOUD_API_TOKEN", raising=False)
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    monkeypatch.delenv("LLMCHECK_MINERU_API_KEY", raising=False)
    monkeypatch.setattr("llmcheck.cli.process_documents", fake_process_documents)

    exit_code = main(
        [
            "run",
            "--input",
            str(source),
            "--output-dir",
            str(tmp_path / "out"),
            "--llm-api-url",
            "http://llm.test",
            "--llm-api-key",
            "key",
            "--llm-model",
            "model",
        ]
    )

    assert exit_code == 0
    settings = captured["settings"]
    assert isinstance(settings, LlmCheckSettings)
    assert settings.mineru_api_key == "dotenv-token"


def test_gui_reads_mineru_segment_status(tmp_path: Path) -> None:
    mineru_dir = tmp_path / "preprocess" / "14本草备要讲解" / "mineru"
    (mineru_dir / "segment_001").mkdir(parents=True)
    (mineru_dir / "segment_002").mkdir(parents=True)
    (mineru_dir / "segment_001" / "status.json").write_text('{"status":"cached"}\n', encoding="utf-8")
    (mineru_dir / "segment_002" / "status.json").write_text(
        '{"status":"polling","state_counts":{"pending":1}}\n',
        encoding="utf-8",
    )

    status = _read_mineru_segment_status(tmp_path, book_name="14本草备要讲解.pdf")

    assert status["total"] == 2
    assert status["status_counts"] == {"cached": 1, "polling": 1}
    assert status["cloud_state_counts"] == {"pending": 1}
    assert status["updated_age_seconds"] >= 0


def test_gui_live_progress_includes_book_diagnostics(tmp_path: Path) -> None:
    mineru_dir = tmp_path / "preprocess" / "14本草备要讲解" / "mineru"
    (mineru_dir / "segment_001").mkdir(parents=True)
    (mineru_dir / "segment_001" / "status.json").write_text(
        '{"status":"polling","state_counts":{"pending":1}}\n',
        encoding="utf-8",
    )
    ppx_markdown = tmp_path / "preprocess" / "14本草备要讲解" / "ppx" / "clean" / "ppx.md"
    ppx_markdown.parent.mkdir(parents=True)
    ppx_markdown.write_text("ppx audit text", encoding="utf-8")
    job = {"current_output_dir": str(tmp_path), "current_book": "14本草备要讲解.pdf"}

    result = _with_live_progress(job)

    assert result["mineru_segments"]["total"] == 1
    assert result["diagnostics"]["artifacts"]["ppx_audit_size"] == len("ppx audit text".encode())


def test_write_mineru_segment_status_replaces_existing_json(tmp_path: Path) -> None:
    status_path = tmp_path / "segment_001" / "status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text('{"status":"old"}\n', encoding="utf-8")

    _write_mineru_segment_status(status_path, status="polling", state_counts={"pending": 1})

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "polling"
    assert payload["state_counts"] == {"pending": 1}
    assert payload["updated_at"]
    assert not (status_path.parent / "status.json.tmp").exists()


def test_job_store_get_returns_copy() -> None:
    store = JobStore()
    job = {"job_id": "job", "steps": []}
    store._jobs["job"] = job  # noqa: SLF001 - verifies thread-safe public read behavior.

    returned = store.get("job")

    assert returned is not None
    returned["steps"].append({"label": "mutated"})
    assert store.get("job") == {"job_id": "job", "steps": []}


def test_save_uploaded_files_returns_directory_outside_output_for_batch(tmp_path: Path) -> None:
    output = tmp_path / "out"
    payload = {
        "uploaded_files": [
            {"name": "books/a.md", "data_base64": "YQ=="},
            {"name": "books/b.md", "data_base64": "Yg=="},
        ]
    }

    saved = _save_uploaded_files(payload, output_dir=output)

    assert saved is not None
    assert saved.is_dir()
    assert output not in saved.parents
    assert (saved / "books" / "a.md").read_text(encoding="utf-8") == "a"
    assert (saved / "books" / "b.md").read_text(encoding="utf-8") == "b"


def test_gui_html_exposes_directory_output_llm_and_concurrency_controls() -> None:
    html = render_index_html()

    assert 'id="uploaded_files"' in html
    assert 'id="uploaded_directory"' in html
    assert "webkitdirectory" in html
    assert ".png,.jpg,.jpeg,.jp2,.webp,.gif,.bmp,.doc,.docx,.ppt,.pptx,.xls,.xlsx" in html
    assert "supportedSuffixes" in html
    assert "endsWith('.md')" not in html
    assert 'id="input_path"' in html
    assert 'id="output_dir"' in html
    assert 'id="llm_api_url"' in html
    assert 'id="llm_api_key"' in html
    assert 'id="llm_model"' in html
    assert 'id="concurrency"' in html
    assert 'id="book_concurrency"' in html
    assert 'id="start_index"' in html
    assert 'id="limit"' in html
    assert 'id="force"' in html
    assert 'id="llm_chunk_chars"' in html
    assert 'id="mineru_api_key"' in html
    assert 'id="mineru_concurrency"' in html
    assert 'id="mineru_batch_size"' in html
    assert 'id="mineru_timeout_seconds"' in html
    assert 'id="mineru_request_timeout_seconds"' in html
    assert 'id="mineru_max_retries"' in html
    assert 'id="mineru_retry_backoff_seconds"' in html
    assert 'id="ppx_cwd"' in html
    assert 'id="ppx_timeout_seconds"' in html
    assert 'id="ppx_backend"' in html
    assert 'id="ppx_ocr"' in html
    assert 'id="ppx_formula"' in html
    assert "mineru_batch_size: document.getElementById('mineru_batch_size').value" in html
    assert "ppx_backend: document.getElementById('ppx_backend').value" in html
    assert "ppx_ocr: document.getElementById('ppx_ocr').value" in html
    assert "ppx_formula: document.getElementById('ppx_formula').value" in html


def test_gui_exe_launcher_starts_server_without_browser(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(app: object, *, host: str, port: int) -> None:
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr("llmcheck.gui_exe.uvicorn.run", fake_run)
    monkeypatch.setattr("llmcheck.gui_exe.create_app", lambda: "app")

    assert gui_exe.main(["--host", "0.0.0.0", "--port", "8767", "--no-browser"]) == 0
    assert captured == {"app": "app", "host": "0.0.0.0", "port": 8767}
    assert gui_exe._browser_url(host="0.0.0.0", port=8767) == "http://127.0.0.1:8767"
