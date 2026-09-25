"""Local web UI server: FastAPI + one WebSocket per browser tab.

Serves the hand-built frontend in `static/` (no build step, no CDN -- nothing
is fetched from the internet) and bridges it to the same orchestrator,
subagents, security checks and audit hook the CLI uses. Only the approval
provider and the audit sink differ: approvals are pushed to the browser as
cards and resolved by a click; audit events stream to the activity panel.

Wire protocol (JSON over /ws):

  browser -> server
    hello {}                      start a session on the default workspace
    set_workspace {path}          (re)start on another workspace
    prompt {text, focus}          run one turn (focus: FOCUS_AREAS key or null)
    approval {id, decision}       "allow" | "deny" for a pending approval
    interrupt {}                  stop the running turn
    lockdown {active}             engage / lift the LOCKDOWN kill switch
    reset {}                      fresh conversation, same workspace

  server -> browser
    session {...}                 workspace, protected dir, lockdown, focus areas
    need_workspace {recents, error}
    turn_start / turn_end {...}
    text {text, parent}           assistant text (parent = subagent tool id)
    tool_start {id, name, label, kind, detail, input, parent, subagent}
    tool_end {id, output, is_error}
    approval_request {id, ...} / approval_resolved {id, decision}
    audit {...}                   one decided tool call (live activity feed)
    lockdown {active}
    error {message}

Local-only by construction: the server binds 127.0.0.1 (see launch.py), HTTP
requests must carry a localhost Host header (blocks DNS rebinding), and the
WebSocket additionally requires a localhost Origin -- otherwise any web page
open in the same browser could connect to ws://localhost and click "Allow".
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from thesis_agent.config import WorkspaceConfig
from thesis_agent.orchestrator import FOCUS_AREAS, apply_focus, build_options
from thesis_agent.permissions import AuditEvent
from thesis_agent.web import bridge

STATIC_DIR = Path(__file__).with_name("static")
LOCAL_HOSTS = ("127.0.0.1", "localhost")
_MAX_OUTPUT_CHARS = 6000
_MAX_INPUT_CHARS = 3000


# --------------------------------------------------------------------------
# Tool presentation
# --------------------------------------------------------------------------
def tool_label(name: str) -> tuple[str, str]:
    """(human label, icon kind) for a tool name."""
    if name == "Agent":
        return "Delegate", "agent"
    if name == "mcp__arxiv__search_papers":
        return "arXiv search", "search"
    if name.startswith("mcp__sogo__"):
        short = name.removeprefix("mcp__sogo__")
        kind = "calendar" if short == "upcoming_events" else "mail"
        return short.replace("_", " ").capitalize(), kind
    kinds = {
        "Read": "read", "Grep": "search", "Glob": "search", "Write": "write",
        "Edit": "write", "Bash": "shell", "WebSearch": "web", "WebFetch": "web",
    }
    return name, kinds.get(name, "other")


def tool_detail(name: str, tool_input: dict[str, Any]) -> str:
    """One-line summary shown next to a step (path, query, command...)."""
    for key in ("description", "file_path", "pattern", "command", "query",
                "url", "subject", "sender", "folder"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:160]
    return ""


def _stringify(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text", b)) if isinstance(b, dict) else str(b) for b in content
        )
    return "" if content is None else str(content)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit} more characters)"


def _is_local_host(value: str | None) -> bool:
    if not value:
        return False
    host = urlparse(f"//{value}").hostname or ""
    return host in LOCAL_HOSTS


def websocket_is_trusted(headers: Any) -> bool:
    """Host AND Origin must both be localhost (see module docstring)."""
    origin = headers.get("origin")
    origin_host = urlparse(origin).hostname if origin else None
    return _is_local_host(headers.get("host")) and origin_host in LOCAL_HOSTS


# --------------------------------------------------------------------------
# One browser tab = one session
# --------------------------------------------------------------------------
class Session:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self._send_lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self.cfg: WorkspaceConfig | None = None
        self.client: ClaudeSDKClient | None = None
        self.broker: bridge.WebApprovalBroker | None = None
        self.pending: dict[str, bridge.PendingApproval] = {}
        self.consumer: asyncio.Task | None = None
        self.turn: asyncio.Task | None = None

    async def send(self, type_: str, **data: Any) -> None:
        # Approvals, audit events and the streaming turn all send from
        # different tasks; serialize so frames never interleave.
        async with self._send_lock:
            try:
                await self.ws.send_text(json.dumps({"type": type_, **data}, default=str))
            except Exception:  # noqa: BLE001 - socket gone; teardown handles it
                pass

    # -- lifecycle ---------------------------------------------------------
    async def start(self, cfg: WorkspaceConfig) -> None:
        await self.stop()
        self.cfg = cfg
        self.broker = bridge.WebApprovalBroker()
        bus = bridge.WebEventBus()
        bus.set_listener(self._on_audit)
        client = ClaudeSDKClient(
            options=build_options(
                cfg, approval_provider=self.broker.request_approval, event_sink=bus.emit
            )
        )
        await client.connect()
        self.client = client
        self.consumer = asyncio.create_task(self._consume_approvals())
        bridge.remember_workspace(cfg.workspace_dir)
        await self.send(
            "session",
            workspace=str(cfg.workspace_dir),
            workspace_name=cfg.workspace_dir.name,
            protected=str(cfg.sensitive_data_dir),
            locked=bridge.lockdown_active(cfg),
            recents=bridge.load_recent_workspaces(),
            focus_areas=[{"key": k, "label": v[0]} for k, v in FOCUS_AREAS.items()],
        )

    async def stop(self) -> None:
        if self.turn is not None and not self.turn.done():
            self.turn.cancel()
        # Fail closed: anything still waiting on a human is denied.
        for pending in self.pending.values():
            pending.resolve(False)
        self.pending.clear()
        if self.consumer is not None:
            self.consumer.cancel()
            self.consumer = None
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass

    # -- approvals & audit -------------------------------------------------
    async def _consume_approvals(self) -> None:
        assert self.broker is not None
        while True:
            pending = await self.broker.next_pending()
            approval_id = f"ap{next(self._ids)}"
            self.pending[approval_id] = pending
            req = pending.request
            label, kind = tool_label(req.tool_name)
            await self.send(
                "approval_request",
                id=approval_id,
                tool=req.tool_name,
                label=label,
                kind=kind,
                agent=req.agent_id,
                summary=req.summary,
                domain_warning=req.domain_warning,
                tool_use_id=req.tool_use_id,
            )

    async def resolve_approval(self, approval_id: str, decision: str) -> None:
        pending = self.pending.pop(approval_id, None)
        if pending is None:
            return
        allowed = decision == "allow"
        pending.resolve(allowed)
        await self.send("approval_resolved", id=approval_id,
                        decision="allow" if allowed else "deny")

    async def _on_audit(self, event: AuditEvent) -> None:
        label, kind = tool_label(event.tool_name)
        await self.send(
            "audit",
            time=event.timestamp,
            agent=event.agent_id,
            tool=event.tool_name,
            label=label,
            kind=kind,
            summary=event.summary,
            decision=event.decision,
            blocked=event.security_block,
            reason=event.reason,
        )

    # -- a turn ------------------------------------------------------------
    async def run_turn(self, text: str, focus: str | None) -> None:
        assert self.client is not None
        await self.send("turn_start")
        stats: dict[str, Any] = {}
        try:
            await self.client.query(apply_focus(text, focus))
            async for msg in self.client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            await self.send("text", text=block.text,
                                            parent=msg.parent_tool_use_id)
                        elif isinstance(block, ToolUseBlock):
                            await self._tool_start(block, msg.parent_tool_use_id)
                elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                    for block in msg.content:
                        if isinstance(block, ToolResultBlock):
                            await self.send(
                                "tool_end",
                                id=block.tool_use_id,
                                output=_clip(_stringify(block.content), _MAX_OUTPUT_CHARS),
                                is_error=bool(block.is_error),
                            )
                elif isinstance(msg, ResultMessage):
                    stats = {"duration_ms": msg.duration_ms,
                             "cost_usd": msg.total_cost_usd,
                             "is_error": msg.is_error}
        except asyncio.CancelledError:
            stats = {"cancelled": True}
            raise
        except Exception as e:  # noqa: BLE001 - surface, keep the session alive
            await self.send("error", message=f"{type(e).__name__}: {e}")
        finally:
            await self.send("turn_end", **stats)

    async def _tool_start(self, block: ToolUseBlock, parent: str | None) -> None:
        label, kind = tool_label(block.name)
        tool_input = block.input if isinstance(block.input, dict) else {}
        await self.send(
            "tool_start",
            id=block.id,
            name=block.name,
            label=label,
            kind=kind,
            detail=tool_detail(block.name, tool_input),
            input=_clip(json.dumps(tool_input, indent=2, default=str), _MAX_INPUT_CHARS),
            parent=parent,
            subagent=tool_input.get("subagent_type") if block.name == "Agent" else None,
        )

    # -- inbound messages --------------------------------------------------
    async def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind in ("hello", "set_workspace"):
            raw = msg.get("path") if kind == "set_workspace" else bridge.default_workspace()
            await self._open_workspace(raw)
        elif kind == "prompt":
            await self._start_turn(str(msg.get("text", "")), msg.get("focus"))
        elif kind == "approval":
            await self.resolve_approval(str(msg.get("id")), str(msg.get("decision")))
        elif kind == "interrupt":
            if self.client is not None and self.turn is not None and not self.turn.done():
                try:
                    await self.client.interrupt()
                except Exception:  # noqa: BLE001 - fall back to cancelling
                    self.turn.cancel()
        elif kind == "lockdown" and self.cfg is not None:
            active = bridge.set_lockdown(self.cfg, bool(msg.get("active")))
            await self.send("lockdown", active=active)
        elif kind == "reset" and self.cfg is not None:
            await self.start(self.cfg)

    async def _open_workspace(self, raw: str | None) -> None:
        if not raw:
            await self.send("need_workspace", recents=bridge.load_recent_workspaces())
            return
        try:
            cfg = WorkspaceConfig.resolve(raw)
        except ValueError as e:
            await self.send("need_workspace", recents=bridge.load_recent_workspaces(),
                            error=str(e))
            return
        try:
            await self.start(cfg)
        except Exception as e:  # noqa: BLE001 - e.g. `claude login` missing
            await self.send("error", message=f"Could not start the agent: {e}")

    async def _start_turn(self, text: str, focus: Any) -> None:
        if self.client is None:
            await self.send("error", message="No workspace is open yet.")
            return
        if self.turn is not None and not self.turn.done():
            await self.send("error", message="Still working on the previous message.")
            return
        if not text.strip():
            return
        focus = focus if focus in FOCUS_AREAS else None
        self.turn = asyncio.create_task(self.run_turn(text, focus))


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
def create_app() -> FastAPI:
    app = FastAPI(title="Thesis Agent", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(LOCAL_HOSTS))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html",
                            headers={"Cache-Control": "no-store"})

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        if not websocket_is_trusted(ws.headers):
            await ws.close(code=1008)
            return
        await ws.accept()
        session = Session(ws)
        try:
            while True:
                try:
                    msg = json.loads(await ws.receive_text())
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    await session.handle(msg)
        except WebSocketDisconnect:
            pass
        finally:
            await session.stop()

    return app


app = create_app()
