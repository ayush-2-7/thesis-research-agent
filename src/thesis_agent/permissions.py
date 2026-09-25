"""Tool permission gating and audit logging.

Every tool call made by the orchestrator or any subagent passes through a
`PreToolUse` hook (registered with `matcher=None`, which matches every tool --
confirmed against the installed SDK source, since `can_use_tool` alone is
*not* invoked for tools the CLI's own default rules already resolve, e.g.
plain `Read`). The hook does the following, in order, on every call:

1. Runs the hard security checks in `security.py` (sensitive-data directory,
   secrets, filesystem jail, dangerous shell commands, outbound PII/PHI
   heuristics, lockdown file). A hit here is denied immediately -- no human
   prompt -- and logged in full to `.thesis-agent/security.log`.
2. Appends a JSON-line audit record to `.thesis-agent/audit.log` either way.
3. Auto-allows read-only tools (Read/Grep/Glob/WebSearch/WebFetch/Agent, the
   arXiv search tool, and read-only SOGo mail/calendar calls) and asks for
   approval before anything that can modify files, run shell commands, save
   an email draft, or touch GitHub. (Sending email is never possible: see
   the never-send block in security.py.)

The *approval* mechanism and an optional *event sink* are injectable so the
same hook drives both the terminal REPL and the web UI:

- `approval_provider` -- an async callable that, given an `ApprovalRequest`,
  returns True (allow) or False (deny). Defaults to `terminal_approval_provider`
  (a y/N `input()` prompt), which preserves the CLI's behavior exactly.
- `event_sink` -- an optional async callable invoked with an `AuditEvent` for
  every decision (allow / deny / security-block), so a UI can render a live
  audit trail. The on-disk `audit.log` / `security.log` are written regardless.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import HookContext, HookMatcher

from thesis_agent import security
from thesis_agent.agents import SOGO_READ_ONLY_TOOLS
from thesis_agent.config import WorkspaceConfig

# Tools that only read state and never need a human in the loop. Security
# checks in `security.py` still run first regardless of this list -- e.g.
# Read is auto-allowed in general, but a Read of the sensitive data
# directory is denied before it ever reaches this set. SOGo's save_draft is
# deliberately NOT here -- see SOGO_GATED_TOOLS in agents.py -- so it stays
# behind the interactive approval prompt below. (There is no send tool.)
READ_ONLY_TOOLS = {
    "Read",
    "Grep",
    "Glob",
    "WebSearch",
    "WebFetch",
    "Agent",
    "mcp__arxiv__search_papers",
    *SOGO_READ_ONLY_TOOLS,
}


@dataclass(frozen=True)
class ApprovalRequest:
    """A gated tool call awaiting an allow/deny decision."""

    tool_name: str
    agent_id: str
    summary: str
    tool_use_id: str | None
    domain_warning: str | None = None


@dataclass(frozen=True)
class AuditEvent:
    """One decided tool call, emitted to the (optional) event sink."""

    timestamp: str
    agent_id: str
    tool_name: str
    summary: str
    decision: str  # "allow" | "deny"
    security_block: bool = False
    reason: str = ""


ApprovalProvider = Callable[[ApprovalRequest], Awaitable[bool]]
EventSink = Callable[[AuditEvent], Awaitable[None]]


def _summarize_input(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Produce a short human-readable summary of a tool call for prompts/logs."""
    if tool_name == "Bash":
        return str(tool_input.get("command", ""))[:200]
    if tool_name in ("Write", "Edit"):
        return str(tool_input.get("file_path", ""))
    if tool_name == "WebSearch":
        return str(tool_input.get("query", ""))
    return json.dumps(tool_input)[:200]


def _append_log(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


async def terminal_approval_provider(request: ApprovalRequest) -> bool:
    """Default approval provider: a blocking y/N prompt on the terminal.

    Fails closed (deny) when no interactive terminal is attached -- e.g. a
    piped/non-interactive process or a test harness -- rather than hanging or
    crashing the session.
    """
    warning = ""
    if request.domain_warning:
        warning = (
            f"  [warning] '{request.domain_warning}' is not on the "
            "known-safe domain list.\n"
        )
    prompt_text = (
        f"\n[approval needed] agent={request.agent_id} tool={request.tool_name}\n"
        f"{warning}"
        f"  {request.summary}\n"
        f"Allow? [y/N] "
    )
    try:
        answer = (await asyncio.to_thread(input, prompt_text)).strip().lower()
        return answer == "y"
    except EOFError:
        return False


def make_pre_tool_use_hook(
    cfg: WorkspaceConfig,
    approval_provider: ApprovalProvider | None = None,
    event_sink: EventSink | None = None,
):
    """Build a PreToolUse hook bound to a specific workspace.

    Register with `HookMatcher(matcher=None, hooks=[this])` so it fires for
    every tool call, built-in or MCP -- unlike `can_use_tool`, which the CLI
    only invokes for calls it doesn't already have a default answer for.

    `approval_provider` decides gated (non-read-only) calls; it defaults to the
    terminal y/N prompt. `event_sink`, if given, receives an `AuditEvent` for
    every decision so a UI can render a live audit trail.
    """

    approve = approval_provider or terminal_approval_provider

    async def _emit(event: AuditEvent) -> None:
        if event_sink is not None:
            try:
                await event_sink(event)
            except Exception:  # noqa: BLE001 - a UI sink must never break gating
                pass

    async def pre_tool_use(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict[str, Any]:
        tool_name = input_data["tool_name"]
        tool_input = input_data.get("tool_input", {})
        summary = _summarize_input(tool_name, tool_input)
        agent_id = input_data.get("agent_id") or "orchestrator"
        timestamp = datetime.now(timezone.utc).isoformat()

        verdict = security.evaluate(tool_name, tool_input, cfg)
        if verdict.blocked:
            # Full, untruncated tool_input goes to security.log (not the
            # normal audit log's truncated summary) so a real attempt can be
            # fully reconstructed later.
            _append_log(
                cfg.security_log_path,
                {
                    "timestamp": timestamp,
                    "agent": agent_id,
                    "tool": tool_name,
                    "tool_input": tool_input,
                    "reason": verdict.reason,
                },
            )
            _append_log(
                cfg.audit_log_path,
                {
                    "timestamp": timestamp,
                    "agent": agent_id,
                    "tool": tool_name,
                    "summary": summary,
                    "decision": "deny",
                    "security_block": True,
                },
            )
            print(
                f"\n[SECURITY BLOCK] agent={agent_id} tool={tool_name}\n"
                f"  {verdict.reason}\n",
                file=sys.stderr,
            )
            await _emit(
                AuditEvent(
                    timestamp=timestamp,
                    agent_id=agent_id,
                    tool_name=tool_name,
                    summary=summary,
                    decision="deny",
                    security_block=True,
                    reason=verdict.reason,
                )
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": verdict.reason,
                }
            }

        auto_allowed = tool_name in READ_ONLY_TOOLS
        if auto_allowed:
            decision = "allow"
        else:
            domain = security.webfetch_domain_warning(tool_name, tool_input)
            allowed = await approve(
                ApprovalRequest(
                    tool_name=tool_name,
                    agent_id=agent_id,
                    summary=summary,
                    tool_use_id=tool_use_id,
                    domain_warning=domain,
                )
            )
            decision = "allow" if allowed else "deny"

        _append_log(
            cfg.audit_log_path,
            {
                "timestamp": timestamp,
                "agent": agent_id,
                "tool": tool_name,
                "summary": summary,
                "decision": decision,
            },
        )
        await _emit(
            AuditEvent(
                timestamp=timestamp,
                agent_id=agent_id,
                tool_name=tool_name,
                summary=summary,
                decision=decision,
            )
        )

        hook_specific_output: dict[str, Any] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
        if decision == "deny":
            hook_specific_output["permissionDecisionReason"] = (
                f"User denied {tool_name} for agent '{agent_id}'."
            )

        return {"hookSpecificOutput": hook_specific_output}

    return pre_tool_use


def make_audit_hooks(
    cfg: WorkspaceConfig,
    approval_provider: ApprovalProvider | None = None,
    event_sink: EventSink | None = None,
) -> dict[str, list[HookMatcher]]:
    """Build the hooks dict to pass as `ClaudeAgentOptions.hooks`."""
    return {
        "PreToolUse": [
            HookMatcher(
                matcher=None,
                hooks=[make_pre_tool_use_hook(cfg, approval_provider, event_sink)],
            )
        ]
    }
