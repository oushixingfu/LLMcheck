from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any
import hashlib
import http.client
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request

from llmcheck.final_gate import quality_hints
from llmcheck.profiles import DocumentProfile, get_profile


class CircuitBreaker:
    """Tracks consecutive LLM failures and opens the circuit after a threshold.

    When open, all calls are rejected immediately (fast-fail) until the
    recovery timeout elapses, at which point a single half-open probe is
    allowed.  A successful probe resets the breaker to closed.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 60.0,
    ) -> None:
        self._failure_threshold = max(1, failure_threshold)
        self._recovery_timeout = recovery_timeout_seconds
        self._consecutive_failures = 0
        self._opened_at: float = 0.0
        self._lock = Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._consecutive_failures < self._failure_threshold:
                return False
            if time.monotonic() - self._opened_at >= self._recovery_timeout:
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_threshold:
                self._opened_at = time.monotonic()


@dataclass(frozen=True)
class LlmConfig:
    api_url: str
    api_key: str
    model: str
    timeout_seconds: int = 600
    max_calls_per_book: int = 0


class LlmClient:
    def __init__(self, config: LlmConfig) -> None:
        self.config = config
        self._call_lock = Lock()
        self._call_count = 0
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=int(os.environ.get("LLMCHECK_CIRCUIT_FAILURE_THRESHOLD", "5")),
            recovery_timeout_seconds=float(os.environ.get("LLMCHECK_CIRCUIT_RECOVERY_SECONDS", "60")),
        )

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit_breaker

    @property
    def call_count(self) -> int:
        with self._call_lock:
            return self._call_count

    def _reserve_call(self) -> dict[str, Any] | None:
        max_calls = max(0, int(self.config.max_calls_per_book))
        with self._call_lock:
            if max_calls and self._call_count >= max_calls:
                return {
                    "status": "error",
                    "error": f"LLM call budget exceeded: {self._call_count}/{max_calls}",
                    "code": "llm_call_budget_exceeded",
                    "call_count": self._call_count,
                    "max_calls_per_book": max_calls,
                }
            self._call_count += 1
            return None

    def _max_attempts(self) -> int:
        raw_value = os.environ.get("LLMCHECK_LLM_RETRIES")
        if raw_value is None:
            return 3
        try:
            return max(1, int(raw_value))
        except ValueError:
            return 3

    def complete_json(self, prompt: str) -> dict[str, Any]:
        if self._circuit_breaker.is_open:
            return {"status": "error", "error": "LLM circuit breaker is open (consecutive failures exceeded threshold)"}
        budget_error = self._reserve_call()
        if budget_error is not None:
            return budget_error
        result = self._complete_json_with_curl(prompt) if os.environ.get("LLMCHECK_LLM_TRANSPORT") == "curl" else self._complete_json_with_urllib(prompt)
        if result.get("status") == "error":
            self._circuit_breaker.record_failure()
        else:
            self._circuit_breaker.record_success()
        return result

    def _complete_json_with_urllib(self, prompt: str) -> dict[str, Any]:
        url = self.config.api_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"
        payload = {
            "model": self.config.model,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
        )
        raw_response = ""
        max_attempts = self._max_attempts()
        for attempt in range(1, max_attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                    raw_response = response.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as error:
                if error.code in {408, 429, 500, 502, 503, 504} and attempt < max_attempts:
                    time.sleep(attempt * 2)
                    continue
                return {"status": "error", "error": f"HTTP Error {error.code}: {error.reason}"}
            except (
                TimeoutError,
                urllib.error.URLError,
                http.client.RemoteDisconnected,
                http.client.IncompleteRead,
                ConnectionResetError,
                BrokenPipeError,
                socket.timeout,
            ) as error:
                if attempt < max_attempts:
                    time.sleep(attempt * 2)
                    continue
                return {"status": "error", "error": str(error)}
            try:
                data = json.loads(raw_response)
                return parse_json_object(_extract_message_text(data))
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                if attempt < max_attempts:
                    time.sleep(attempt * 2)
                    continue
                return {"status": "error", "error": f"invalid LLM response: {error}", "raw_response_chars": len(raw_response)}
        return {"status": "error", "error": "empty LLM response", "raw_response_chars": len(raw_response)}

    def _complete_json_with_curl(self, prompt: str) -> dict[str, Any]:
        url = self.config.api_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"
        payload = {
            "model": self.config.model,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }
        raw_response = ""
        timeout = max(1, int(self.config.timeout_seconds))
        max_attempts = self._max_attempts()
        for attempt in range(1, max_attempts + 1):
            try:
                completed = subprocess.run(
                    [
                        "curl",
                        "--silent",
                        "--show-error",
                        "--max-time",
                        str(timeout),
                        "--write-out",
                        "\nHTTP_STATUS:%{http_code}",
                        url,
                        "-H",
                        f"Authorization: Bearer {self.config.api_key}",
                        "-H",
                        "Content-Type: application/json",
                        "--data-binary",
                        json.dumps(payload, ensure_ascii=False),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout + 5,
                )
            except subprocess.TimeoutExpired:
                if attempt < max_attempts:
                    time.sleep(attempt * 2)
                    continue
                return {"status": "error", "error": f"LLM request timed out after {timeout} seconds"}
            if completed.returncode != 0:
                if attempt < max_attempts:
                    time.sleep(attempt * 2)
                    continue
                message = (completed.stderr or completed.stdout or "").strip()
                return {"status": "error", "error": message or f"curl exited {completed.returncode}"}
            raw_response, http_status = _split_curl_response(completed.stdout)
            status_code = int(http_status) if http_status.isdigit() else 0
            if status_code in {408, 429, 500, 502, 503, 504} and attempt < max_attempts:
                time.sleep(attempt * 2)
                continue
            if status_code < 200 or status_code >= 300:
                reason = completed.stderr.strip() or "LLM request failed"
                return {"status": "error", "error": f"HTTP Error {status_code}: {reason}"}
            try:
                data = json.loads(raw_response)
                return parse_json_object(_extract_message_text(data))
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                if attempt < max_attempts:
                    time.sleep(attempt * 2)
                    continue
                return {"status": "error", "error": f"invalid LLM response: {error}", "raw_response_chars": len(raw_response)}
        return {"status": "error", "error": "empty LLM response", "raw_response_chars": len(raw_response)}


def _split_curl_response(output: str) -> tuple[str, str]:
    marker = "\nHTTP_STATUS:"
    if marker not in output:
        return output, ""
    body, status = output.rsplit(marker, 1)
    return body, status.strip()


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
        + lines("glue_markers:", profile.glue_markers)
    )


def build_correction_prompt(*, source_name: str, text_path: Path, text: str, profile: DocumentProfile | None = None) -> str:
    document_profile = profile or get_profile()
    return (
        "你是文档清洗与结构规范化编辑。任务：通读本次输入文本，修正 OCR/清洗残留造成的错别字、缺标点、异常分段、正文粘连、乱码、异常空格和强制换行。\n"
        "\n"
        "硬性规则：\n"
        "1. corrected_text 必须是本次输入文本的完整纠正文，不是摘要或 diff；如果源文件名显示为第 N/M 片段，只返回该片段完整纠正文，不要因为片段边界判定为截断。\n"
        "2. 只能修正明显 OCR/清洗/排版问题，不得补写源文档未出现内容。\n"
        "3. 不得摘要化、现代化改写或删改事实、数字、术语、编号、页码线索中的实质信息。\n"
        "4. 标题、目录、条目、表格和关键结构可以保留自然换行；普通正文自然段内不得保留 OCR 物理折行。\n"
        "5. 不确定内容保留原文，并写入 unresolved_issues。\n"
        "\n"
        "文档 profile：\n"
        f"{_profile_prompt_block(document_profile)}"
        "\n"
        "请只返回 JSON，不要 Markdown，不要代码块。格式：\n"
        "{\n"
        '  "status": "draft_ready" 或 "needs_manual_review",\n'
        '  "confidence": 0.0 到 1.0,\n'
        '  "summary": "一句中文结论",\n'
        '  "corrected_text": "本次输入文本的完整候审正文",\n'
        '  "changes": [{"location_hint": "章节/页码/行号线索", "before": "短摘录", "after": "短摘录", "reason": "修改原因"}],\n'
        '  "unresolved_issues": [{"location_hint": "章节/页码/行号线索", "excerpt": "短摘录", "reason": "为何不能自动修"}]\n'
        "}\n"
        f"\n源文件：{source_name}\n文本路径：{text_path}\n"
        f"程序只读提示（不能代替你的判断）：{json.dumps(quality_hints(text), ensure_ascii=False)}\n"
        "\n待纠错文本如下：\n<TEXT_BEGIN>\n"
        f"{text}\n"
        "<TEXT_END>\n"
    )


def build_acceptance_prompt(*, source_name: str, text_path: Path, text: str, profile: DocumentProfile | None = None) -> str:
    document_profile = profile or get_profile()
    return (
        "你是标准文档最终验收员。请通读本次输入文本，判断该文本是否可以交付给后续知识抽取和人工阅读；如果源文件名显示为第 N/M 片段，只验收该片段，不要因为片段边界判定为截断。\n"
        "\n"
        "验收标准：\n"
        "1. 标点、断句、分段达到人类可读；不存在大段正文粘连、乱码或异常空格。\n"
        "2. 普通正文自然段内不得保留 OCR 物理折行。\n"
        "3. 标题、列表、表格、引用、注释和关键结构应尽量清晰。\n"
        "4. 不要求现代化润色，只判断文本是否忠实、可读、可继续使用。\n"
        "5. 不应残留强制换行、扫描噪声或无依据扩写。\n"
        "6. 保留图片链接、图表占位、无法确认的缺字/疑似原文问题或 unresolved 说明是允许的；不能因为需要回查原图、外部底本或人工校勘才判定为阻断。\n"
        "7. 对古籍、术语、表格和命盘类内容，不得凭领域知识要求改字、补月份、补表格或重排源文不可确认的内容；只在文本本身明显不可读、乱码化、严重粘连或 Markdown 结构破坏时才标记 needs_revision。\n"
        "\n"
        "文档 profile：\n"
        f"{_profile_prompt_block(document_profile)}"
        "\n"
        "请只返回 JSON，不要 Markdown，不要代码块。格式：\n"
        "{\n"
        '  "status": "passed" 或 "needs_revision",\n'
        '  "confidence": 0.0 到 1.0,\n'
        '  "summary": "一句中文结论",\n'
        '  "blocking_issues": [{"category": "punctuation|layout|ocr_noise|missing_text|other", "severity": "high|medium", "location_hint": "页码/章节/行号线索", "excerpt": "短摘录", "reason": "为什么阻断", "suggested_action": "如何修"}],\n'
        '  "non_blocking_notes": ["..."]\n'
        "}\n"
        f"\n源文件：{source_name}\n文本路径：{text_path}\n"
        f"程序只读提示（不能代替你的判断）：{json.dumps(quality_hints(text), ensure_ascii=False)}\n"
        "\n待验收文本如下：\n<TEXT_BEGIN>\n"
        f"{text}\n"
        "<TEXT_END>\n"
    )


def build_review_prompt(*, source_name: str, text_path: Path, text: str, profile: DocumentProfile | None = None) -> str:
    document_profile = profile or get_profile()
    return (
        "你是标准 Markdown 文档的人类视角审查员。任务：从交付质量角度审查当前文本，识别一眼可见的问题，并给出结构化 issue 列表。\n"
        "\n"
        "审查重点：\n"
        "1. Markdown 标题、段落、列表、表格等结构是否明显损坏。\n"
        "2. 是否残留 LaTeX/公式/单位噪声，例如 \\mathrm、\\circ 或多余的 $。\n"
        "3. 是否存在明显 OCR 噪声、乱码、错行、粘连、异常空格或结构断裂。\n"
        "4. 是否存在会影响交付的内容缺失风险、目录层级问题或表格破坏。\n"
        "5. 只报告显著问题；无法确认时保守处理。\n"
        "\n"
        "硬性规则：\n"
        "1. 只输出 issues，不得输出 corrected_text、repaired_text 或任何全文改写结果。\n"
        "2. 不得凭空补写原文，不得因为追求通顺而重写正文。\n"
        "3. accepted 只有在没有 blocking/major 级问题时才可为 true。\n"
        "4. safe_fix_type 只能使用 rule_fix、safe_llm_patch、manual_review、none。\n"
        "\n"
        "文档 profile：\n"
        f"{_profile_prompt_block(document_profile)}"
        "\n"
        "请只返回 JSON，不要 Markdown，不要代码块。格式：\n"
        "{\n"
        '  "status": "reviewed",\n'
        '  "accepted": true 或 false,\n'
        '  "confidence": 0.0 到 1.0,\n'
        '  "summary": "一句中文结论",\n'
        '  "issues": [{\n'
        '    "id": "issue-001",\n'
        '    "category": "latex_artifact|markdown_structure|heading_hierarchy|paragraph_break|ocr_noise|mojibake|table_broken|content_loss_risk|pdf_md_mismatch|model_uncertain|other",\n'
        '    "severity": "blocking|major|minor",\n'
        '    "location_hint": "章节/页码/行号线索",\n'
        '    "excerpt": "短摘录",\n'
        '    "reason": "为什么这是问题",\n'
        '    "suggested_action": "建议动作",\n'
        '    "safe_fix_type": "rule_fix|safe_llm_patch|manual_review|none"\n'
        "  }],\n"
        '  "manual_review_notes": ["..."]\n'
        "}\n"
        f"\n源文件：{source_name}\n文本路径：{text_path}\n"
        f"程序只读提示（不能代替你的判断）：{json.dumps(quality_hints(text), ensure_ascii=False)}\n"
        "\n待审查 Markdown 文本如下：\n<TEXT_BEGIN>\n"
        f"{text}\n"
        "<TEXT_END>\n"
    )


def review_result_payload(
    *,
    source_name: str,
    text_path: Path,
    text: str,
    client: Any,
    model: str,
    profile: DocumentProfile | None = None,
) -> dict[str, Any]:
    document_profile = profile or get_profile()
    prompt = build_review_prompt(source_name=source_name, text_path=text_path, text=text, profile=document_profile)
    result = client.complete_json(prompt)
    if result.get("status") == "error":
        return {
            "source_name": source_name,
            "text_path": str(text_path),
            "content_sha256": _sha256(text),
            "prompt_sha256": _sha256(prompt),
            "profile_id": document_profile.id,
            "model": model,
            "status": "error",
            "accepted": False,
            "issues": [],
            "manual_review_notes": [],
            "llm_result": result,
        }
    raw_issues = result.get("issues")
    issues = [dict(issue) for issue in raw_issues if isinstance(issue, dict)] if isinstance(raw_issues, list) else []
    raw_manual_review_notes = result.get("manual_review_notes")
    manual_review_notes = (
        [note.strip() for note in raw_manual_review_notes if isinstance(note, str) and note.strip()]
        if isinstance(raw_manual_review_notes, list)
        else []
    )
    status = str(result.get("status") or "error")
    accepted = status == "reviewed" and bool(result.get("accepted")) and not _has_blocking_review_issues(issues)
    return {
        "source_name": source_name,
        "text_path": str(text_path),
        "content_sha256": _sha256(text),
        "prompt_sha256": _sha256(prompt),
        "profile_id": document_profile.id,
        "model": model,
        "status": status,
        "accepted": accepted,
        "issues": issues,
        "manual_review_notes": manual_review_notes,
        "llm_result": result,
    }


def _has_blocking_review_issues(issues: list[dict[str, Any]]) -> bool:
    return any(str(issue.get("severity") or "").strip().lower() in {"blocking", "major"} for issue in issues)


def build_repair_prompt(
    *,
    source_name: str,
    text_path: Path,
    text: str,
    acceptance_issue: dict[str, Any],
    previous_text: str = "",
    next_text: str = "",
    audit_text: str = "",
    profile: DocumentProfile | None = None,
) -> str:
    document_profile = profile or get_profile()
    context_payload = {
        "acceptance_issue": acceptance_issue,
        "previous_text_excerpt": previous_text[-1200:],
        "next_text_excerpt": next_text[:1200],
        "audit_text_excerpt": audit_text[:4000],
    }
    return (
        "你是标准文档验收返修员。任务：只针对本片段的阻断验收问题，输出本片段完整返修文本。\n"
        "\n"
        "硬性规则：\n"
        "1. repaired_text 必须是本片段完整文本，不是摘要或 diff。\n"
        "2. 优先依据本片段、相邻片段和 PPX 审计文本修正 OCR/排版/漏识问题。\n"
        "3. 不得改写源文档实质；不得为了通顺大段新增源文档未提供内容。\n"
        "4. 若验收意见指出固定枚举或固定配属缺项，且上下文已经给出完整序列，可按上下文补齐明显缺项，并在 changes 写明依据。\n"
        "5. 若无法可靠修复，保持原文并在 unresolved_issues 说明原因。\n"
        "\n"
        "文档 profile：\n"
        f"{_profile_prompt_block(document_profile)}"
        "\n"
        "请只返回 JSON，不要 Markdown，不要代码块。格式：\n"
        "{\n"
        '  "status": "repaired" 或 "needs_manual_review",\n'
        '  "confidence": 0.0 到 1.0,\n'
        '  "summary": "一句中文结论",\n'
        '  "repaired_text": "本片段完整返修文本",\n'
        '  "changes": [{"location_hint": "章节/行号线索", "before": "短摘录", "after": "短摘录", "reason": "修改依据"}],\n'
        '  "unresolved_issues": [{"location_hint": "章节/行号线索", "excerpt": "短摘录", "reason": "为何仍需人工核对"}]\n'
        "}\n"
        f"\n源文件：{source_name}\n文本路径：{text_path}\n"
        f"返修上下文：{json.dumps(context_payload, ensure_ascii=False)}\n"
        "\n待返修片段如下：\n<TEXT_BEGIN>\n"
        f"{text}\n"
        "<TEXT_END>\n"
    )


def correction_result_payload(
    *,
    source_name: str,
    text_path: Path,
    text: str,
    client: Any,
    model: str,
    profile: DocumentProfile | None = None,
) -> dict[str, Any]:
    document_profile = profile or get_profile()
    input_hash = _sha256(text)
    prompt = build_correction_prompt(source_name=source_name, text_path=text_path, text=text, profile=document_profile)
    result = client.complete_json(prompt)
    corrected = str(result.get("corrected_text") or "")
    status = str(result.get("status") or "error")
    empty_corrected_text_allowed = not corrected.strip() and _is_markup_residue_text(text)
    return {
        "source_name": source_name,
        "text_path": str(text_path),
        "input_sha256": input_hash,
        "prompt_sha256": _sha256(prompt),
        "profile_id": document_profile.id,
        "model": model,
        "status": status,
        "draft_ready": status in {"draft_ready", "needs_manual_review"} and (bool(corrected.strip()) or empty_corrected_text_allowed),
        "requires_review": status == "needs_manual_review",
        "empty_corrected_text_allowed": empty_corrected_text_allowed,
        "output_sha256": _sha256(corrected) if corrected else "",
        "llm_result": result,
    }


def acceptance_result_payload(
    *,
    source_name: str,
    text_path: Path,
    text: str,
    client: Any,
    model: str,
    profile: DocumentProfile | None = None,
) -> dict[str, Any]:
    document_profile = profile or get_profile()
    prompt = build_acceptance_prompt(source_name=source_name, text_path=text_path, text=text, profile=document_profile)
    result = client.complete_json(prompt)
    return {
        "source_name": source_name,
        "text_path": str(text_path),
        "content_sha256": _sha256(text),
        "prompt_sha256": _sha256(prompt),
        "profile_id": document_profile.id,
        "model": model,
        "status": result.get("status") or "error",
        "accepted": result.get("status") == "passed",
        "llm_result": result,
    }


def repair_result_payload(
    *,
    source_name: str,
    text_path: Path,
    text: str,
    acceptance_issue: dict[str, Any],
    previous_text: str,
    next_text: str,
    audit_text: str,
    client: Any,
    model: str,
    profile: DocumentProfile | None = None,
) -> dict[str, Any]:
    document_profile = profile or get_profile()
    prompt = build_repair_prompt(
        source_name=source_name,
        text_path=text_path,
        text=text,
        acceptance_issue=acceptance_issue,
        previous_text=previous_text,
        next_text=next_text,
        audit_text=audit_text,
        profile=document_profile,
    )
    result = client.complete_json(prompt)
    repaired = str(result.get("repaired_text") or "")
    status = str(result.get("status") or "error")
    return {
        "source_name": source_name,
        "text_path": str(text_path),
        "input_sha256": _sha256(text),
        "prompt_sha256": _sha256(prompt),
        "profile_id": document_profile.id,
        "model": model,
        "status": status,
        "repaired": status in {"repaired", "needs_manual_review"} and bool(repaired.strip()),
        "requires_review": status == "needs_manual_review",
        "output_sha256": _sha256(repaired) if repaired else "",
        "llm_result": result,
    }


def parse_json_object(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("LLM response is not a JSON object")
    return value


def _extract_message_text(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item["text"] for item in content if isinstance(item, dict) and isinstance(item.get("text"), str))
    return ""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_markup_residue_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    without_entities = re.sub(r"&(?:nbsp|#160|#xA0);", "", stripped, flags=re.IGNORECASE)
    without_tags = re.sub(r"</?[A-Za-z][A-Za-z0-9:-]*(?:\s+[^<>]*)?>", "", without_entities)
    leftovers = re.sub(r"[\s/<>|]+", "", without_tags)
    return leftovers == ""
