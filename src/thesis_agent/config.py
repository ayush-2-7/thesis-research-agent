"""Workspace and state-directory resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SENSITIVE_README = """\
Files placed in this directory are permanently off-limits to every agent
tool call -- Read, Grep, Glob, Write, Edit, Bash, WebFetch, WebSearch, and
any MCP tool (including the arXiv search tool). This is enforced in code
by src/thesis_agent/security.py, not by prompt instruction alone, and the
block cannot be approved away at the interactive y/N prompt.

Put patient-level or other sensitive source data here if you want a hard
guarantee that no subagent -- including one whose context gets hijacked by
a prompt injection in a fetched web page -- can ever read it into context
or send it off this machine.

Override this location (e.g. to point at an existing dataset elsewhere on
disk) with the THESIS_AGENT_SENSITIVE_DIR environment variable.
"""


@dataclass(frozen=True)
class WorkspaceConfig:
    """Paths for the thesis workspace this agent session operates on."""

    workspace_dir: Path
    state_dir: Path
    audit_log_path: Path
    security_log_path: Path
    progress_file: Path
    research_log_file: Path
    email_log_file: Path
    sensitive_data_dir: Path
    lockdown_file: Path

    @classmethod
    def resolve(cls, workspace: str | Path) -> "WorkspaceConfig":
        workspace_dir = Path(workspace).expanduser().resolve()
        if not workspace_dir.is_dir():
            raise ValueError(f"Workspace directory does not exist: {workspace_dir}")

        state_dir = workspace_dir / ".thesis-agent"
        state_dir.mkdir(exist_ok=True)

        override = os.environ.get("THESIS_AGENT_SENSITIVE_DIR")
        sensitive_data_dir = (
            Path(override).expanduser().resolve()
            if override
            else workspace_dir / "sensitive_data"
        )
        sensitive_data_dir.mkdir(parents=True, exist_ok=True)
        try:
            # Best-effort: owner-only permissions. Not portable to all
            # filesystems (e.g. some network mounts), so failures here are
            # not fatal -- the tool-level block in security.py is the real
            # control regardless of OS file permissions.
            sensitive_data_dir.chmod(0o700)
        except OSError:
            pass

        readme = sensitive_data_dir / "README.txt"
        if not readme.exists():
            readme.write_text(SENSITIVE_README, encoding="utf-8")

        return cls(
            workspace_dir=workspace_dir,
            state_dir=state_dir,
            audit_log_path=state_dir / "audit.log",
            security_log_path=state_dir / "security.log",
            progress_file=state_dir / "progress.md",
            research_log_file=state_dir / "research_log.md",
            email_log_file=state_dir / "email_log.md",
            sensitive_data_dir=sensitive_data_dir,
            lockdown_file=state_dir / "LOCKDOWN",
        )
