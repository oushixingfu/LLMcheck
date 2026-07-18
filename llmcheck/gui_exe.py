from __future__ import annotations

from argparse import ArgumentParser
from collections.abc import Sequence

from llmcheck.desktop_gui import run_desktop_gui


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(description="Start the native LLMcheck desktop GUI.")
    parser.parse_args(argv)
    return run_desktop_gui()


if __name__ == "__main__":
    raise SystemExit(main())
