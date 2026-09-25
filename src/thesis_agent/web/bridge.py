"""UI-agnostic bridge between the permission hook and a web frontend.

The `PreToolUse` hook (permissions.py) is async and, for gated tool calls,
awaits an injected `approval_provider`. In the CLI that provider is a terminal
y/N prompt. Here it is `WebApprovalBroker.request_approval`, which:

  1. parks the request on an `asyncio.Queue` with a fresh `Future`, and
  2. awaits that Future.

A single consumer task in the Chainlit app (running in a known-good Chainlit
session context) drains the queue, renders one Allow/Deny prompt at a time, and
resolves the Future -- which unblocks the hook and lets the agent proceed.

This queue+consumer indirection is deliberate: the hook runs inside an
SDK-spawned asyncio task whose contextvars may not carry Chainlit's per-session
context, so the hook must not touch Chainlit directly. It only ever touches
asyncio primitives here. Serializing prompts through one consumer also means
concurrent approvals from parallel subagents queue up instead of colliding on
one input channel.

Nothing in this module imports Chainlit, so it is unit-testable on its own.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from thesis_agent.config import WorkspaceConfig
from thesis_agent.permissions import ApprovalRequest, AuditEvent


# --------------------------------------------------------------------------
# Approval brokering
# --------------------------------------------------------------------------
@dataclass
class PendingApproval:
    """One gated tool call waiting for a human decision."""

    request: ApprovalRequest
    future: "asyncio.Future[bool]"

    def resolve(self, decision: bool) -> None:
        """Deliver the decision to the waiting hook. Idempotent."""
        if not self.future.done():
            self.future.set_result(decision)


class WebApprovalBroker:
    """Bridges hook-side approval requests to a UI-side consumer."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[PendingApproval]" = asyncio.Queue()

    async def request_approval(self, request: ApprovalRequest) -> bool:
        """ApprovalProvider: park the request and await the human decision.

        The Future is never given a timeout here -- a local UI should wait for
        the human indefinitely. The consumer is responsible for any timeout
        policy (Chainlit's own action timeout), and fails closed by resolving
        the Future to False.
        """
        loop = asyncio.get_running_loop()
        pending = PendingApproval(request=request, future=loop.create_future())
        await self._queue.put(pending)
        return await pending.future

    async def next_pending(self) -> PendingApproval:
        """Consumer side: block until the next request needs a decision."""
        return await self._queue.get()


# --------------------------------------------------------------------------
# Audit event fan-out
# --------------------------------------------------------------------------
EventListener = Callable[[AuditEvent], Awaitable[None]]


@dataclass
class WebEventBus:
    """Collects audit events and forwards them to an optional live listener."""

    events: list[AuditEvent] = field(default_factory=list)
    _listener: EventListener | None = None

    def set_listener(self, listener: EventListener | None) -> None:
        self._listener = listener

    async def emit(self, event: AuditEvent) -> None:
        """EventSink: record the event and push it to the live UI listener."""
        self.events.append(event)
        if self._listener is not None:
            await self._listener(event)


# --------------------------------------------------------------------------
# Lockdown (emergency stop) toggle
# --------------------------------------------------------------------------
def lockdown_active(cfg: WorkspaceConfig) -> bool:
    return cfg.lockdown_file.exists()


def set_lockdown(cfg: WorkspaceConfig, active: bool) -> bool:
    """Create or remove the LOCKDOWN file. Returns the resulting state.

    `security.evaluate` re-reads this file on every tool call, so a toggle
    takes effect immediately for all subsequent calls -- no restart needed.
    """
    if active:
        cfg.lockdown_file.write_text(
            "Lockdown engaged from the web UI.\n", encoding="utf-8"
        )
    elif cfg.lockdown_file.exists():
        cfg.lockdown_file.unlink()
    return lockdown_active(cfg)


# --------------------------------------------------------------------------
# Remembered recent workspaces
# --------------------------------------------------------------------------
RECENTS_PATH = Path.home() / ".thesis-agent" / "recent_workspaces.json"
_MAX_RECENTS = 8


def load_recent_workspaces() -> list[str]:
    """Return remembered workspace paths, most-recent first. Never raises."""
    try:
        data = json.loads(RECENTS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(p) for p in data if isinstance(p, str)]
    except (OSError, ValueError):
        pass
    return []


def remember_workspace(path: str | Path) -> None:
    """Move `path` to the front of the recents list. Best-effort."""
    resolved = str(Path(path).expanduser().resolve())
    recents = [p for p in load_recent_workspaces() if p != resolved]
    recents.insert(0, resolved)
    recents = recents[:_MAX_RECENTS]
    try:
        RECENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        RECENTS_PATH.write_text(json.dumps(recents, indent=2), encoding="utf-8")
    except OSError:
        pass


def default_workspace() -> str | None:
    """The workspace to open without asking, or None to fall back to a prompt.

    `THESIS_AGENT_WORKSPACE` (set by `thesis-agent-web --workspace`, or
    exported in the shell) wins; otherwise the most recently used workspace.
    A path that no longer exists as a directory is skipped.
    """
    candidates = [os.environ.get("THESIS_AGENT_WORKSPACE")]
    candidates += load_recent_workspaces()[:1]
    for raw in candidates:
        if raw and Path(raw).expanduser().is_dir():
            return raw
    return None
