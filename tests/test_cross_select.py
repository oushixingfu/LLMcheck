from __future__ import annotations

import json
from pathlib import Path

from llmcheck.cross import score_candidate, select_initial_markdown, write_cross_artifacts


CLEAN_TEXT = (
    "# 医案\n\n"
    "本套丛书以每位医家独立成册，每册按医家小传、专病论治、诊余漫话、年谱四部分进行编写。"
    "其中，医家小传简要介绍医家的生平及成才之路；专病论治意在以病统论、以论统案、以案统话，便于临床学习与借鉴。\n"
)

DIRTY_TEXT = (
    "锟斤拷乱码文本�\x00\n"
    "这是一个普通段落的第一部分\n第二部分仍然是同一个句子\n第三部分才结束。\n"
)


def test_score_candidate_penalizes_empty_and_mojibake() -> None:
    empty = score_candidate("   ")
    dirty = score_candidate(DIRTY_TEXT)
    clean = score_candidate(CLEAN_TEXT)

    assert empty["empty"] is True
    assert empty["usable"] is False
    assert empty["score"] < dirty["score"] < clean["score"]
    assert dirty["mojibake"] is True
    assert dirty["replacement_characters"] is True
    assert dirty["control_chars"] is True
    assert clean["readable_chars"] > 20


def test_select_prefers_cleaner_candidate() -> None:
    result = select_initial_markdown(mineru_text=DIRTY_TEXT, ppx_text=CLEAN_TEXT)

    assert result["mode"] == "selected_ppx"
    assert result["winner"] == "ppx"
    assert result["text"] == CLEAN_TEXT
    assert result["scores"]["ppx"]["score"] > result["scores"]["mineru"]["score"]


def test_select_near_tie_prefers_mineru() -> None:
    # Identical candidates are a pure near-tie; MinerU must win.
    result = select_initial_markdown(mineru_text=CLEAN_TEXT, ppx_text=CLEAN_TEXT)

    assert result["scores"]["mineru"]["score"] == result["scores"]["ppx"]["score"]
    assert result["winner"] == "mineru"
    assert result["mode"] == "selected_mineru"
    assert "near_tie_prefers_mineru" in result["reasons"]
    assert result["text"] == CLEAN_TEXT


def test_select_only_one_side_works() -> None:
    mineru_only = select_initial_markdown(mineru_text=CLEAN_TEXT, ppx_text=None)
    assert mineru_only["mode"] == "mineru_only"
    assert mineru_only["winner"] == "mineru"
    assert mineru_only["text"] == CLEAN_TEXT

    ppx_only = select_initial_markdown(mineru_text=None, ppx_text=CLEAN_TEXT)
    assert ppx_only["mode"] == "ppx_fallback"
    assert ppx_only["winner"] == "ppx"
    assert ppx_only["text"] == CLEAN_TEXT

    mineru_beats_empty_ppx = select_initial_markdown(mineru_text=CLEAN_TEXT, ppx_text="   ")
    assert mineru_beats_empty_ppx["mode"] == "selected_mineru"
    assert mineru_beats_empty_ppx["winner"] == "mineru"


def test_select_both_empty_failed() -> None:
    result = select_initial_markdown(mineru_text="", ppx_text="   ")

    assert result["mode"] == "failed"
    assert result["winner"] is None
    assert result["text"] == ""
    assert result["scores"]["mineru"]["empty"] is True
    assert result["scores"]["ppx"]["empty"] is True


def test_select_existing_markdown() -> None:
    result = select_initial_markdown(mineru_text=None, ppx_text=None, existing_md=CLEAN_TEXT)

    assert result["mode"] == "existing_md"
    assert result["winner"] == "existing_md"
    assert result["text"] == CLEAN_TEXT


def test_write_cross_artifacts_creates_files(tmp_path: Path) -> None:
    work_dir = tmp_path / "doc"
    mineru_path = work_dir / "mineru" / "mineru_vlm.md"
    ppx_path = work_dir / "ppx" / "clean" / "ppx.md"
    mineru_path.parent.mkdir(parents=True, exist_ok=True)
    ppx_path.parent.mkdir(parents=True, exist_ok=True)
    mineru_path.write_text(DIRTY_TEXT, encoding="utf-8")
    ppx_path.write_text(CLEAN_TEXT, encoding="utf-8")

    initial = write_cross_artifacts(work_dir, mineru_path=mineru_path, ppx_path=ppx_path)

    assert initial == work_dir / "cross" / "initial.md"
    assert initial.exists()
    assert initial.read_text(encoding="utf-8").startswith("# 医案")
    report_path = work_dir / "cross" / "cross_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["mode"] == "selected_ppx"
    assert report["winner"] == "ppx"
    assert report["initial_markdown"] == str(initial)
    assert report["usable"] is True


def test_write_cross_artifacts_existing_md(tmp_path: Path) -> None:
    work_dir = tmp_path / "doc"
    source = tmp_path / "source.md"
    source.write_text(CLEAN_TEXT, encoding="utf-8")

    initial = write_cross_artifacts(work_dir, mineru_path=None, ppx_path=None, existing_md_path=source)
    report = json.loads((work_dir / "cross" / "cross_report.json").read_text(encoding="utf-8"))

    assert initial.read_text(encoding="utf-8") == CLEAN_TEXT if CLEAN_TEXT.endswith("\n") else CLEAN_TEXT + "\n"
    assert report["mode"] == "existing_md"
    assert report["winner"] == "existing_md"
