"""Standalone local MCP server exposing the thesis workspace to Claude Desktop.

Unlike the CLI / web UI -- where *this project's* orchestrator drives its own
subagents -- here **Claude (Desktop) is the orchestrator** and calls these
tools directly. So each tool is a plain, deterministic function that returns
data for Claude to reason over, not a model call.

Every filesystem/network tool is routed through the exact same hard security
checks as the rest of the project (`security.evaluate`): the sensitive-data
directory stays unreadable, the workspace jail holds, secret files are
blocked, dangerous shell is refused, outbound calls are PII-scanned, and the
LOCKDOWN file freezes everything. That guarantee does NOT depend on Claude
Desktop's own permission prompts -- it's enforced here, in code.

Claude Desktop spawns this over stdio. The workspace is chosen by the
connector config, via the THESIS_AGENT_WORKSPACE env var (preferred) or a
`--workspace <dir>` argument.

Run manually for testing:
    THESIS_AGENT_WORKSPACE=/path/to/thesis thesis-agent-mcp
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from thesis_agent import security
from thesis_agent.config import WorkspaceConfig
from thesis_agent.tools.arxiv import fetch_arxiv_text


def _resolve_workspace() -> WorkspaceConfig:
    """Pick the workspace from --workspace, THESIS_AGENT_WORKSPACE, or cwd."""
    workspace = os.environ.get("THESIS_AGENT_WORKSPACE")
    argv = sys.argv[1:]
    if "--workspace" in argv:
        i = argv.index("--workspace")
        if i + 1 < len(argv):
            workspace = argv[i + 1]
    return WorkspaceConfig.resolve(workspace or ".")


CFG = _resolve_workspace()
mcp = FastMCP("thesis-agent")

# Directory/file names that are noise in a workspace listing -- skipped so
# Claude sees the researcher's actual content, not VCS/build plumbing.
_IGNORED_NAMES = {".git", "__pycache__", ".DS_Store", ".venv", ".thesis-agent"}


class SecurityBlocked(Exception):
    """Raised (and surfaced to Claude as a tool error) on a hard-blocked call."""


def _guard(tool_name: str, tool_input: dict[str, Any]) -> None:
    """Run the project's hard security checks; raise on a block.

    Uses the same `security.evaluate` as the CLI/web hook, so the
    sensitive-data directory, workspace jail, secret-file, dangerous-shell,
    outbound-PII, and LOCKDOWN rules all apply here identically.
    """
    verdict = security.evaluate(tool_name, tool_input, CFG)
    if verdict.blocked:
        raise SecurityBlocked(verdict.reason)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
@mcp.tool()
def workspace_info() -> str:
    """Report the active thesis workspace, its protected sensitive-data
    directory, and whether the LOCKDOWN kill switch is engaged."""
    locked = CFG.lockdown_file.exists()
    return (
        f"Workspace: {CFG.workspace_dir}\n"
        f"Protected (never readable): {CFG.sensitive_data_dir}\n"
        f"Progress log: {CFG.progress_file}\n"
        f"Status: {'LOCKDOWN ACTIVE (all tools frozen)' if locked else 'active'}"
    )


@mcp.tool()
def list_workspace(subpath: str = ".") -> str:
    """List files and folders under the thesis workspace (recursively).

    `subpath` is relative to the workspace root. The sensitive-data directory
    is never listed.
    """
    base = (CFG.workspace_dir / subpath).resolve()
    _guard("Glob", {"path": str(base)})
    if not base.exists():
        return f"No such path in workspace: {subpath}"

    rows: list[str] = []
    for p in sorted(base.rglob("*")):
        # Skip VCS/build noise and the protected dir at any depth.
        if any(part in _IGNORED_NAMES for part in p.relative_to(CFG.workspace_dir).parts):
            continue
        try:
            p.relative_to(CFG.sensitive_data_dir)
            continue
        except ValueError:
            pass
        rel = p.relative_to(CFG.workspace_dir)
        rows.append(f"{'d' if p.is_dir() else '-'} {rel}")
    return "\n".join(rows) if rows else "(empty)"


@mcp.tool()
def read_workspace_file(file_path: str) -> str:
    """Read a UTF-8 text file from the thesis workspace.

    `file_path` may be absolute or relative to the workspace. Reads of the
    sensitive-data directory, secret files, or anything outside the workspace
    are refused.
    """
    _guard("Read", {"file_path": file_path})
    target = Path(file_path)
    if not target.is_absolute():
        target = CFG.workspace_dir / target
    target = target.resolve()
    if not target.is_file():
        return f"No such file: {file_path}"
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"{file_path} is not a UTF-8 text file (binary content not shown)."


@mcp.tool()
def search_arxiv(query: str, max_results: int = 10) -> str:
    """Search arXiv for papers matching a query (titles, authors, dates,
    abstracts, links), ranked by relevance. Reads abstracts for you to judge;
    it does not do the relevance judgment itself."""
    _guard("mcp__arxiv__search_papers", {"query": query})
    return fetch_arxiv_text(query, max_results)


@mcp.tool()
def git_log(max_count: int = 20) -> str:
    """Show recent git commits in the workspace (one line each). Returns a
    clear message if the workspace is not a git repository."""
    if CFG.lockdown_file.exists():
        raise SecurityBlocked("Lockdown active: all tools are frozen.")
    n = min(max(int(max_count or 20), 1), 200)
    try:
        out = subprocess.run(
            ["git", "-C", str(CFG.workspace_dir), "log", f"-{n}", "--oneline"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return f"Could not run git: {e}"
    if out.returncode != 0:
        return f"No git history available: {out.stderr.strip() or 'not a git repository'}"
    return out.stdout.strip() or "(no commits yet)"


@mcp.tool()
def get_progress() -> str:
    """Return the running progress log the agents maintain for this workspace,
    or a note that none exists yet."""
    _guard("Read", {"file_path": str(CFG.progress_file)})
    if not CFG.progress_file.exists():
        return "No progress log yet."
    return CFG.progress_file.read_text(encoding="utf-8")


@mcp.tool()
def append_progress(entry: str) -> str:
    """Append a short dated entry to the workspace's progress log. Use this to
    record a durable milestone (a section drafted, an experiment run)."""
    from datetime import date

    _guard("Write", {"file_path": str(CFG.progress_file)})
    line = f"\n## {date.today().isoformat()}\n{entry.strip()}\n"
    with CFG.progress_file.open("a", encoding="utf-8") as f:
        f.write(line)
    return f"Appended to {CFG.progress_file.name}."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
