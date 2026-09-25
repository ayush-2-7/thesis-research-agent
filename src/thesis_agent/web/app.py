"""Chainlit frontend for the thesis research agent.

Run via `thesis-agent-web` (see launch.py) or directly:
    chainlit run src/thesis_agent/web/app.py

This module wires the existing orchestrator/permission stack to a browser UI:
- the workspace from `--workspace` / THESIS_AGENT_WORKSPACE / the last one
  used, falling back to an in-browser picker (with a "Change workspace" button),
- clickable focus areas (Planning / Research / Coding / Learning) that
  pre-route each message to the right subagent(s),
- streamed answers with each tool call shown as a collapsible Step,
- mid-stream Allow/Deny approval prompts (brokered through bridge.py so the
  permission hook never touches Chainlit directly),
- a transparency sidebar: workspace, protected sensitive-data dir, a live
  audit trail, and a LOCKDOWN kill switch.

It reuses `WorkspaceConfig.resolve`, `build_options`, and the security/audit
hook unchanged -- the web layer only supplies an approval provider and an
event sink.
"""

from __future__ import annotations

import asyncio
from typing import Any

import chainlit as cl
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from thesis_agent.config import WorkspaceConfig
from thesis_agent.orchestrator import FOCUS_AREAS, apply_focus, build_options
from thesis_agent.permissions import AuditEvent
from thesis_agent.web import bridge

_MAX_AUDIT_ROWS = 40


# --------------------------------------------------------------------------
# Display helpers
# --------------------------------------------------------------------------
def _friendly_tool(name: str) -> str:
    """Human-readable label for a tool name shown in Steps and prompts."""
    if name == "mcp__arxiv__search_papers":
        return "arXiv search"
    if name.startswith("mcp__sogo__"):
        return "SOGo: " + name.removeprefix("mcp__sogo__").replace("_", " ")
    if name == "Agent":
        return "Delegate to subagent"
    return name


def _stringify(content: Any) -> str:
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", block)))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def _audit_icon(event: AuditEvent) -> str:
    if event.security_block:
        return "⛔"
    return "✅" if event.decision == "allow" else "🚫"


def _panel_markdown() -> str:
    """Render the transparency sidebar body from current session state."""
    cfg: WorkspaceConfig = cl.user_session.get("cfg")
    bus: bridge.WebEventBus = cl.user_session.get("bus")
    locked = bridge.lockdown_active(cfg)
    focus = cl.user_session.get("focus")

    lines = [
        "### Session",
        f"**Workspace**\n`{cfg.workspace_dir}`",
        f"**Focus** {FOCUS_AREAS[focus][0] if focus in FOCUS_AREAS else 'none'}",
        f"**Protected (never read)**\n`{cfg.sensitive_data_dir}`",
        f"**Status** {'🔴 LOCKDOWN ACTIVE' if locked else '🟢 Active'}",
        "",
        "### Audit trail",
    ]
    events = bus.events[-_MAX_AUDIT_ROWS:] if bus else []
    if not events:
        lines.append("_No tool calls yet._")
    else:
        for e in reversed(events):
            row = f"{_audit_icon(e)} `{e.agent_id[:10]}` **{_friendly_tool(e.tool_name)}**"
            if e.security_block:
                row += " — BLOCKED"
            elif e.decision == "deny":
                row += " — denied"
            lines.append(row)
    return "\n\n".join(lines)


async def _refresh_panel() -> None:
    await cl.ElementSidebar.set_title("Thesis Agent")
    await cl.ElementSidebar.set_elements(
        [cl.Text(content=_panel_markdown(), name="panel")]
    )


# --------------------------------------------------------------------------
# Approval consumer (one prompt at a time -> no cross-subagent races)
# --------------------------------------------------------------------------
async def _approval_consumer(broker: bridge.WebApprovalBroker) -> None:
    while True:
        pending = await broker.next_pending()
        req = pending.request
        warn = (
            f"\n\n⚠️ Domain **not** on the known-safe list: `{req.domain_warning}`"
            if req.domain_warning
            else ""
        )
        try:
            res = await cl.AskActionMessage(
                content=(
                    f"**Approval needed** — `{req.agent_id}` wants to run "
                    f"**{_friendly_tool(req.tool_name)}**\n\n"
                    f"```\n{req.summary}\n```{warn}"
                ),
                actions=[
                    cl.Action(name="approval", payload={"decision": "allow"}, label="✅ Allow"),
                    cl.Action(name="approval", payload={"decision": "deny"}, label="🚫 Deny"),
                ],
                timeout=3600,
            ).send()
        except Exception:  # noqa: BLE001 - never let a UI error wedge the hook
            res = None
        decision = bool(res and res.get("payload", {}).get("decision") == "allow")
        pending.resolve(decision)  # fail-closed: None/error -> deny


# --------------------------------------------------------------------------
# Lockdown toggle
# --------------------------------------------------------------------------
@cl.action_callback("toggle_lockdown")
async def _on_toggle_lockdown(action: cl.Action) -> None:
    cfg: WorkspaceConfig = cl.user_session.get("cfg")
    now = bridge.set_lockdown(cfg, not bridge.lockdown_active(cfg))
    await _refresh_panel()
    await cl.Message(
        content=(
            "🔴 **LOCKDOWN engaged** — every tool call is now blocked until lifted."
            if now
            else "🟢 **Lockdown lifted** — tool calls resume (still subject to approval)."
        )
    ).send()


async def _send_control_bar() -> None:
    cfg: WorkspaceConfig = cl.user_session.get("cfg")
    locked = bridge.lockdown_active(cfg)
    await cl.Message(
        content="**Controls** — freeze/resume all tool calls, or switch workspace.",
        actions=[
            cl.Action(
                name="toggle_lockdown",
                payload={},
                label="🟢 Lift lockdown" if locked else "🔴 Engage lockdown",
            ),
            cl.Action(name="change_workspace", payload={}, label="📁 Change workspace"),
        ],
    ).send()


# --------------------------------------------------------------------------
# Focus area (preselected interest for the session)
# --------------------------------------------------------------------------
_FOCUS_ICONS = {"planning": "🗓", "research": "🔎", "coding": "💻", "learning": "📚"}
_FOCUS_FOLLOWUPS = {
    "planning": "What would you like to plan?",
    "research": "What should I look into?",
    "coding": "What should I build or fix?",
    "learning": "What would you like to understand?",
}


@cl.action_callback("set_focus")
async def _on_set_focus(action: cl.Action) -> None:
    focus = action.payload.get("focus")
    if focus not in FOCUS_AREAS:
        return
    cl.user_session.set("focus", focus)
    await _refresh_panel()
    await cl.Message(
        content=f"Focus set to **{FOCUS_AREAS[focus][0]}**. {_FOCUS_FOLLOWUPS[focus]}"
    ).send()


# --------------------------------------------------------------------------
# Workspace selection
# --------------------------------------------------------------------------
async def _ask_workspace() -> WorkspaceConfig:
    recents = bridge.load_recent_workspaces()
    hint = ""
    if recents:
        hint = "\n\nRecent:\n" + "\n".join(f"- `{p}`" for p in recents[:5])
    while True:
        res = await cl.AskUserMessage(
            content=(
                "Which **thesis workspace folder** should this session operate on?\n"
                "Paste an absolute path." + hint
            ),
            timeout=3600,
        ).send()
        raw = (res or {}).get("output", "").strip() if res else ""
        if not raw:
            await cl.Message(content="No path received — let's try again.").send()
            continue
        try:
            cfg = WorkspaceConfig.resolve(raw)
        except ValueError as e:
            await cl.Message(content=f"⚠️ {e}\n\nTry again.").send()
            continue
        bridge.remember_workspace(cfg.workspace_dir)
        return cfg


async def _initial_workspace() -> WorkspaceConfig:
    """Open the default workspace without asking; prompt only if there is none."""
    raw = bridge.default_workspace()
    if raw:
        try:
            cfg = WorkspaceConfig.resolve(raw)
            bridge.remember_workspace(cfg.workspace_dir)
            return cfg
        except ValueError:
            pass
    return await _ask_workspace()


@cl.action_callback("change_workspace")
async def _on_change_workspace(action: cl.Action) -> None:
    await _stop_session()
    cfg = await _ask_workspace()
    await _start_session(cfg)


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------
async def _start_session(cfg: WorkspaceConfig) -> None:
    broker = bridge.WebApprovalBroker()
    bus = bridge.WebEventBus()

    cl.user_session.set("cfg", cfg)
    cl.user_session.set("broker", broker)
    cl.user_session.set("bus", bus)

    # Live audit panel updates whenever the hook decides a call.
    async def _on_event(_event: AuditEvent) -> None:
        await _refresh_panel()

    bus.set_listener(_on_event)

    options = build_options(
        cfg,
        approval_provider=broker.request_approval,
        event_sink=bus.emit,
    )
    client = ClaudeSDKClient(options=options)
    await client.connect()
    cl.user_session.set("client", client)

    consumer = asyncio.create_task(_approval_consumer(broker))
    cl.user_session.set("consumer", consumer)

    await _refresh_panel()
    await _send_control_bar()
    locked = bridge.lockdown_active(cfg)
    await cl.Message(
        content=(
            f"Connected to `{cfg.workspace_dir}`.\n\n"
            + ("🔴 **LOCKDOWN is currently active** — lift it before running tools.\n\n"
               if locked else "")
            + "**Pick a focus for this session, or just type what you're looking "
            "for.** Anything that writes, runs shell, or sends email will pause "
            "for your approval."
        ),
        actions=[
            cl.Action(
                name="set_focus",
                payload={"focus": key},
                label=f"{_FOCUS_ICONS[key]} {label}",
            )
            for key, (label, _directive) in FOCUS_AREAS.items()
        ],
    ).send()


async def _stop_session() -> None:
    consumer: asyncio.Task | None = cl.user_session.get("consumer")
    if consumer is not None:
        consumer.cancel()
        cl.user_session.set("consumer", None)
    client: ClaudeSDKClient | None = cl.user_session.get("client")
    if client is not None:
        cl.user_session.set("client", None)
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass


@cl.on_chat_start
async def on_chat_start() -> None:
    await _start_session(await _initial_workspace())


@cl.on_message
async def on_message(message: cl.Message) -> None:
    client: ClaudeSDKClient = cl.user_session.get("client")
    if client is None:
        await cl.Message(content="Session not ready — reload the page.").send()
        return

    await client.query(apply_focus(message.content, cl.user_session.get("focus")))

    answer: cl.Message | None = None
    open_steps: dict[str, cl.Step] = {}

    async def _finalize_answer() -> None:
        nonlocal answer
        if answer is not None:
            await answer.update()
            answer = None

    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    if answer is None:
                        answer = cl.Message(content="")
                        await answer.send()
                    await answer.stream_token(block.text)
                elif isinstance(block, ToolUseBlock):
                    await _finalize_answer()
                    step = cl.Step(name=_friendly_tool(block.name), type="tool")
                    await step.__aenter__()
                    step.input = block.input
                    open_steps[block.id] = step
        elif isinstance(msg, UserMessage):
            content = msg.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        step = open_steps.pop(block.tool_use_id, None)
                        if step is not None:
                            step.output = _stringify(block.content)
                            if getattr(block, "is_error", False):
                                step.is_error = True
                            await step.__aexit__(None, None, None)

    # Close anything still open (e.g. a denied call), then flush text.
    for step in open_steps.values():
        await step.__aexit__(None, None, None)
    await _finalize_answer()


@cl.on_chat_end
async def on_chat_end() -> None:
    await _stop_session()
