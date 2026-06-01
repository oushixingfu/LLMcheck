from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
import filecmp
import json
import re
import shutil


BOOK_DIR_PATTERN = re.compile(r"^\d{4}_")
MD_DIR_NAME = "md"
TEXT_PDF_DIR_NAME = "文字版pdf"
PROCESS_DIR_NAME = "process"


@dataclass(frozen=True)
class Action:
    kind: Literal["copy", "move", "mkdir"]
    source: str
    target: str


def main() -> int:
    parser = ArgumentParser(description="Organize LLMcheck output into md, 文字版pdf, and process folders.")
    parser.add_argument("--root", default="/mnt/d/pdf/output", help="Output directory to organize.")
    parser.add_argument("--apply", action="store_true", help="Apply the plan. Defaults to dry-run.")
    args = parser.parse_args()

    root = Path(args.root).expanduser()
    try:
        actions = build_plan(root)
        validate_plan(root, actions)
        if args.apply:
            apply_plan(root, actions)
        print(json.dumps(summarize(root, actions, applied=args.apply), ensure_ascii=False, indent=2))
    except Exception as error:  # noqa: BLE001 - CLI should show a direct actionable error.
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False, indent=2))
        return 1
    return 0


def build_plan(root: Path) -> list[Action]:
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"output root not found: {root}")

    actions: list[Action] = [
        Action("mkdir", "", MD_DIR_NAME),
        Action("mkdir", "", TEXT_PDF_DIR_NAME),
        Action("mkdir", "", PROCESS_DIR_NAME),
        Action("mkdir", "", f"{PROCESS_DIR_NAME}/books"),
        Action("mkdir", "", f"{PROCESS_DIR_NAME}/logs"),
        Action("mkdir", "", f"{PROCESS_DIR_NAME}/misc"),
    ]

    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.name in {MD_DIR_NAME, TEXT_PDF_DIR_NAME, PROCESS_DIR_NAME}:
            continue
        if entry.is_dir() and BOOK_DIR_PATTERN.match(entry.name):
            actions.extend(delivery_copy_actions(root, entry))
            actions.append(Action("move", entry.name, f"{PROCESS_DIR_NAME}/books/{entry.name}"))
            continue
        if entry.is_file() and entry.name in {"llmcheck_batch_state.jsonl", "llmcheck_batch_summary.json"}:
            actions.append(Action("move", entry.name, f"{PROCESS_DIR_NAME}/{entry.name}"))
            continue
        if entry.is_file() and entry.suffix.lower() in {".log", ".exit"}:
            actions.append(Action("move", entry.name, f"{PROCESS_DIR_NAME}/logs/{entry.name}"))
            continue
        actions.append(Action("move", entry.name, f"{PROCESS_DIR_NAME}/misc/{entry.name}"))

    return actions


def delivery_copy_actions(root: Path, book_dir: Path) -> list[Action]:
    actions: list[Action] = []
    for source in sorted((book_dir / "final_markdown").glob("*.md")):
        target_name = delivery_name(root, MD_DIR_NAME, book_dir.name, source, ".md")
        actions.append(Action("copy", rel(root, source), f"{MD_DIR_NAME}/{target_name}"))
    for source in sorted((book_dir / "text_pdfs").glob("*.pdf")):
        target_name = delivery_name(root, TEXT_PDF_DIR_NAME, book_dir.name, source, ".pdf")
        actions.append(Action("copy", rel(root, source), f"{TEXT_PDF_DIR_NAME}/{target_name}"))
    return actions


def delivery_name(root: Path, delivery_dir: str, book_name: str, source: Path, suffix: str) -> str:
    siblings = sorted(source.parent.glob(f"*{suffix}"))
    if len(siblings) <= 1:
        return f"{book_name}{suffix}"
    return f"{book_name}__{source.stem}{suffix}"


def validate_plan(root: Path, actions: list[Action]) -> None:
    seen_targets: set[str] = set()
    for action in actions:
        if action.kind == "mkdir":
            continue
        if action.target in seen_targets:
            raise FileExistsError(f"duplicate planned target: {action.target}")
        seen_targets.add(action.target)
        source = root / action.source
        target = root / action.target
        if not source.exists():
            raise FileNotFoundError(f"planned source missing: {action.source}")
        if target.exists():
            if action.kind == "copy" and source.is_file() and target.is_file() and filecmp.cmp(source, target, shallow=False):
                continue
            raise FileExistsError(f"planned target already exists: {action.target}")


def apply_plan(root: Path, actions: list[Action]) -> None:
    for action in actions:
        if action.kind == "mkdir":
            (root / action.target).mkdir(parents=True, exist_ok=True)
    for action in actions:
        if action.kind == "copy":
            target = root / action.target
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / action.source, target)
    for action in actions:
        if action.kind == "move":
            target = root / action.target
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(root / action.source), str(target))


def summarize(root: Path, actions: list[Action], *, applied: bool) -> dict[str, object]:
    copies = [action for action in actions if action.kind == "copy"]
    moves = [action for action in actions if action.kind == "move"]
    return {
        "status": "applied" if applied else "dry_run",
        "root": str(root),
        "creates": [action.target for action in actions if action.kind == "mkdir"],
        "copy_final_markdown": sum(1 for action in copies if action.target.startswith(f"{MD_DIR_NAME}/")),
        "copy_text_pdf": sum(1 for action in copies if action.target.startswith(f"{TEXT_PDF_DIR_NAME}/")),
        "moves": {
            "total": len(moves),
            "books": sum(1 for action in moves if action.target.startswith(f"{PROCESS_DIR_NAME}/books/")),
            "logs": sum(1 for action in moves if action.target.startswith(f"{PROCESS_DIR_NAME}/logs/")),
            "batch_state": sum(1 for action in moves if action.target.startswith(f"{PROCESS_DIR_NAME}/llmcheck_batch_")),
            "misc": sum(1 for action in moves if action.target.startswith(f"{PROCESS_DIR_NAME}/misc/")),
        },
        "sample_actions": [action.__dict__ for action in actions if action.kind != "mkdir"][:12],
    }


def rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


if __name__ == "__main__":
    raise SystemExit(main())
