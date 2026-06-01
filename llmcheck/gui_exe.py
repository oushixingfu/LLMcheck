from __future__ import annotations

from argparse import ArgumentParser
from collections.abc import Sequence
import threading
import time
import webbrowser

import uvicorn

from llmcheck.gui import create_app


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(description="Start the LLMcheck GUI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically.")
    args = parser.parse_args(argv)

    url = _browser_url(host=args.host, port=args.port)
    if not args.no_browser:
        threading.Thread(target=_open_browser_later, args=(url,), daemon=True).start()

    print(f"LLMcheck GUI: {url}", flush=True)
    uvicorn.run(create_app(), host=args.host, port=max(1, args.port))
    return 0


def _browser_url(*, host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{browser_host}:{max(1, port)}"


def _open_browser_later(url: str) -> None:
    time.sleep(1.0)
    webbrowser.open(url)


if __name__ == "__main__":
    raise SystemExit(main())
