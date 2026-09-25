"""Hard security stops for the agentic workflow.

These checks run inside the `PreToolUse` hook, *before* the normal
read-only-auto-allow / human-approval logic in `permissions.py`. A hard
block here is never shown as a y/N prompt: a human rubber-stamping the
Nth "Allow?" prompt of the session is not a real control, and a subagent
whose context has been hijacked by a prompt injection (e.g. instructions
hidden in a fetched web page or paper abstract) won't stop to ask either.
So anything in this file fails closed -- silently to the model (it gets a
denial reason and can adjust), loudly to the terminal and security.log.

Primary guarantee: `sensitive_data_dir` can never enter any agent's
context in the first place (checked first, before path-jailing or
anything else). That is a stronger property than trying to stop the data
from leaving once a tool call has already read it -- it holds even if
every other check in this file, or the human at the approval prompt, gets
it wrong.
"""

from __future__ import annotations

import fnmatch
import glob
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from thesis_agent.config import WorkspaceConfig

# Tool input keys that hold a filesystem path, across every built-in tool
# that touches the filesystem (Read/Write/Edit/Grep/Glob).
PATH_ARG_KEYS = ("file_path", "path", "notebook_path", "directory")

# Tools that can move data off this machine. Any `mcp__*` tool counts too
# (a custom MCP server is, by construction, making an external call) --
# including our own arXiv tool, which is why it's still routed through
# this check rather than special-cased away.
NETWORK_TOOLS = {"WebFetch", "WebSearch"}


def _is_network_tool(tool_name: str) -> bool:
    return tool_name in NETWORK_TOOLS or tool_name.startswith("mcp__")


# Credential/secret files that are off-limits even inside the workspace --
# these are not research data and an agent never has a legitimate reason
# to read or write them.
SECRET_FILE_GLOBS = (
    "*.env", ".env*", "*.pem", "*.key", "id_rsa*", "id_ed25519*",
    "credentials.json", "*.p12", "*.pfx", "known_hosts", ".netrc",
    "*token*.json", "*secret*",
)

# Shell fragments that either exfiltrate data off-box (network clients,
# remote copy, pushing to a remote git) or are broadly destructive/
# privilege-escalating. Matched as case-insensitive substrings against the
# full command line -- deliberately coarse, since a false positive here
# just means "ask a human" territory doesn't even get reached and the
# researcher has to phrase the request differently.
DANGEROUS_BASH_PATTERNS = (
    "curl", "wget", "scp ", "scp:", "rsync", "netcat", " nc ", "ssh ",
    "sftp", "ftp ", "git push", "git remote add", "base64",
    "rm -rf /", "sudo ", "chmod 777", "printenv", "env |",
    ">/dev/tcp", "osascript", "pbcopy",
    # Never send email (see NEVER_SEND_TOOL_RE below): no SMTP from the shell.
    "smtplib", "sendmail", "msmtp", "swaks", "mutt ", "mailx", "mail -s",
    "smtp.", ":465", ":587",
    # Never let the shell read the SOGo password out of the Keychain.
    "find-generic-password", "find-internet-password", "dump-keychain",
    "thesis-agent-sogo",
)

# Email may be read and drafted, NEVER sent. No send tool exists in this
# project (tools/sogo.py can only APPEND to Drafts), but this blocks any
# send-like tool name outright -- including ones that don't exist yet or
# arrive via an inherited connector -- plus the old Gmail connector entirely.
NEVER_SEND_TOOL_RE = re.compile(r"send|reply|forward|smtp|submit", re.IGNORECASE)
BLOCKED_TOOL_PREFIXES = ("mcp__claude_ai_gmail__",)

# Domains WebFetch can reach without extra scrutiny in the approval prompt.
# This is NOT a hard block -- an unlisted domain still goes through the
# normal y/N flow, just with a visible warning, since legitimate research
# sites outside this list absolutely exist.
WEBFETCH_DOMAIN_ALLOWLIST = (
    "arxiv.org", "export.arxiv.org", "ncbi.nlm.nih.gov",
    "pubmed.ncbi.nlm.nih.gov", "scholar.google.com", "en.wikipedia.org",
    "github.com", "raw.githubusercontent.com", "doi.org",
    "semanticscholar.org", "openreview.net", "arxiv.org",
)

# Heuristic identifier patterns that should never appear in an *outbound*
# tool call (WebFetch/WebSearch/any mcp__ tool). Defense-in-depth on top of
# the sensitive-directory block above -- e.g. if a researcher pastes a
# patient identifier directly into a chat message rather than a file, the
# directory check above never fires, but this still catches it before it
# goes out over the network. Not a substitute for the directory check:
# tune/extend this list for your actual identifier formats.
PII_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                          # SSN
    re.compile(r"\bMRN[:#\s]{0,3}\d{4,}\b", re.IGNORECASE),        # medical record #
    re.compile(r"\bDOB[:\s]{0,3}\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", re.IGNORECASE),
    re.compile(r"\bpatient[_\s]?id[:#\s]{0,3}\d{3,}\b", re.IGNORECASE),
]


@dataclass(frozen=True)
class SecurityVerdict:
    blocked: bool
    reason: str = ""


def _candidate_paths(tool_input: dict[str, Any]) -> list[Path]:
    paths = []
    for key in PATH_ARG_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            paths.append(Path(value))
    return paths


def _resolve(raw: Path, cfg: WorkspaceConfig) -> Path:
    try:
        p = raw if raw.is_absolute() else cfg.workspace_dir / raw
        return p.expanduser().resolve()
    except OSError:
        return raw


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_secret_file(path: Path) -> bool:
    return any(fnmatch.fnmatch(path.name, pat) for pat in SECRET_FILE_GLOBS)


def _bash_sensitive_path(command: str, cfg: WorkspaceConfig) -> Path | None:
    """Return the first shell argument that resolves into the sensitive dir.

    A literal-substring check on the command misses a symlink planted
    elsewhere in the workspace (e.g. a repo's `data/raw/x.xlsx` pointing into
    `sensitive_data/`), so each word -- and each `--flag=value` value, and each
    glob's matches -- is resolved like a file-tool path, following symlinks.
    Best-effort: paths built at runtime inside a script, or relative to a
    `cd` earlier in the same command, are not seen.
    """
    try:
        words = shlex.split(command)
    except ValueError:  # unbalanced quotes -- fall back to plain whitespace
        words = command.split()
    for word in words:
        for token in {word, word.partition("=")[2]}:
            if not token:
                continue
            candidates = [token]
            if glob.has_magic(token):
                try:
                    candidates += glob.glob(
                        str(Path(token).expanduser()), root_dir=cfg.workspace_dir
                    )
                except OSError:
                    pass
            for c in candidates:
                resolved = _resolve(Path(c), cfg)
                if _is_within(resolved, cfg.sensitive_data_dir):
                    return resolved
    return None


def _bash_command_hit(command: str) -> str | None:
    lowered = command.lower()
    for pattern in DANGEROUS_BASH_PATTERNS:
        if pattern in lowered:
            return pattern
    return None


def _outbound_text(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Everything about this call that could carry data off the machine."""
    parts = []
    for key in ("query", "url", "prompt", "content", "body", "text"):
        value = tool_input.get(key)
        if isinstance(value, str):
            parts.append(value)
    if tool_name.startswith("mcp__"):
        parts.append(str(tool_input))
    return "\n".join(parts)


def _pii_hit(text: str) -> str | None:
    for pattern in PII_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def evaluate(tool_name: str, tool_input: dict[str, Any], cfg: WorkspaceConfig) -> SecurityVerdict:
    """Run all hard checks for one tool call. First hit wins."""

    # 0. Emergency stop -- freeze everything if the lockdown file exists.
    if cfg.lockdown_file.exists():
        return SecurityVerdict(
            True,
            f"Lockdown active ({cfg.lockdown_file} exists). All tool calls "
            "are frozen until that file is removed.",
        )

    # 0b. Never send email; never touch the retired Gmail connector.
    if NEVER_SEND_TOOL_RE.search(tool_name):
        return SecurityVerdict(
            True,
            f"'{tool_name}' looks like a send/reply/forward action. Sending "
            "email is permanently disabled -- save a draft instead and the "
            "researcher sends it from SOGo.",
        )
    if tool_name.lower().startswith(BLOCKED_TOOL_PREFIXES):
        return SecurityVerdict(
            True, f"'{tool_name}' belongs to the Gmail connector, which is disabled."
        )

    # 1. Sensitive data directory: unreadable/unwritable/unreferenceable by
    #    any tool, full stop. Checked before the workspace jail below so
    #    this holds even if sensitive_data_dir lives outside the workspace.
    for raw in _candidate_paths(tool_input):
        resolved = _resolve(raw, cfg)
        if _is_within(resolved, cfg.sensitive_data_dir):
            return SecurityVerdict(
                True,
                f"'{resolved}' is inside the sensitive data directory "
                f"({cfg.sensitive_data_dir}). No agent tool may read, write, "
                "or reference it -- this is a hard boundary, not a judgment call.",
            )

    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        if str(cfg.sensitive_data_dir) in command:
            return SecurityVerdict(
                True, "Shell command references the sensitive data directory path."
            )
        hit_path = _bash_sensitive_path(command, cfg)
        if hit_path is not None:
            return SecurityVerdict(
                True,
                f"Shell command references '{hit_path}', which is inside the "
                f"sensitive data directory ({cfg.sensitive_data_dir}).",
            )

    # 2. Credential/secret files, anywhere they're referenced.
    for raw in _candidate_paths(tool_input):
        resolved = _resolve(raw, cfg)
        if _is_secret_file(resolved):
            return SecurityVerdict(
                True, f"'{resolved}' matches a credential/secret file pattern."
            )

    # 3. Filesystem jail: file tools may only touch the workspace (or the
    #    sensitive dir, which is already fully blocked above regardless).
    for raw in _candidate_paths(tool_input):
        resolved = _resolve(raw, cfg)
        if not _is_within(resolved, cfg.workspace_dir) and not _is_within(
            resolved, cfg.sensitive_data_dir
        ):
            return SecurityVerdict(
                True,
                f"'{resolved}' is outside the workspace ({cfg.workspace_dir}). "
                "File tools are jailed to the workspace directory.",
            )

    # 4. Bash: exfiltration / destructive / privilege-escalation patterns.
    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        hit = _bash_command_hit(command)
        if hit:
            return SecurityVerdict(
                True, f"Shell command matched a disallowed pattern ('{hit.strip()}')."
            )

    # 5. Network-capable tools: heuristic PII/PHI scan on outbound content.
    if _is_network_tool(tool_name):
        hit = _pii_hit(_outbound_text(tool_name, tool_input))
        if hit:
            return SecurityVerdict(
                True,
                f"Outbound call to '{tool_name}' contains what looks like an "
                f"identifier ('{hit}') -- refusing to send it off this machine.",
            )

    return SecurityVerdict(False)


def webfetch_domain_warning(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """Informational only: flag (not block) a WebFetch to an unlisted domain,
    so the human approving the prompt sees it before typing 'y'."""
    if tool_name != "WebFetch":
        return None
    domain = urlparse(str(tool_input.get("url", ""))).netloc.lower()
    if not domain:
        return None
    if any(domain == d or domain.endswith("." + d) for d in WEBFETCH_DOMAIN_ALLOWLIST):
        return None
    return domain
