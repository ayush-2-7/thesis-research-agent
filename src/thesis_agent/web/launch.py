"""Console entry point: `thesis-agent-web`.

Starts the local web UI (server.py) on 127.0.0.1 and opens the browser.
`--workspace/-w PATH` pins the workspace for the session (passed on as
THESIS_AGENT_WORKSPACE); without it the app reuses the env var or the last
workspace used, and only asks in the browser if neither exists.
"""

from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path


def _workspace_arg(argv: list[str]) -> str | None:
    for flag in ("--workspace", "-w"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                return str(Path(argv[i + 1]).expanduser().resolve())
    return None


def main() -> None:
    import uvicorn

    workspace = _workspace_arg(sys.argv[1:])
    if workspace:
        os.environ["THESIS_AGENT_WORKSPACE"] = workspace
    port = int(os.environ.get("THESIS_AGENT_WEB_PORT", "8000"))
    url = f"http://127.0.0.1:{port}"

    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()
    print(f"Thesis Agent running at {url}  (Ctrl+C to stop)")
    # 127.0.0.1 only: the UI can approve tool calls, so it must never be
    # reachable from another machine. wsproto is a pure-Python WebSocket
    # backend, so no compiled extras are needed.
    uvicorn.run(
        "thesis_agent.web.server:app",
        host="127.0.0.1",
        port=port,
        ws="wsproto",
        log_level="warning",
    )


if __name__ == "__main__":
    main()
