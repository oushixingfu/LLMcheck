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
        glue_markers=(
            "头部：",
            "面部：",
            "颈部：",
            "胸胁部：",
            "腹部：",
            "腰背部：",
            "肩部：",
            "肘部：",
            "腕手部：",
            "髋部：",
            "膝部：",
            "踝部：",
            "足部：",
        ),
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
