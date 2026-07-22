from __future__ import annotations

from llmcheck.quality import clean_markdown_with_report
from llmcheck.final_gate import final_acceptance_report as layered_final_acceptance_report
from llmcheck.rules import RULE_REGISTRY
from llmcheck.structure import normalize_document_structure
from llmcheck.quality import clean_markdown_text, final_acceptance_report, finalize_standard_document, quality_errors, quality_hints


def test_quality_errors_blocks_visible_encoding_artifacts() -> None:
    text = "这是锟斤拷文本，含有�替换符和零宽​字符。\n"

    errors = quality_errors(text)

    assert "mojibake" in errors
    assert "replacement_characters" in errors
    assert "zero_width_characters" in errors


def test_quality_errors_blocks_abnormal_spacing_and_forced_line_breaks() -> None:
    text = "这是一个普通段落的第一部分\n第二部分仍然是同一个句子\n第三部分才结束。另有中  文异常空格。\n"

    errors = quality_errors(text)

    assert "abnormal_cjk_spaces" in errors
    assert "forced_line_breaks" in errors


def test_quality_errors_blocks_blank_separated_forced_line_breaks() -> None:
    text = (
        "本套丛书以每位医家独立成册，每册按医家小传、专病论治、诊余漫话、年谱四部分进行编写。其中，医家小传简要介绍医家的\n\n"
        "生平及成才之路；专病论治意在以病统论、以论统案、以案统话，便于临床学习与借鉴。\n"
    )

    report = final_acceptance_report(text)

    assert report["accepted"] is False
    assert "forced_line_breaks" in report["blocking_errors"]


def test_quality_errors_allows_blank_line_separated_front_matter() -> None:
    text = "# 王合三\n\n主编 王旭 王超凡 王继先\n\n编委 王继先 王超凡 王卫红\n\n中国中医药出版社\n"

    assert "forced_line_breaks" not in quality_errors(text)


def test_quality_errors_allows_case_heading_before_case_body() -> None:
    text = (
        "# 医案\n\n"
        "案三 藏某 22岁 未婚 杭州工作\n\n"
        "1976年6月25日初诊 经阻三月而崩，屡经治疗，服激素及中药并输血后，崩势较缓，犹未净止。\n"
        "\n"
        "案三颜某28岁女已婚普陀县人民医院护士\n\n"
        "1974年11月29日初诊 婚四年未育，兹后每触及腰脊即休克，记忆力差。\n"
    )

    assert "forced_line_breaks" not in quality_errors(text)


def test_clean_markdown_text_merges_inline_body_part_label_breaks() -> None:
    text = "肩部：肩髑、外关；肘部：曲池、外关、合谷；腕指部：阳池、阳谷、后溪；腰\n背部：大椎、肾俞；膝部：膝眼、足三里。\n"

    cleaned = clean_markdown_text(text)

    assert "腰背部：大椎" in cleaned
    assert "forced_line_breaks" not in quality_errors(cleaned)


def test_clean_markdown_text_removes_isolated_replacement_characters() -> None:
    text = "曾于1952年被推选�担任四川省民主青年联合会常务委员会委员。\n"

    report = clean_markdown_with_report(text)
    cleaned = str(report["text"])

    assert "被推选担任" in cleaned
    assert "�" not in cleaned
    assert "replacement_characters" not in quality_errors(cleaned)
    assert any(change["rule_id"] == "artifact.replacement_char_remove" for change in report["rule_changes"])


def test_clean_markdown_text_merges_blank_separated_medical_word_breaks() -> None:
    text = "会诊意见明确。先服中药，并准备手\n\n术治疗。予以清肝利胆。\n"

    cleaned = clean_markdown_text(text)

    assert "准备手术治疗" in cleaned
    assert "手\n\n术" not in cleaned
    assert "forced_line_breaks" not in quality_errors(cleaned)


def test_quality_errors_blocks_duplicate_repeated_lines() -> None:
    text = "扫描页眉\n正文第一段。\n\n扫描页眉\n正文第二段。\n\n扫描页眉\n正文第三段。\n"

    assert "duplicate_repeated_lines" in quality_errors(text)


def test_structure_removes_list_prefixed_running_headers() -> None:
    """OCR often list-prefixes series headers; cleanup must still drop them."""
    text = (
        "# 中医临证备要\n\n"
        "- - 现代著名老中医名著重刊丛书\n\n"
        "正文第一段。\n\n"
        "- - 第\n\n"
        "- - 一\n\n"
        "- - 辑\n\n"
        "正文第二段。\n\n"
        "- - 现代著名老中医名著重刊丛书\n\n"
        "正文第三段。\n\n"
        "- - 第\n\n"
        "- - 一\n\n"
        "- - 辑\n\n"
        "正文第四段。\n\n"
        "- - 现代著名老中医名著重刊丛书\n\n"
        "正文第五段。\n\n"
        "- - 第\n\n"
        "- - 一\n\n"
        "- - 辑\n\n"
        "- 【验案】\n\n"
        "验案正文。\n\n"
        "- 【验案】\n\n"
        "另一验案。\n\n"
        "- 【验案】\n\n"
        "第三验案。\n"
    )

    report = normalize_document_structure(text)
    cleaned = report["text"]

    assert "- - 现代著名老中医名著重刊丛书" not in cleaned
    assert "- - 第" not in cleaned
    assert "正文第一段" in cleaned
    assert "【验案】" in cleaned
    assert "duplicate_repeated_lines" not in quality_errors(cleaned)
    assert any(change.get("kind") == "removed_repeated_lines" for change in report["changes"])


def test_quality_errors_allows_repeated_standalone_list_markers() -> None:
    text = "# 标题\n\n(1)\n\n正文第一段。\n\n(1)\n\n正文第二段。\n\n- (1)\n\n正文第三段。\n\n- (1)\n\n正文第四段。\n"

    assert "duplicate_repeated_lines" not in quality_errors(text)


def test_quality_errors_allows_repeated_case_content_labels() -> None:
    text = "# 标题\n\n- 【验案】\n\n正文第一段。\n\n- - 【验案】\n\n正文第二段。\n\n- - - 【验案】\n\n正文第三段。\n"

    assert "duplicate_repeated_lines" not in quality_errors(text)


def test_quality_errors_blocks_toc_page_headings_left_as_headings() -> None:
    text = "# 日录\n\n# 医家小传 (1)\n\n## 冠心病 (7)\n\n# 医家小传\n\n正文段落。\n"

    report = final_acceptance_report(text)

    assert report["accepted"] is False
    assert "toc_page_heading_residue" in report["blocking_errors"]


def test_quality_errors_allows_classic_citation_number_headings() -> None:
    text = (
        "# 阳明病证\n\n"
        "### 2. 蒸蒸发热\n\n"
        "## 《伤寒论》“太阳病三日，发汗不解，蒸蒸发热者，属胃也，调胃承气汤主之。”(248)\n\n"
        "正文解释这条经文的临床含义。\n\n"
        "## “阳明病，谵语，发潮热，脉滑而疾者，小承气汤主之。”(214)\n\n"
        "继续正文分析。\n"
    )

    report = final_acceptance_report(text)

    assert "toc_page_heading_residue" not in report["blocking_errors"]


def test_quality_errors_allows_unquoted_classic_clause_number_headings() -> None:
    text = "# 呕吐门\n\n## 干呕，吐涎沫，头痛者，吴茱萸汤主之。(377)\n\n治法：针上星、百会。\n"

    report = final_acceptance_report(text)

    assert "toc_page_heading_residue" not in report["blocking_errors"]




def test_quality_errors_allows_figure_caption_number_headings() -> None:
    text = "# 黄疸\n\n## 图 3\n\n证候 本病多发于春天。\n"

    report = final_acceptance_report(text)

    assert "toc_page_heading_residue" not in report["blocking_errors"]


def test_quality_errors_allows_yuanwen_clause_number_headings() -> None:
    text = (
        "# 太阳病\n\n"
        "## 【原文】太阳之为病，脉浮，头项强痛而恶寒。(1)\n\n"
        "本条为太阳病提纲。\n"
    )

    report = final_acceptance_report(text)

    assert "toc_page_heading_residue" not in report["blocking_errors"]

def test_quality_errors_allows_decimal_measurement_headings() -> None:
    text = "# 观察结果\n\n### 4. 植物神经平衡的测定：正常人的 Y 值为 0 ± 0.56\n\n正文记录测定结果。\n"

    report = final_acceptance_report(text)

    assert "toc_page_heading_residue" not in report["blocking_errors"]


def test_quality_errors_allows_journal_volume_issue_citation_headings() -> None:
    text = "# 口疮抗复发的方药介绍\n\n## 摘自《中西医结合杂志》1987；7（2）\n\n“专家为基层服务之角”栏目。\n"

    report = final_acceptance_report(text)

    assert "toc_page_heading_residue" not in report["blocking_errors"]


def test_quality_errors_allows_numbered_journal_volume_issue_page_headings() -> None:
    text = (
        "# 附录\n\n"
        "## 许润三教授主要论著一览表\n\n"
        "### 21. 许润三教授验案四则. 中级医刊, 1990, 25(5): 62\n\n"
        "正文列出该条文献的出版信息。\n"
    )

    report = final_acceptance_report(text)

    assert "toc_page_heading_residue" not in report["blocking_errors"]


def test_quality_errors_allows_toc_declared_body_heading_with_parenthetical_number() -> None:
    text = (
        "# 目录\n\n"
        "- 太阴水血停留（1） (24)\n\n"
        "- 太阴水血停留（2） (25)\n\n"
        "# 专病论治\n\n"
        "# 太阴水血停留（1）\n\n"
        "正文记录第一则辨治内容。\n\n"
        "# 太阴水血停留 (2)\n\n"
        "正文记录第二则辨治内容。\n"
    )

    report = final_acceptance_report(text)

    assert "toc_page_heading_residue" not in report["blocking_errors"]


def test_quality_errors_allows_classic_reading_plan_year_headings() -> None:
    text = (
        "# 年谱\n\n"
        "是年冬季再订十年读书计划，安排如次：\n\n"
        "### 1.《神农本草经》 1年 (1943)\n\n"
        "### 5.《难经》 1年 (1952)\n\n"
        "公元1943年，23岁。\n"
    )

    report = final_acceptance_report(text)

    assert "toc_page_heading_residue" not in report["blocking_errors"]



def test_finalize_strips_body_heading_toc_page_suffix_and_caret_footnote() -> None:
    from llmcheck.structure import _strip_body_heading_toc_page_suffixes

    text = (
        "# 各论\n\n"
        "各论导语段落包含标点符号，说明本书讲解体系。\n\n"
        "## 问心堂温病条辨原病篇 ^(1)\n\n"
        "正文讲解开始，包含标点符号，讨论规矩与学术体系。\n\n"
        "## 暑 温(1)\n\n"
        "暑温证候说明，包含标点符号，讨论白虎汤主之。\n\n"
        "## 伏暑(1)\n\n"
        "伏暑证候说明，包含标点符号，讨论湿热平等两解之。\n\n"
        "## 风温 温热 温疫 温毒 冬温(2)\n\n"
        "中下焦篇合论五种温病，包含标点符号。\n"
    )

    stripped, changed = _strip_body_heading_toc_page_suffixes(text)
    report = final_acceptance_report(stripped)

    assert "## 问心堂温病条辨原病篇" in stripped
    assert "原病篇 ^(1)" not in stripped
    assert "## 暑 温(1)" not in stripped
    assert "## 暑 温" in stripped
    assert "## 伏暑" in stripped
    assert "## 风温 温热 温疫 温毒 冬温" in stripped
    assert any("暑 温(1)" in item for item in changed)
    assert "toc_page_heading_residue" not in report["blocking_errors"]
    assert report["accepted"] is True



def test_clean_markdown_with_report_emits_rule_changes() -> None:
    report = clean_markdown_with_report("体温 $40.1^{\\circ} \\mathrm{C}$，瓜蒌 $9\\mathrm{g}$。​\n")

    assert report["status"] == "cleaned"
    assert "40.1℃" in report["text"]
    assert "9g" in report["text"]
    assert "​" not in report["text"]
    changes = report["rule_changes"]
    assert isinstance(changes, list)
    assert {change["rule_id"] for change in changes} >= {"artifact.zero_width_remove", "latex.unit_math_to_text"}
    for change in changes:
        assert change["risk_level"] in {"low", "medium", "high"}
        assert change["write_mode"] == "auto_apply"
        assert "description" in change
        assert change["match_count"] >= 1
        assert change["input_sha256"]
        assert change["output_sha256"]


def test_clean_markdown_with_report_respects_write_mode() -> None:
    report = clean_markdown_with_report("锟斤拷乱码文本\n")

    assert report["status"] == "cleaned"
    # mojibake检测目前在final_gate中作为质量错误检测，不在cleaning阶段作为rule执行
    # 这是合理的架构设计：cleaning做确定性清理，final_gate做质量门禁
    assert "text" in report


def test_finalize_standard_document_normalizes_heading_spacing() -> None:
    text = "# 标题一\n正文段落。\n## 标题二\n另一段落。\n"

    result = finalize_standard_document(text)

    assert result["status"] == "finalized"
    finalized = result["text"]
    # 标题与正文之间应该有空行
    assert "\n\n正文段落" in finalized
    assert "\n\n另一段落" in finalized


def test_finalize_standard_document_removes_repeated_running_headers() -> None:
    text = "页眉重复行\n正文第一段。\n\n页眉重复行\n正文第二段。\n\n页眉重复行\n正文第三段。\n"

    result = finalize_standard_document(text)

    assert result["finalized"] is True
    assert "页眉重复行" not in result["text"]
    assert "正文第一段" in result["text"]
    assert "正文第二段" in result["text"]


def test_finalize_standard_document_removes_title_page_name_before_front_matter() -> None:
    text = "张文康\n\n# 中国百年百名中医临床家丛书\n\n主编\n\n张文康\n\n# 出版者的话\n\n正文段落。\n"

    result = finalize_standard_document(text)

    finalized = result["text"]
    assert finalized.startswith("# 出版者的话")
    assert "张文康" not in finalized
    assert "主编\n\n张文康" not in finalized


def test_finalize_standard_document_removes_preface_ocr_fragments() -> None:
    text = """王

编

张文康

# 临中床医

蔡

小

苏

临中

家医

# 中国百年百名中医临床家丛书

# 蔡小荪

# 图书在版编目（CIP）数据

ISBN 7-80156-330-1

![](images/photo.jpg)
蔡小荪教授

蔡小蔡先生

造妙福神千半升天

# 技 传 户 扁

# 杨树姐科

为蔡小莲先生经验集题

左换

# 内容提要

正文段落。
"""

    result = finalize_standard_document(text)

    finalized = result["text"]
    assert "# 临中床医" not in finalized
    assert "临中\n\n家医" not in finalized
    assert "# 技 传 户 扁" not in finalized
    assert "# 杨树姐科" not in finalized
    assert "造妙福神千半升天" not in finalized
    assert "# 中国百年百名中医临床家丛书" not in finalized
    assert finalized.startswith("# 内容提要")
    assert "# 图书在版编目（CIP）数据" not in finalized
    assert "![](images/photo.jpg)" not in finalized
    assert "蔡小荪教授" not in finalized
    assert "# 内容提要" in finalized


def test_finalize_standard_document_removes_preface_image_math_noise_but_keeps_poem() -> None:
    text = """# 出版者的话

正文段落。

![](images/person.jpg)
承淡安先生

# 战争巨斧

中

{x}_2 = \\frac{-b - \\sqrt{{b}^2 - {4ac}}}{2a}

\\frac12x - 1 > 0

![](images/poem.jpg)

# 赠针灸专家承淡安先生七言诗二首

落落襟怀自寡俦，复兴学社展鸿猷。

# 前言

正文。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "\\frac" not in finalized
    assert "\\sqrt" not in finalized
    assert "{x}_2" not in finalized
    assert "# 战争巨斧" not in finalized
    assert "承淡安先生" in finalized
    assert "# 赠针灸专家承淡安先生七言诗二首" in finalized
    assert "落落襟怀自寡俦" in finalized
    assert "# 前言" in finalized


def test_finalize_standard_document_removes_short_digit_ocr_heading_in_preface_image_segment() -> None:
    text = """# 出版者的话

正文段落。

![](images/calligraphy.jpg)
著名书法家沙孟海为裘笑梅教授题字

# 路 酱道 3 裁 女

原浙江省委书记薛驹为裘笑梅教授题字

# 目录

- 医家小传 (1)

# 医家小传

正文。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "# 路 酱道 3 裁 女" not in finalized
    assert "著名书法家沙孟海为裘笑梅教授题字" in finalized
    assert "原浙江省委书记薛驹为裘笑梅教授题字" in finalized
    assert "# 目录" in finalized
    assert "nonstandard_heading_content" not in quality_errors(finalized)


def test_finalize_standard_document_removes_latex_formula_heading_in_preface_image_segment() -> None:
    text = """# 出版者的话

正文段落。

![](images/calligraphy.jpg)
魏龙骧先生处方手迹

# 6

# \\therefore m = 3/11 ;

# 2

# 目录

- 医家小传 (1)

# 医家小传

正文。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "# \\therefore m = 3/11 ;" not in finalized
    assert "# 6" not in finalized
    assert "# 2" not in finalized
    assert "魏龙骧先生处方手迹" in finalized
    assert "# 目录" in finalized
    assert "latex_artifacts" not in quality_errors(finalized)
    assert "nonstandard_heading_content" not in quality_errors(finalized)


def test_finalize_standard_document_removes_embedded_foreign_theorem_ocr_noise() -> None:
    text = """# 胃病

治疗应以升清降浊为主，药

Theorem 1.2. (A) Let F be a finite field and let F(x) be the set of all elements of F such that x \\in F(x) . Then F(x) is a prime ideal of F .
Theorem 1.2. (Theorem 1.1) Let F be a finite field and let F(x) be the set of all elements of F such that x \\in F(x) . Then F(x) is a prime ideal of F .

黄，脉象细滑。此乃胃中有热，肠中有寒。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "Theorem 1.2" not in finalized
    assert "\\in" not in finalized
    assert "finite field" not in finalized
    assert "治疗应以升清降浊为主" in finalized
    assert "黄，脉象细滑" in finalized
    assert "latex_artifacts" not in quality_errors(finalized)


def test_finalize_standard_document_removes_trailing_series_catalog_noise() -> None:
    text = """# 医家小传

正文段落。

1991年8月于日本富山

# 中国百年百名中医临床家丛书

# （按姓氏笔画排列）

丁光迪 于己百 干祖望万友生 马光亚 马新云王文彦 王云铭 王任之王合三 王伯岳 邓铁涛韦文贵 韦玉英 田乃庚 史沛棠叶心清 叶桔泉 叶熙春石筱山 石仰山 刘云鹏 刘仕昌刘冠军 刘炳凡 刘弼臣朱良春 米伯让 许玉山邢子亨 杜雨茂 何任何世英 何炎燊 余无言宋祚民 宋爱人 张子琳张珍玉 张梦依 张琪张云鹏 张缙 张镜人李今庸 李玉奇 李仲愚李克绍 李寿山 李济仁

# 临中床医
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "1991年8月于日本富山" in finalized
    assert "# 中国百年百名中医临床家丛书" not in finalized
    assert "# （按姓氏笔画排列）" not in finalized
    assert "# 临中床医" not in finalized
    assert "nonstandard_heading_content" not in quality_errors(finalized)


def test_finalize_standard_document_removes_trailing_series_catalog_label_inline() -> None:
    text = """# 年谱

正文段落。

# 中国百年百名中医临床家丛书（按姓氏笔画排列）

丁光迪 于己百 干祖望万友生 马光亚 孔伯华王文彦 王任之 王合三王伯岳 邓铁涛 韦文贵韦玉英 叶心清 叶熙春 叶橘泉 石筱山石幼山 刘云鹏刘冠军 刘炳凡 刘弼臣朱仁康 朱兴恭 朱良春朱春霆 米伯让 许玉山邢子亨 何任 何炎燊余无言 宋祚民 宋爱人张子琳 张珍玉 张梦侬张琪 张赞臣 张镜人李今庸 李玉奇 李仲愚李克绍 李寿山 李斯炽
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "# 中国百年百名中医临床家丛书" not in finalized
    assert "丁光迪 于己百" not in finalized
    assert finalized.rstrip().endswith("正文段落。")
    assert "nonstandard_heading_content" not in quality_errors(finalized)
    assert any(change["kind"] == "removed_trailing_series_catalog_noise" for change in result["changes"])


def test_finalize_standard_document_removes_trailing_series_catalog_table_noise() -> None:
    body = "\n\n".join(f"{year}年 董建华学术活动记录。" for year in range(1950, 1960))
    text = f"""# 年谱

{body}

1991年 董建华学术思想研究会成立。

# 中国百年百名中医临床家丛书

# （按姓氏笔画排列）

| 丁光迪 | 于己百 | 十祖望 |
| --- | --- | --- |
| 万友生 | 马光亚 | 孔伯华 |
| 王文彦 | 王任之 | 王合三 |
| 王伯岳 | 邓铁涛 | 韦文贵 韦玉英 |
| 史沛棠 | 叶心清 | 叶熙春 |
| 叶橘泉 | 石筱山 石幼山 | 刘云鹏 |
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "1991年 董建华学术思想研究会成立。" in finalized
    assert "# 中国百年百名中医临床家丛书" not in finalized
    assert "| 丁光迪 | 于己百 | 十祖望 |" not in finalized
    assert "nonstandard_heading_content" not in quality_errors(finalized)
    assert any(change["kind"] == "removed_trailing_series_catalog_noise" for change in result["changes"])


def test_finalize_standard_document_removes_trailing_vertical_series_catalog_noise() -> None:
    body = "\n\n".join(f"{year}年 裘笑梅学术活动记录。" for year in range(1950, 1970))
    names = "\n\n".join(
        [
            "丁光迪",
            "干祖望",
            "王云铭",
            "王任之",
            "王国三",
            "邓铁涛",
            "史沛棠",
            "刘云鹏",
            "刘炳凡",
            "朱良春",
            "许润三",
            "余无言",
            "何炎藥",
            "李玉奇",
            "李寿山",
            "杨继荪",
            "宋祚民",
            "张子琳",
            "马光亚",
            "王文彦",
        ]
    )
    text = f"""# 年谱

{body}

2006年5月按照裘老遗愿成立裘笑梅中医妇科发展基金。

# 中国百年百名中医临床家丛书

# （按姓氏笔画排列）

{names}
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "2006年5月按照裘老遗愿成立裘笑梅中医妇科发展基金。" in finalized
    assert "# 中国百年百名中医临床家丛书" not in finalized
    assert "# （按姓氏笔画排列）" not in finalized
    assert "丁光迪" not in finalized
    assert "nonstandard_heading_content" not in quality_errors(finalized)
    assert any(change["kind"] == "removed_trailing_series_catalog_noise" for change in result["changes"])


def test_finalize_standard_document_removes_series_catalog_before_toc() -> None:
    text = """# 内容提要

杜雨茂先生学验俱丰，本书可供中医临床医师参考。

# 中国百年百名中医临床家丛书

（按姓氏笔画排列）

丁光迪 于己百 干祖望万友生 马光亚 马新云王文彦 王云铭 王任之王合三 王伯岳 邓铁涛韦文贵 韦玉英 田乃庚 史沛棠叶心清 叶橘泉 石筱山石仰山刘云鹏 刘仕昌 刘冠军刘炳凡 刘弼臣 朱良春米伯让 许玉山 邢子亨杜雨茂 何任 何世英何炎槃 余无言 宋祚民宋爱人 张子琳 张珍玉张梦侬 张琪 张云鹏张缙 张镜人

# 目录

- 医家小传 (1)

# 医家小传

正文段落。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "# 内容提要" in finalized
    assert "# 目录" in finalized
    assert "# 中国百年百名中医临床家丛书" not in finalized
    assert "丁光迪 于己百" not in finalized
    assert "nonstandard_heading_content" not in quality_errors(finalized)
    assert any(change["kind"] == "removed_series_catalog_before_toc" for change in result["changes"])


def test_finalize_standard_document_removes_late_front_series_catalog_before_toc() -> None:
    text = """# 出版者的话

正文段落。

# 中国百年百名中医临床家丛书

# （按姓氏笔画排列）

丁光迪

万友生

王文彦

欧阳琦 罗元恺 郑守谦俞慎初 姚国美 姜春华施今墨 查玉明 胡天雄胡希恕 赵荣 赵心波赵炳南 赵绍琴哈荔田 夏桂成 徐志华徐恕甫 耿鉴庭 袁鹤侪贾堃 高辉远 郭士魁钱伯煊 梁剑波 盛国荣章真如 黄竹斋 黄宗勖黄坚白 傅方珍 董廷瑶 董建华蒲辅周 蔡小荪 袭笑梅路志正 潘澄濂 颜德馨魏长春 魏龙骧 魏指薪

![](images/catalog.jpg)

# 目录

- 医家小传 (1)

# 医家小传

正文。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "# 出版者的话" in finalized
    assert "# 目录" in finalized
    assert "# 医家小传" in finalized
    assert "# 中国百年百名中医临床家丛书" not in finalized
    assert "# （按姓氏笔画排列）" not in finalized
    assert "丁光迪" not in finalized
    assert "![](images/catalog.jpg)" not in finalized
    assert "nonstandard_heading_content" not in quality_errors(finalized)
    assert any(change["kind"] == "removed_series_catalog_before_toc" for change in result["changes"])


def test_finalize_standard_document_removes_series_catalog_before_normalized_toc() -> None:
    text = """# 出版者的话

正文段落。

中国中医药出版社

2000年10月28日

# 中国百年百名中医临床家丛书

# （按姓氏笔画排列）

丁光迪

万友生

王文彦

| 杨甲三 | 杨志一 | 杨继荪 |  |
| --- | --- | --- | --- |
| 汪逢春 | 邱茂良 | 邹云翔 |  |
| 陈苏生 | 单健民 | 周仲瑛 |  |
| 岳美中 | 承淡安 | 林如高 |  |
| 查玉明 | 胡天雄 | 胡希恕 |  |
| 耿鉴庭 | 袁鹤侪 | 贾堃 |  |

# 医家小传 (1.)

北京四大名医之一——孔伯华 ………………………………………… (1)

专病论治 (7)

咳喘 (7)

肺痈 (27)

# 医家小传

正文。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "# 出版者的话" in finalized
    assert "# 目录" in finalized
    assert "# 医家小传" in finalized
    assert "# 中国百年百名中医临床家丛书" not in finalized
    assert "# （按姓氏笔画排列）" not in finalized
    assert "丁光迪" not in finalized
    assert "| 杨甲三 | 杨志一 | 杨继荪 |" not in finalized
    assert "nonstandard_heading_content" not in quality_errors(finalized)
    assert any(change["kind"] == "removed_series_catalog_before_toc" for change in result["changes"])


def test_finalize_standard_document_removes_trailing_short_outline_noise() -> None:
    text = """# 前言

前言正文。

# 目录

- 医家小传 (1)

# 医家小传

正文段落。

版权页

# 前言

# 目录

正文

跋
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert finalized.count("# 目录") == 1
    assert "# 医家小传\n\n正文段落。" in finalized
    assert "版权页" not in finalized
    assert "\n正文\n\n跋" not in finalized
    assert "repeated_toc_heading_residue" not in quality_errors(finalized)
    assert any(change["kind"] == "removed_trailing_short_outline_noise" for change in result["changes"])


def test_finalize_standard_document_removes_trailing_duplicate_toc_outline() -> None:
    body = "\n\n".join(f"{year}年 杜雨茂学术活动记录。" for year in range(1950, 1970))
    text = f"""# 目录

- 医家小传 (1)

# 医家小传

正文段落。

# 年谱

{body}

2002年

率长子杜治锋硕士等创建“咸阳雨茂制药有限公司”。

# 目录

# 医家小传

# 专病论治

肾脏病诊治心得与要领

## 一、肾脏常见疾病治从六经入手

## 二、治疗蛋白尿宜调脾肾，截流止涩，祛邪安正

## 三、慢性肾炎水肿治分五法，化湿、温阳、健脾、育阴、活血
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert finalized.count("# 目录") == 1
    assert "# 年谱" in finalized
    assert "率长子杜治锋硕士等创建" in finalized
    assert "肾脏病诊治心得与要领" not in finalized
    assert "repeated_toc_heading_residue" not in quality_errors(finalized)
    assert any(change["kind"] == "removed_trailing_duplicate_toc_outline" for change in result["changes"])


def test_finalize_standard_document_removes_duplicate_toc_reintroduced_by_final_cleaning() -> None:
    body = "\n\n".join(f"{year}年 杜雨茂学术活动记录。" for year in range(1950, 1975))
    text = f"""# 目录

- 医家小传 (1)

# 医家小传

正文段落。

# 年谱

{body}

2002年

率长子杜治锋硕士等创建“咸阳雨茂制药有限公司”。

目录

医家小传

专病论治

肾脏病诊治心得与要领

一、肾脏常见疾病治从六经入手
二、治疗蛋白尿宜调脾肾，截流止涩，祛邪安正
三、慢性肾炎水肿治分五法，化湿、温阳、健脾、育阴、活血
四、治淋四法祛邪至要，益肾固本甘平为上
五、消肿仗附子，连翘畅三焦
六、活血利水，重用益母草
七、尿少而赤，首选茅根
八、活用猪苓汤，广泛疗肾疾慢性肾功能衰竭的辨证用药思路与方法
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert finalized.count("# 目录") == 1
    assert "率长子杜治锋硕士等创建" in finalized
    assert "肾脏病诊治心得与要领" not in finalized
    assert "repeated_toc_heading_residue" not in quality_errors(finalized)
    assert any(change["kind"] == "removed_trailing_duplicate_toc_outline" for change in result["changes"])


def test_finalize_standard_document_removes_unstructured_metadata_before_first_heading() -> None:
    text = """![](images/person.jpg)
叶熙春/李学铭主编.一北京：中国中医药出版社，2004.8

（中国百年百名中医临床家丛书）

ISBN 7-80156-646-7

中国版本图书馆CIP数据核字(2004)第072859号

发行者：中国中医药出版社

印刷者：北京泰锐印刷有限公司

书号：ISBN7-80156-646-7/R·646

定价：11.00元

# 出版者的话

正文段落。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert finalized.startswith("# 出版者的话")
    assert "ISBN 7-80156-646-7" not in finalized
    assert "发行者：中国中医药出版社" not in finalized
    assert "unstructured_front_matter_prefix" not in quality_errors(finalized)


def test_finalize_standard_document_removes_pmph_title_page_before_content_summary() -> None:
    text = """# 秦伯未 李岩 张田仁 魏执真 合著

中医临证备要/秦伯未等著. —北京:人民卫生出版社, 2005.9

（现代著名老中医名著重刊丛书 第一辑）
ISBN 7-117-07013-7

中国版本图书馆 CIP 数据核字(2005)第 094055 号

# 现代著名老中医名著重刊丛书 第一辑 中医临证备要

著者：秦伯未等

出版发行：人民卫生出版社（中继线 67616688）

网址：http://www.pmph.com

开 本：850×1168 1/32 印张：9.75

标准书号：ISBN 7-117-07013-7/R·7014

定价：18.00元

# 内容提要

本书从证状着手，便于临床参考。

# 前言

前
言

正文段落。

# 现代著名老中医名著重刊丛书

第
一
辑

## 1. 恶寒

恶寒正文。

# 现代著名老中医名著重刊丛书

## 2. 发热

发热正文。

# 现代著名老中医名著重刊丛书

## 3. 身痛

身痛正文。
"""

    result = finalize_standard_document(text)
    finalized = str(result["text"])

    assert finalized.startswith("# 内容提要")
    assert "ISBN 7-117-07013-7" not in finalized
    assert "出版发行：人民卫生出版社" not in finalized
    assert "www.pmph.com" not in finalized
    assert "# 秦伯未 李岩 张田仁 魏执真 合著" not in finalized
    assert finalized.count("# 现代著名老中医名著重刊丛书") == 0
    assert "\n前\n言\n" not in finalized
    assert "\n第\n一\n辑\n" not in finalized
    assert "unstructured_front_matter_prefix" not in quality_errors(finalized)
    assert any(change["kind"] == "removed_unstructured_front_matter_prefix" for change in result["changes"])
    assert any(change["kind"] == "removed_repeated_lines" for change in result["changes"])


def test_finalize_standard_document_removes_pmph_title_page_before_publishing_note() -> None:
    text = """# 正骨经验

人民卫生出版社
People's Medical Publishing House正骨经验刘寿山

![](images/cover.jpg)
定价：23.00元

# 奚达孙树椿马德水 孙呈祥武春发康瑞廷

![](images/photo.jpg)

刘寿山正骨经验/北京中医药大学东直门医院编.
—北京:人民卫生出版社,2006.

(现代著名老中医名著重刊丛书 第二辑)
ISBN 7-117-07379-9

网址：http://www.pmph.com

E - mail: pmph@pmph.com邮购电话：010-67605754印刷：三河市宏达印刷有限公司经销：新华书店开 本：850×1168 1/32 印张：12.75字数：269千字版次：2006年1月第1版 2006年1月第1版第1次印刷

标准书号：ISBN 7-117-07379-9/R·7380

定 价：23.00元著作权所有,请勿擅自用本书制作各类出版物,违者必究

# 出版说明

自20世纪60年代开始，我社先后组织出版了一批著名老中医经验整理著作。

正文里提到印刷术与多次印刷，应予保留。
"""

    result = finalize_standard_document(text)
    finalized = str(result["text"])

    assert finalized.startswith("# 出版说明")
    assert "ISBN 7-117-07379-9" not in finalized
    assert "www.pmph.com" not in finalized
    assert "标准书号" not in finalized
    assert "印刷：三河市" not in finalized
    assert "奚达孙树椿马德水" not in finalized
    assert "印刷术" in finalized
    assert "多次印刷" in finalized
    assert "unstructured_front_matter_prefix" not in quality_errors(finalized)
    assert any(change["kind"] == "removed_unstructured_front_matter_prefix" for change in result["changes"])


def test_finalize_standard_document_removes_title_page_before_first_heading() -> None:
    text = """主编张文康

# 出版者的话

正文段落。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert finalized.startswith("# 出版者的话")
    assert "主编张文康" not in finalized
    assert "unstructured_front_matter_prefix" not in quality_errors(finalized)


def test_finalize_standard_document_removes_cover_images_before_first_heading() -> None:
    text = """![](images/cover.jpg)

临床中医周筱斋

周仲瑛周珉主编

# 中国百年百名中医临床家丛书

正文段落。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert finalized.startswith("# 中国百年百名中医临床家丛书")
    assert "![](images/cover.jpg)" not in finalized
    assert "周仲瑛周珉主编" not in finalized
    assert "unstructured_front_matter_prefix" not in quality_errors(finalized)


def test_finalize_standard_document_removes_series_title_page_before_front_matter() -> None:
    text = """# 中国百年百名中医临床家丛书

主

编

张文康

# 中国中医药出版社

# 出版者的话

正文段落。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert finalized.startswith("# 出版者的话")
    assert "# 中国百年百名中医临床家丛书" not in finalized
    assert "# 中国中医药出版社" not in finalized
    assert "张文康" not in finalized


def test_finalize_standard_document_removes_editorial_unit_title_page_before_front_matter() -> None:
    text = """# 中国百年百名中医临床家丛书

# 刘仕昌

# 广州中医药大学温病学教研室编

顾问 彭胜权

主编 钟嘉熙 林培政

# 图书在版编目（CIP）数据

ISBN 7-80156-242-9

定价：10.00元

# 出版者的话

正文段落。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert finalized.startswith("# 出版者的话")
    assert "# 中国百年百名中医临床家丛书" not in finalized
    assert "# 广州中医药大学温病学教研室编" not in finalized
    assert "图书在版编目" not in finalized
    assert "nonstandard_heading_content" not in quality_errors(finalized)


def test_finalize_standard_document_removes_embedded_series_title_page_after_publisher_preface() -> None:
    text = """# 出版者的话

出版者序言正文。

中国中医药出版社

2000年10月28日

# 中国百年百名中医临床家丛书

# 米伯让

主编 米烈汉

中国中医药出版社

·北京·

![](images/cover.jpg)

# 贺米伯让研究员从医60周年

古本伤寒发掘难，渊源经学继长安。

# 内容提要

正文段落。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "# 出版者的话" in finalized
    assert "# 贺米伯让研究员从医60周年" in finalized
    assert "# 内容提要" in finalized
    assert "# 中国百年百名中医临床家丛书" not in finalized
    assert "# 米伯让" not in finalized
    assert "主编 米烈汉" not in finalized
    assert "![](images/cover.jpg)" not in finalized
    assert "nonstandard_heading_content" not in quality_errors(finalized)


def test_finalize_standard_document_demotes_table_of_contents_entries() -> None:
    text = """# 目录

# 医家小传 (1)

# 专病论治 (5)

# 月经失调 (5)

（一）治疗思想 (6)
（二）经验方 (9)

# 附录 (281)

# 医家小传

正文段落。
"""

    result = finalize_standard_document(text)

    finalized = result["text"]
    assert "# 目录" in finalized
    assert "# 医家小传 (1)" not in finalized
    assert "# 月经失调 (5)" not in finalized
    assert "- 医家小传 (1)" in finalized
    assert "- （一）治疗思想 (6)" in finalized
    assert "# 医家小传\n\n正文段落。" in finalized


def test_finalize_standard_document_normalizes_ocr_toc_heading_and_demotes_entries() -> None:
    text = """# 日录

# 医家小传 (1)

万里云大万里路

## ——邓铁涛自传 ………………………………………… (1)

# 专病论治 (7)

## 冠心病 (7)

# 医家小传

正文段落。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "# 日录" not in finalized
    assert "# 目录" in finalized
    assert "# 医家小传 (1)" not in finalized
    assert "## 冠心病 (7)" not in finalized
    assert "- 医家小传 (1)" in finalized
    assert "- 万里云大万里路——邓铁涛自传 ………………………………………… (1)" in finalized
    assert "- 冠心病 (7)" in finalized
    assert "# 医家小传\n\n正文段落。" in finalized


def test_finalize_standard_document_keeps_toc_normalization_idempotent() -> None:
    text = "# 目录\n\n# 医家小传 (1)\n\n# 医家小传\n\n正文段落。\n"

    once = str(finalize_standard_document(text)["text"])
    twice = str(finalize_standard_document(once)["text"])

    assert "- 医家小传 (1)" in twice
    assert "- - 医家小传 (1)" not in twice


def test_finalize_standard_document_collapses_repeated_toc_headings() -> None:
    text = "# 目录\n\n# 医家小传 (1)\n\n# 目录\n\n# 专病论治 (5)\n\n# 医家小传\n\n正文段落。\n"

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert finalized.count("# 目录") == 1
    assert "- 医家小传 (1)" in finalized
    assert "- 专病论治 (5)" in finalized
    assert "# 医家小传\n\n正文段落。" in finalized


def test_finalize_standard_document_keeps_wrapped_toc_entries_out_of_headings() -> None:
    text = """# 目录

## 二十一、冠心病与血液病舌象及其形成机理的对比

## 研究 (228)

# 年谱 (237)

# 医家小传

正文段落。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "## 研究 (228)" not in finalized
    assert "# 年谱 (237)" not in finalized
    assert "- 二十一、冠心病与血液病舌象及其形成机理的对比研究 (228)" in finalized
    assert "- 年谱 (237)" in finalized
    assert "# 医家小传" in finalized


def test_finalize_standard_document_promotes_body_structure_headings_after_toc() -> None:
    text = """# 目录

- 一、全身证状 …… 1
- 1. 恶寒 …… 1
- 2. 发热 …… 3

## 一、全身证状

全身证状，是指全身出现或不限于某一部位的一类证状。

## 1. 恶寒

恶寒即怕冷，一般外感证初期均有怕冷现象。

## 2. 发热

发热即身热，在外感证最为多见。

附录：辨证论治浅说

辨证论治正文。
"""

    result = finalize_standard_document(text)
    finalized = str(result["text"])

    assert "# 目录" in finalized
    assert "- 一、全身证状 …… 1" in finalized
    assert "## 一、全身证状" in finalized
    assert "### 1. 恶寒" in finalized
    assert "### 2. 发热" in finalized
    assert "# 附录：辨证论治浅说" in finalized
    # Body must not remain glued into one giant TOC bullet.
    assert not any(line.startswith("- 一、全身证状全身证状") for line in finalized.splitlines())
    assert "恶寒即怕冷" in finalized
    assert "发热即身热" in finalized


def test_finalize_standard_document_keeps_formula_units_complete_and_separate() -> None:
    text = """# 目录

- 1. 恶寒战栗 …… 2

### 2. 恶寒战栗

恶寒时战栗，简称“寒战”。

复脉汤 人参 地黄 桂枝 麦冬 阿胶 炙草

麻仁 姜 枣真武汤 附子 白芍 白术 茯苓 姜

### 3. 发热

发热即身热。

葱豉汤 豆豉 葱白麻黄汤 麻黄 桂枝 杏仁甘草神术散 苍术 防风 甘草

SS号=11490658

### 1. 恶寒

### 2. 恶寒战栗
"""

    result = finalize_standard_document(text)
    finalized = str(result["text"])
    lines = [line.strip() for line in finalized.splitlines() if line.strip()]

    assert any(line.startswith("### 2. 恶寒战栗") or line.startswith("## 2. 恶寒战栗") for line in lines)
    assert any(line.startswith("复脉汤") and "麻仁" in line and "枣" in line for line in lines)
    assert any(line.startswith("真武汤") for line in lines)
    assert not any("枣真武汤" in line for line in lines)
    assert any(line.startswith("葱豉汤") for line in lines)
    assert any(line.startswith("麻黄汤") or "麻黄汤" in line for line in lines)
    assert any(line.startswith("神术散") for line in lines)
    # Trailing empty heading index / archive metadata must not create empty units.
    assert "SS号=11490658" not in finalized
    assert finalized.count("### 1. 恶寒") == 0
    assert any(change["kind"] == "merged_formula_ingredient_lines" for change in result["changes"]) or any(
        line.startswith("真武汤") for line in lines
    )
    assert any(change["kind"] == "removed_trailing_empty_heading_index_block" for change in result["changes"])


def test_finalize_standard_document_keeps_body_when_year_or_publish_words_appear() -> None:
    """Body prose with years / 出版 must never be mistaken for archive cut points."""
    body = (
        "清热化湿为主治疗尿路结石并左肾功能消失。1962 年初，我们中西医紧密结合，"
        "对患者某，男性，61 岁，尿路结石，做了比较长期的诊治，取得较好效果。"
        "本书于 2005 年出版后广为流传。"
    )
    text = (
        "# 医案\n\n"
        f"{body}\n\n"
        "## 二、续案\n\n"
        "续写医案正文，说明出版说明不等于元数据块。\n"
    )

    result = finalize_standard_document(text)
    finalized = str(result["text"])

    assert "1962 年初" in finalized
    assert "出版" in finalized
    assert "清热化湿" in finalized
    assert "续写医案正文" in finalized
    assert len(finalized) >= 0.5 * len(text)
    assert not any(
        change["kind"] == "removed_trailing_empty_heading_index_block" and change.get("lines")
        for change in result["changes"]
        if isinstance(change, dict)
    )


def test_finalize_standard_document_strips_inline_general_information_without_body_loss() -> None:
    """OCR mega-line: body + [General Information]... must keep body, drop archive."""
    body = (
        "清热化湿为主治疗尿路结石并左肾功能消失。1962 年初，我们中西医紧密结合，"
        "对患者某，男性，61 岁，尿路结石，做了比较长期的诊治，取得较好效果。"
        "大灸疗法治疗虚弱证"
    )
    archive = (
        "[General Information]书名 = 岳美中医案集作者 = 中国中医研究院编"
        "页数 = 159SS号=11490661出版日期 = 2005年10月第1版"
    )
    # Pad with enough lines so the trailing-empty-heading guard is active.
    padding = "\n".join(f"## 案{i}\n\n案{i}正文内容。\n" for i in range(1, 10))
    text = f"# 医案\n\n{padding}\n{body}{archive}\n"

    result = finalize_standard_document(text)
    finalized = str(result["text"])

    assert "1962 年初" in finalized
    assert "大灸疗法治疗虚弱证" in finalized
    assert "清热化湿" in finalized
    assert "[General Information]" not in finalized
    assert "SS号=11490661" not in finalized
    assert "书名 = 岳美中医案集" not in finalized
    assert len(finalized) >= 0.5 * len(text)
    assert any(
        change["kind"] == "removed_trailing_empty_heading_index_block"
        for change in result["changes"]
    )


def test_finalize_standard_document_removes_trailing_sshao_and_general_information_block() -> None:
    text = """# 目录

- 1. 恶寒 …… 2

### 2. 恶寒战栗

恶寒时战栗，简称“寒战”。

复脉汤 人参 地黄 桂枝

### 3. 发热

发热即身热。

SS号=11490658

[General Information]
书名 = 中医临证备要
作者 = 某
页数 = 100
"""

    result = finalize_standard_document(text)
    finalized = str(result["text"])

    assert "恶寒时战栗" in finalized
    assert "复脉汤" in finalized
    assert "SS号=11490658" not in finalized
    assert "[General Information]" not in finalized
    assert "书名 = 中医临证备要" not in finalized
    assert any(
        change["kind"] == "removed_trailing_empty_heading_index_block"
        for change in result["changes"]
    )


def test_finalize_standard_document_removes_trailing_empty_numbered_heading_list() -> None:
    body = "\n".join(f"## 章{i}\n\n章{i}正文。\n" for i in range(1, 12))
    empty_tail = "\n".join(f"### {i}. 空标题{i}\n" for i in range(1, 12))
    text = f"# 正文\n\n{body}\n{empty_tail}\n"

    result = finalize_standard_document(text)
    finalized = str(result["text"])

    assert "章1正文" in finalized
    assert "章11正文" in finalized
    assert "### 1. 空标题1" not in finalized
    assert "### 10. 空标题10" not in finalized
    assert len(finalized) >= 0.5 * len(text)
    assert any(
        change["kind"] == "removed_trailing_empty_heading_index_block"
        for change in result["changes"]
    )


def test_finalize_standard_document_normalizes_index_and_removes_book_title_headers() -> None:
    text = """# 目录

- 1. 恶寒 …… 1

### 1. 恶寒

恶寒即怕冷。

复脉汤 人参 地黄 桂枝

## 中医临证备要

### 2. 发热

发热即身热。

银翘散 荆芥 豆豉 薄荷 银花 连翘 桔梗甘草 竹叶

## 中医临证备要

# 索引

## 二 画

- - 七日风 74

- 子晕 242

(参见 97)

儿枕痛 245

## 三 画

三画子悬 240

亡阳 13

## 中医临证备要

十八画

- - 癲疝 174
- 十九画癣疮 11
"""

    result = finalize_standard_document(text)
    finalized = str(result["text"])

    assert "## 中医临证备要" not in finalized
    assert "# 索引" in finalized
    assert "## 二画" in finalized
    assert "- 七日风 74" in finalized
    assert "- 儿枕痛（参见 97） 245" in finalized
    assert "## 三画" in finalized
    assert "- 子悬 240" in finalized
    assert "## 十八画" in finalized
    assert "- 癲疝 174" in finalized
    assert "## 十九画" in finalized
    assert "- 癣疮 11" in finalized
    assert "- - " not in finalized
    assert "桔梗 甘草" in finalized
    assert any(change["kind"] == "normalized_index_block" for change in result["changes"])
    assert any(change["kind"] == "removed_repeated_book_title_headings" for change in result["changes"])

def test_finalize_standard_document_reconverges_toc_entries_after_final_cleaning() -> None:
    text = """![](images/cover.jpg)

# 临中床医

# 中国百年百名中医临床家丛书

# 谢海洲

图书在版编目(CIP)数据

# 出版者的话

正文段落。

# 前言

正文段落。

![](images/photo.jpg)

# 目 录

# 医家小传 (1)

# 专病论治 (9)

鲜药应用一得 (382)

自创方（化痰通络汤、补肾荣脑汤、补肾活血汤、

柴胡枣仁汤、三黑荣脑汤、神复康、风湿搽剂、

痹痛宁、暖宫促孕汤、席汉氏综合征经验方、发音散、鼻塞通茶、11臭清除剂、尿毒症方）…（388)

年谱 (405)

# 医家小传

正文段落。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "# 年谱 (405)" not in finalized
    assert "## 柴胡枣仁汤" not in finalized
    assert "- 年谱 (405)" in finalized
    assert "- 自创方（化痰通络汤、补肾荣脑汤、补肾活血汤、柴胡枣仁汤" in finalized
    assert "# 医家小传" in finalized
    assert "toc_page_heading_residue" not in quality_errors(finalized)


def test_finalize_standard_document_normalizes_unheaded_toc_page_block() -> None:
    text = """# 出版者的话

正文。

# 医家小传/1

# 专病论治/9

咳嗽 11

诊余漫话 / 103

年谱/293

![](images/photo.jpg)

张珍玉，1920年出生。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    twice = str(finalize_standard_document(finalized)["text"])
    assert "# 目录" in finalized
    assert "# 医家小传/1" not in finalized
    assert "# 专病论治/9" not in finalized
    assert "- 医家小传/1" in finalized
    assert "- 咳嗽 11" in finalized
    assert "- 诊余漫话 / 103" in finalized
    assert "![](images/photo.jpg)" in finalized
    assert "- ![](images/photo.jpg)" not in finalized
    assert "- 张珍玉，1920年出生。" not in twice
    assert twice == finalized
    assert "toc_page_heading_residue" not in quality_errors(finalized)


def test_finalize_standard_document_keeps_unheaded_split_toc_entry_open() -> None:
    text = """# 出版者的话

正文。

医家小传 (1)

诊余漫话 (227)

肿瘤患者心理调护五法 (256)

余桂清教授中西医结合防治肿瘤的业绩及学术思想

## (262)

# 年谱 (269)

# 医家小传

正文段落。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "- 余桂清教授中西医结合防治肿瘤的业绩及学术思想(262)" in finalized
    assert "- 年谱 (269)" in finalized
    assert "## (262)" not in finalized
    assert "# 年谱 (269)" not in finalized
    assert "# 医家小传\n\n正文段落。" in finalized
    assert "toc_page_heading_residue" not in quality_errors(finalized)


def test_finalize_standard_document_normalizes_heading_levels() -> None:
    text = "一、第一章\n正文。\n\n1. 第一节\n正文。\n\n（1）第一条\n正文。\n"

    result = finalize_standard_document(text)

    finalized = result["text"]
    assert "# 一、第一章" in finalized
    assert "## 1. 第一节" in finalized
    assert "#### （1）第一条" in finalized or "### （1）第一条" in finalized


def test_finalize_standard_document_normalizes_nested_markdown_heading_marker() -> None:
    text = "## #太阳病提要\n\n## 一、本病\n\n### （一）表证（经病）\n\n脉浮，头项强痛而恶寒。\n"

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "#太阳病提要" not in finalized
    assert "# 太阳病提要" in finalized
    assert "invalid_heading_syntax" not in quality_errors(finalized)
    assert "heading_level_jump" not in quality_errors(finalized)


def test_finalize_standard_document_repairs_heading_marker_without_space() -> None:
    text = "#第四节疫 疡\n\n正文。\n\n#辨证是治疗的根据，治疗是辨证的结果。因势利导是根据外界致病因素引起的伤寒病。\n"

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "# 第四节疫 疡" in finalized
    assert "#辨证是治疗的根据" not in finalized
    assert "辨证是治疗的根据，治疗是辨证的结果。" in finalized
    assert "invalid_heading_syntax" not in quality_errors(finalized)


def test_finalize_standard_document_removes_nested_empty_heading_marker() -> None:
    text = "# 标题\n\n正文。\n\n## #\n\n下一段。\n"

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "## #" not in finalized
    assert "下一段。" in finalized


def test_finalize_standard_document_demotes_standalone_figure_number_headings() -> None:
    text = """# 手术治疗经验

![](images/a.jpg)

(1)

![](images/b.jpg)

## (2)

图15 直肠狭窄挂线法

# 操作要点

正文段落。
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "## (1)" not in finalized
    assert "## (2)" not in finalized
    assert "(1)" in finalized
    assert "(2)" in finalized
    assert "toc_page_heading_residue" not in quality_errors(finalized)


def test_finalize_standard_document_caps_heading_level_jumps() -> None:
    text = "# 月经失调\n\n### （一）治疗思想\n\n正文。\n\n# 闭经\n\n### （一）治疗原则\n\n正文。\n"

    result = finalize_standard_document(text)

    finalized = result["text"]
    lines = finalized.splitlines()
    assert "## （一）治疗思想" in lines
    assert "## （一）治疗原则" in lines
    assert "### （一）治疗思想" not in lines
    assert "### （一）治疗原则" not in lines
    assert "heading_level_jump" not in quality_errors(finalized)


def test_finalize_standard_document_removes_empty_heading_markers() -> None:
    text = "# 正文标题\n\n正文。\n\n#\n\n下一段。\n"

    result = finalize_standard_document(text)

    finalized = result["text"]
    assert "\n#\n" not in finalized
    assert "# 正文标题" in finalized
    assert "下一段。" in finalized
    assert "invalid_heading_syntax" not in quality_errors(finalized)


def test_finalize_standard_document_demotes_nonstandard_heading_content() -> None:
    text = "# 医家小传\n\n正文段落。\n\n# ※※※※※\n\n编者按正文。\n\n# 麦冬10克 鱼腥草10克\n\n方义正文。\n"

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "# ※※※※※" not in finalized
    assert "※※※※※" not in finalized
    assert "# 麦冬10克 鱼腥草10克" not in finalized
    assert "麦冬10克 鱼腥草10克" in finalized
    assert "nonstandard_heading_content" not in quality_errors(finalized)


def test_finalize_standard_document_keeps_demoted_mixed_parenthesis_dosage_lines_plain() -> None:
    text = "# 医家小传\n\n正文段落。\n\n## (2）紫硝砂 9g 紫金锭 30g\n\n两药研细粉混匀。\n\n## (2）黄柏 10g 青黛 10g\n\n研细末外涂。\n"

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "## (2）紫硝砂 9g 紫金锭 30g" not in finalized
    assert "## (2）黄柏 10g 青黛 10g" not in finalized
    assert "(2）紫硝砂 9g 紫金锭 30g" in finalized
    assert "(2）黄柏 10g 青黛 10g" in finalized
    assert "nonstandard_heading_content" not in quality_errors(finalized)


def test_finalize_standard_document_removes_cover_ocr_noise_before_front_matter() -> None:
    text = "# 临家中床医\n\n# 草真\n\n编\n著\n郑\nTCM999.5d6d.com\n\n# 出版者的话\n\n正文段落。\n\n# 中国百年百名中医临床家丛书\n\n主编张文康\n"

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "# 临家中床医" not in finalized
    assert "# 草真" not in finalized
    assert "TCM999.5d6d.com" not in finalized
    assert finalized.startswith("# 出版者的话")
    assert "nonstandard_heading_content" not in quality_errors(finalized)


def test_finalize_standard_document_removes_trailing_cover_ocr_heading_noise() -> None:
    text = "# 医家小传\n\n正文段落。\n\n# 临中床医\n"

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "# 临中床医" not in finalized
    assert finalized.rstrip().endswith("正文段落。")
    assert "nonstandard_heading_content" not in quality_errors(finalized)
    assert any(change["kind"] == "removed_trailing_cover_ocr_heading_noise" for change in result["changes"])


def test_finalize_standard_document_removes_trailing_cover_ocr_tail_block() -> None:
    text = "# 年谱\n\n2001年获世界华人交流会优秀论文奖。\n\n# 临中床医\n\n![](images/cover.jpg)\n定价：15.00元\n"

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "# 临中床医" not in finalized
    assert "![](images/cover.jpg)" not in finalized
    assert "定价：15.00元" not in finalized
    assert finalized.rstrip().endswith("2001年获世界华人交流会优秀论文奖。")
    assert "nonstandard_heading_content" not in quality_errors(finalized)
    assert any(change["kind"] == "removed_trailing_cover_ocr_heading_noise" for change in result["changes"])


def test_finalize_standard_document_removes_trailing_publication_metadata_block() -> None:
    body = "\n\n".join(f"正文段落{i}。" for i in range(40))
    text = f"""# 出版者的话

正文段落。

{body}

# 年谱

1984年3月1日病逝。

# 图书在版编目(CIP)数据

胡希恕/冯世纶主编.一北京：中国中医药出版社，2001.1

ISBN 7-80156-150-3

中国版本图书馆CIP数据核字(2000)第59979号

发行者：中国中医药出版社

印刷者：衡水华兴印刷有限责任公司

定价：10.00元

# 临中床医

![](images/cover.jpg)

# 中国百年百名中医临床家丛书

# 目录

- 医家小传 (1)
"""

    result = finalize_standard_document(text)

    finalized = str(result["text"])
    assert "# 图书在版编目(CIP)数据" not in finalized
    assert "# 临中床医" not in finalized
    assert "# 中国百年百名中医临床家丛书" not in finalized
    assert finalized.rstrip().endswith("1984年3月1日病逝。")
    assert "nonstandard_heading_content" not in quality_errors(finalized)
    assert any(change["kind"] == "removed_trailing_publication_metadata_noise" for change in result["changes"])
    assert any(change["kind"] == "removed_trailing_publication_metadata_noise" for change in result["changes"])


def test_final_acceptance_report_blocks_nonstandard_heading_content() -> None:
    text = "# 医家小传\n\n正文段落。\n\n# ※※※※※\n\n# 麦冬10克 鱼腥草10克\n\n方义正文。\n"

    report = final_acceptance_report(text)

    assert report["accepted"] is False
    assert "nonstandard_heading_content" in report["blocking_errors"]


def test_final_acceptance_report_blocks_cover_ocr_heading_residue() -> None:
    text = "# 临家中床医\n\n# 出版者的话\n\n正文段落。\n"

    report = final_acceptance_report(text)

    assert report["accepted"] is False
    assert "nonstandard_heading_content" in report["blocking_errors"]


def test_final_acceptance_report_blocks_repeated_toc_heading_residue() -> None:
    text = "# 目录\n\n- 医家小传 (1)\n\n# 目录\n\n- 专病论治 (5)\n\n# 医家小传\n\n正文段落。\n"

    report = final_acceptance_report(text)

    assert report["accepted"] is False
    assert "repeated_toc_heading_residue" in report["blocking_errors"]


def test_final_acceptance_report_blocks_series_title_page_heading() -> None:
    text = "# 医家小传\n\n正文段落。\n\n# 中国百年百名中医临床家丛书\n\n# （按姓氏笔画排列）\n"

    report = final_acceptance_report(text)

    assert report["accepted"] is False
    assert "nonstandard_heading_content" in report["blocking_errors"]


def test_final_acceptance_report_blocks_unstructured_metadata_before_first_heading() -> None:
    text = """![](images/person.jpg)
叶熙春/李学铭主编.一北京：中国中医药出版社，2004.8

（中国百年百名中医临床家丛书）

ISBN 7-80156-646-7

中国版本图书馆CIP数据核字(2004)第072859号

发行者：中国中医药出版社

印刷者：北京泰锐印刷有限公司

书号：ISBN7-80156-646-7/R·646

定价：11.00元

# 出版者的话

正文段落。
"""

    report = final_acceptance_report(text)

    assert report["accepted"] is False
    assert "unstructured_front_matter_prefix" in report["blocking_errors"]


def test_final_acceptance_report_blocks_any_text_before_first_heading() -> None:
    text = "主编张文康\n\n# 出版者的话\n\n正文段落。\n"

    report = final_acceptance_report(text)

    assert report["accepted"] is False
    assert "unstructured_front_matter_prefix" in report["blocking_errors"]


def test_final_acceptance_report_blocks_series_title_page_before_front_matter() -> None:
    text = "# 中国百年百名中医临床家丛书\n\n主\n\n编\n\n张文康\n\n# 中国中医药出版社\n\n# 出版者的话\n\n正文段落。\n"

    report = final_acceptance_report(text)

    assert report["accepted"] is False
    assert "unstructured_front_matter_prefix" in report["blocking_errors"]


def test_final_acceptance_report_allows_publisher_signature_inside_preface() -> None:
    text = "# 出版者的话\n\n正文段落。\n\n中国中医药出版社\n\n2000年10月28日\n\n# 前言\n\n前言正文。\n"

    report = final_acceptance_report(text)

    assert report["accepted"] is True
    assert "unstructured_front_matter_prefix" not in report["blocking_errors"]


def test_final_acceptance_report_blocks_unheaded_toc_page_heading_residue() -> None:
    text = "# 医家小传/1\n\n# 专病论治/9\n\n正文段落。\n"

    report = final_acceptance_report(text)

    assert report["accepted"] is False
    assert "toc_page_heading_residue" in report["blocking_errors"]


def test_final_acceptance_report_passes_clean_text() -> None:
    text = "# 标题\n\n正文段落包含标点符号。\n\n另一段落也符合规范。\n"

    report = final_acceptance_report(text)

    assert report["status"] == "passed"
    assert report["accepted"] is True
    assert not report["blocking_errors"]


def test_final_acceptance_report_blocks_mojibake_and_replacement_chars() -> None:
    text = "锟斤拷乱码文本�替换符\n"

    report = final_acceptance_report(text)

    assert report["status"] == "needs_revision"
    assert report["accepted"] is False
    assert "mojibake" in report["blocking_errors"]
    assert "replacement_characters" in report["blocking_errors"]


def test_final_acceptance_report_blocks_forced_line_breaks() -> None:
    text = "这是一个普通段落的第一部分\n第二部分仍然是同一个句子\n第三部分才结束。\n"

    report = final_acceptance_report(text)

    assert report["status"] == "needs_revision"
    assert report["accepted"] is False
    assert "forced_line_breaks" in report["blocking_errors"]


def test_final_acceptance_report_blocks_headingless_long_document() -> None:
    text = "正文" * 1000 + "\n"

    report = final_acceptance_report(text)

    assert report["status"] == "needs_revision"
    assert "headingless_long_document" in report["blocking_errors"]


def test_final_acceptance_report_blocks_mega_line_with_single_heading() -> None:
    # One legitimate heading is not enough if the body is a whole-book mega line.
    mega_body = "这是粘连正文。" * 500  # 3500 chars
    text = f"# 目录\n\n{mega_body}\n"

    report = final_acceptance_report(text)

    assert report["accepted"] is False
    assert "mega_line" in report["blocking_errors"]
    assert report["hints"]["max_line_chars"] >= 3000


def test_final_acceptance_report_blocks_low_heading_density() -> None:
    # Long document with only a handful of headings (collapsed structure signature).
    # Keep each line short so mega_line does not dominate the failure mode.
    parts = [f"正文段落，包含标点。段落{i}。" for i in range(3000)]
    body = "\n\n".join(parts)
    text = "# 前言\n\n说明。\n\n# 目录\n\n- 条目\n\n# 正文\n\n" + body + "\n\n# 附录\n\n结束。\n"

    report = final_acceptance_report(text)

    assert report["accepted"] is False
    assert "low_heading_density" in report["blocking_errors"]
    assert report["hints"]["heading_count"] < 5
    assert report["hints"]["max_line_chars"] < 3000


def test_soft_join_does_not_cross_headings_or_exceed_cap() -> None:
    from llmcheck.cleaning import _merge_soft_wrapped_lines

    # Adjacent prose would join, but heading boundary must stay.
    lines = [
        "这是上一段没有句号",
        "# 下一节",
        "从这里开始新的段落内容仍然很长",
    ]
    merged = _merge_soft_wrapped_lines(lines)
    assert any(line.strip().startswith("# 下一节") for line in merged)

    # Soft join must not produce an unbounded mega line.
    left = "甲" * 1900
    right = "乙" * 200
    merged_long = _merge_soft_wrapped_lines([left, right])
    assert max(len(line) for line in merged_long) <= 2100


def test_prefer_better_structure_text_keeps_cleaned_on_collapse() -> None:
    from llmcheck.pipeline import _prefer_better_structure_text

    cleaned = "# 章一\n\n" + "\n\n".join(f"## 节{i}\n\n内容{i}。" for i in range(1, 40))
    collapsed = "# 目录\n\n" + ("粘连正文。" * 5000)
    chosen, guard = _prefer_better_structure_text(cleaned=cleaned, candidate=collapsed)
    assert guard["used_cleaned_fallback"] is True
    assert chosen == cleaned
    assert "candidate_mega_line" in guard["reasons"] or "candidate_heading_collapse" in guard["reasons"]


def test_final_acceptance_report_provides_quality_hints() -> None:
    text = "正文段落。\n"

    report = final_acceptance_report(text)

    assert "hints" in report
    hints = report["hints"]
    assert "total_chars" in hints
    assert "chinese_chars" in hints
    assert "punctuation_density" in hints


def test_normalize_document_structure_delegates_to_finalize_standard_document() -> None:
    text = "# 标题\n正文。\n"

    result = normalize_document_structure(text)

    assert result["status"] == "finalized"
    assert "text" in result


def test_rule_registry_contains_expected_rules() -> None:
    assert "latex.strip_empty_math" in RULE_REGISTRY
    assert "latex.unit_math_to_text" in RULE_REGISTRY
    assert "markdown.heading_spacing" in RULE_REGISTRY
    assert "paragraph.safe_line_join" in RULE_REGISTRY
    assert "artifact.zero_width_remove" in RULE_REGISTRY
    assert "artifact.mojibake_detect" in RULE_REGISTRY


def test_rule_registry_entries_have_required_fields() -> None:
    for rule_id, rule in RULE_REGISTRY.items():
        assert rule.rule_id == rule_id
        assert rule.description
        assert rule.risk_level in {"low", "medium", "high"}
        assert rule.write_mode in {"auto_apply", "report_only", "block"}


def test_batch_proven_repairs_merge_forced_breaks() -> None:
    from llmcheck.repair import merge_forced_breaks
    from llmcheck.final_gate import final_acceptance_report

    text = "这是一个普通段落的第一部分仍然没有结束\n第二部分继续同一句直到结束。\n"
    fixed = merge_forced_breaks(text)
    assert "第一部分仍然没有结束第二部分继续" in fixed.replace(" ", "")
    rep = final_acceptance_report("# 标题\n\n" + fixed + "\n")
    assert "forced_line_breaks" not in rep["blocking_errors"]


def test_batch_proven_repairs_demote_toc_latex() -> None:
    from llmcheck.repair import (
        demote_nonstandard_headings,
        fix_toc_page_headings,
        strip_latex_artifacts,
        apply_batch_proven_repairs,
    )

    demoted = demote_nonstandard_headings("## ※※※\n\n正文段落。\n")
    assert "※※※" in demoted
    assert not any(line.strip().startswith("## ※") for line in demoted.splitlines())

    toc = fix_toc_page_headings("## 头痛 …… 12\n\n# 正文\n\n说明文字。\n")
    assert toc.splitlines()[0].startswith("- ")

    latex = strip_latex_artifacts("体温 $37$ \\mathrm{C} 正常。\n")
    assert "$" not in latex
    assert "mathrm" not in latex

    text = "# 标题\n\n上半句没有结束\n下半句才结束。\n"
    fixed, labels = apply_batch_proven_repairs(text)
    assert isinstance(labels, list)
    assert isinstance(fixed, str)


def test_finalize_runs_batch_proven_repairs_accepts_forced_wrap() -> None:
    from llmcheck.structure import finalize_standard_document
    from llmcheck.final_gate import final_acceptance_report

    raw = "# 导言\n\n这是一个普通段落的第一部分仍然没有结束\n第二部分继续同一句直到结束。\n"
    result = finalize_standard_document(raw)
    text = str(result.get("text") or "")
    rep = final_acceptance_report(text)
    assert rep["accepted"] is True
    assert "forced_line_breaks" not in rep["blocking_errors"]


def test_split_overlong_lines_under_pack_limit() -> None:
    from llmcheck.repair import split_overlong_lines

    line = (("论述内容，" * 20) + "。") * 30
    assert len(line) > 2500
    fixed = split_overlong_lines("# 题\n\n" + line + "\n", max_len=2490)
    assert max(len(l) for l in fixed.splitlines()) <= 2490
