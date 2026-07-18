from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import os
import time


DEFAULT_STALE_SECONDS = 6 * 60 * 60


class RunAlreadyActiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunLock:
    path: Path
    run_id: str

    def heartbeat(self, *, status: str = "running", stage: str = "running") -> None:
        _write_json(
            self.path,
            {
                **_base_payload(self.run_id, status=status, stage=stage),
                "updated_at": _now(),
            },
        )

    def release(self, *, status: str) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if str(payload.get("run_id") or "") != self.run_id:
            return
        _write_json(
            self.path,
            {
                **_base_payload(self.run_id, status=status, stage="finished"),
                "updated_at": _now(),
                "finished_at": _now(),
            },
        )


def acquire_run_lock(process_dir: Path, *, run_id: str, stale_seconds: int = DEFAULT_STALE_SECONDS) -> RunLock:
    process_dir.mkdir(parents=True, exist_ok=True)
    lock_path = process_dir / "llmcheck_run.lock"
    payload = {**_base_payload(run_id, status="running", stage="starting"), "created_at": _now(), "updated_at": _now()}
    try:
        with lock_path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return RunLock(path=lock_path, run_id=run_id)
    except FileExistsError:
        existing = _read_json(lock_path)
        if _lock_is_stale(existing, stale_seconds=stale_seconds):
            _write_json(lock_path.with_suffix(".stale.json"), existing)
            _write_json(lock_path, payload)
            return RunLock(path=lock_path, run_id=run_id)
        raise RunAlreadyActiveError(_active_lock_message(lock_path, existing))


def _lock_is_stale(payload: dict[str, Any], *, stale_seconds: int) -> bool:
    status = str(payload.get("status") or "")
    if status and status != "running":
        return True
    pid = _int_or_zero(payload.get("pid"))
    if pid and not _pid_exists(pid):
        return True
    updated_at = _parse_timestamp(str(payload.get("updated_at") or payload.get("created_at") or ""))
    if updated_at is None:
        return False
    return (datetime.now(timezone.utc) - updated_at).total_seconds() > max(1, stale_seconds)


def _active_lock_message(path: Path, payload: dict[str, Any]) -> str:
    return (
        "LLMcheck run already active for this output directory: "
        f"lock={path}, pid={payload.get('pid')}, run_id={payload.get('run_id')}, "
        f"updated_at={payload.get('updated_at')}"
    )


def _base_payload(run_id: str, *, status: str, stage: str) -> dict[str, Any]:
    return {"run_id": run_id, "pid": os.getpid(), "status": status, "stage": stage}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return False
            ctypes.windll.kernel32.CloseHandle(process)
            return True
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _int_or_zero(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
