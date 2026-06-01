from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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

from llmcheck.quality import quality_hints


@dataclass(frozen=True)
class LlmConfig:
    api_url: str
    api_key: str
    model: str
    timeout_seconds: int = 600


class LlmClient:
    def __init__(self, config: LlmConfig) -> None:
        self.config = config

    def _max_attempts(self) -> int:
        raw_value = os.environ.get("LLMCHECK_LLM_RETRIES")
        if raw_value is None:
            return 3
        try:
            return max(1, int(raw_value))
        except ValueError:
            return 3

    def complete_json(self, prompt: str) -> dict[str, Any]:
        if os.environ.get("LLMCHECK_LLM_TRANSPORT") == "curl":
            return self._complete_json_with_curl(prompt)
        return self._complete_json_with_urllib(prompt)

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


def build_correction_prompt(*, source_name: str, text_path: Path, text: str) -> str:
    return (
        "你是中医 Markdown 文本的保守纠错员。任务：通读本次输入文本，修正 OCR/清洗残留造成的错别字、缺标点、异常分段、正文粘连和强制换行。\n"
        "\n"
        "硬性规则：\n"
        "1. corrected_text 必须是本次输入文本的完整纠正文，不是摘要或 diff；如果源文件名显示为第 N/M 片段，只返回该片段完整纠正文，不要因为片段边界判定为截断。\n"
        "2. 只能修正明显 OCR/清洗/排版问题，不得凭医学常识补写原书未出现内容。\n"
        "3. 不得现代化改写，不得删改医案、处方、剂量、诊断、页码线索中的实质信息。\n"
        "4. 标题、目录、条目、处方结构可以保留自然换行；普通正文自然段内不得保留 OCR 物理折行。\n"
        "5. 不确定内容保留原文，并写入 unresolved_issues。\n"
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


def build_acceptance_prompt(*, source_name: str, text_path: Path, text: str) -> str:
    return (
        "你是中医 Markdown 文本的最终验收员。请通读本次输入文本，判断该文本是否可以交付给后续知识抽取和人工阅读；如果源文件名显示为第 N/M 片段，只验收该片段，不要因为片段边界判定为截断。\n"
        "\n"
        "验收标准：\n"
        "1. 标点、断句、分段达到人类可读；不存在大段正文粘连。\n"
        "2. 普通正文自然段内不得保留 OCR 物理折行。\n"
        "3. 医案、处方、诊断、按语、治疗结果等结构应尽量清晰。\n"
        "4. 不要求现代化润色，只判断文本是否忠实、可读、可继续使用。\n"
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


def build_repair_prompt(
    *,
    source_name: str,
    text_path: Path,
    text: str,
    acceptance_issue: dict[str, Any],
    previous_text: str = "",
    next_text: str = "",
    audit_text: str = "",
) -> str:
    context_payload = {
        "acceptance_issue": acceptance_issue,
        "previous_text_excerpt": previous_text[-1200:],
        "next_text_excerpt": next_text[:1200],
        "audit_text_excerpt": audit_text[:4000],
    }
    return (
        "你是中医 Markdown 文本的验收返修员。任务：只针对本片段的阻断验收问题，输出本片段完整返修文本。\n"
        "\n"
        "硬性规则：\n"
        "1. repaired_text 必须是本片段完整文本，不是摘要或 diff。\n"
        "2. 优先依据本片段、相邻片段和 PPX 审计文本修正 OCR/排版/漏识问题。\n"
        "3. 不得改写医学实质；不得为了通顺大段新增原书未提供内容。\n"
        "4. 若验收意见指出固定枚举或固定配属缺项，且上下文已经给出完整序列，可按上下文补齐明显缺项，并在 changes 写明依据。\n"
        "5. 若无法可靠修复，保持原文并在 unresolved_issues 说明原因。\n"
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


def correction_result_payload(*, source_name: str, text_path: Path, text: str, client: Any, model: str) -> dict[str, Any]:
    input_hash = _sha256(text)
    prompt = build_correction_prompt(source_name=source_name, text_path=text_path, text=text)
    result = client.complete_json(prompt)
    corrected = str(result.get("corrected_text") or "")
    status = str(result.get("status") or "error")
    empty_corrected_text_allowed = not corrected.strip() and _is_markup_residue_text(text)
    return {
        "source_name": source_name,
        "text_path": str(text_path),
        "input_sha256": input_hash,
        "prompt_sha256": _sha256(prompt),
        "model": model,
        "status": status,
        "draft_ready": status in {"draft_ready", "needs_manual_review"} and (bool(corrected.strip()) or empty_corrected_text_allowed),
        "requires_review": status == "needs_manual_review",
        "empty_corrected_text_allowed": empty_corrected_text_allowed,
        "output_sha256": _sha256(corrected) if corrected else "",
        "llm_result": result,
    }


def acceptance_result_payload(*, source_name: str, text_path: Path, text: str, client: Any, model: str) -> dict[str, Any]:
    prompt = build_acceptance_prompt(source_name=source_name, text_path=text_path, text=text)
    result = client.complete_json(prompt)
    return {
        "source_name": source_name,
        "text_path": str(text_path),
        "content_sha256": _sha256(text),
        "prompt_sha256": _sha256(prompt),
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
) -> dict[str, Any]:
    prompt = build_repair_prompt(
        source_name=source_name,
        text_path=text_path,
        text=text,
        acceptance_issue=acceptance_issue,
        previous_text=previous_text,
        next_text=next_text,
        audit_text=audit_text,
    )
    result = client.complete_json(prompt)
    repaired = str(result.get("repaired_text") or "")
    status = str(result.get("status") or "error")
    return {
        "source_name": source_name,
        "text_path": str(text_path),
        "input_sha256": _sha256(text),
        "prompt_sha256": _sha256(prompt),
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
