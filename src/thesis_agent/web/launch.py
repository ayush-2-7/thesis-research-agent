"""Console entry point: `thesis-agent-web`.

Chainlit is launched through its own CLI (`chainlit run <app>`), so this
shells out to it with our app module, on a fixed local port, and opens the
browser. `--workspace/-w PATH` pins the workspace for the session (passed on
as THESIS_AGENT_WORKSPACE); without it the app reuses the env var or the
last workspace used, and only asks in the browser if neither exists.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

APP_PATH = Path(__file__).with_name("app.py")


def _workspace_arg(argv: list[str]) -> str | None:
    for flag in ("--workspace", "-w"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                return str(Path(argv[i + 1]).expanduser().resolve())
    return None


def main() -> None:
    env = dict(os.environ)
    workspace = _workspace_arg(sys.argv[1:])
    if workspace:
        env["THESIS_AGENT_WORKSPACE"] = workspace
    port = os.environ.get("THESIS_AGENT_WEB_PORT", "8000")
    # Chainlit resolves `.chainlit/config.toml` and `chainlit.md` relative to
    # the *current working directory*, not the app file -- so run it from the
    # web package dir where those live, passing just the file name. The
    # server's cwd is otherwise irrelevant: the workspace is chosen in-UI and
    # the SDK subprocess is pinned to the resolved workspace dir (build_options
    # sets `cwd=`), independent of where the server runs.
    app_dir = APP_PATH.parent
    cmd = [
        sys.executable,
        "-m",
        "chainlit",
        "run",
        APP_PATH.name,
        "--port",
        port,
    ]
    # No `-h` (headless) flag: we want the browser to open. The user's env is
    # inherited, carrying the `claude login` session the SDK reuses.
    raise SystemExit(subprocess.call(cmd, cwd=str(app_dir), env=env))


if __name__ == "__main__":
    main()
