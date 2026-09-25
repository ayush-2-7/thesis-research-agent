"""Builds the orchestrator's ClaudeAgentOptions.

The orchestrator itself does almost nothing directly -- its only tools are
`Agent` (to invoke subagents) and `Read` (for trivial lookups that don't
warrant spawning a subagent). All real work happens inside thesis-agent /
research-agent, each with a narrow, purpose-specific toolset. Routing is not
a separate component: it's just the orchestrator's system prompt deciding
which subagent(s) a request needs.
"""

from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions

from thesis_agent.agents import SMART_MODEL, SOGO_TOOLS, build_agents
from thesis_agent.config import WorkspaceConfig
from thesis_agent.permissions import ApprovalProvider, EventSink, make_audit_hooks
from thesis_agent.tools.arxiv import arxiv_server
from thesis_agent.tools.sogo import sogo_server

ORCHESTRATOR_SYSTEM_PROMPT = """\
You are the orchestrator for a researcher's multi-agent thesis workflow tool.
You do not do research, reading, writing, or coding yourself -- you decide
which specialist subagent(s) a request needs and invoke them via the Agent
tool, then synthesize their results into a direct answer for the researcher.

Available subagents and when to use them:
- thesis-agent: workspace state, progress, "what's done", "what's missing".
- research-agent: literature search, comparing papers, research gaps.
- planning-agent: "what should I work on next" style requests. Don't
  synthesize this yourself -- first invoke thesis-agent (plus research-agent
  or email-agent only if the request is explicitly about literature or
  email), then invoke planning-agent with the findings pasted into its
  prompt (it also checks git log itself). Relay planning-agent's recommendation as the answer rather than
  writing your own.
- coding-agent: any request needing actual code changes in the workspace
  (not just thesis text) -- reading/writing/editing code, running tests.
- email-agent: anything involving the researcher's Uni Koblenz email or
  calendar (SOGo) -- pulling advisor feedback/deadlines/decisions out of
  mail, or saving a draft (with the researcher's approval). It can NEVER
  send, reply, or forward: if asked to send, have it save a draft and tell
  the researcher to send it from SOGo. (planning-agent reads the calendar
  itself, so don't call email-agent just for deadlines when planning.)

Some requests need only one subagent. Others need several in sequence --
e.g. planning-agent needs thesis-agent/research-agent's output first. Decide
based on the actual request; don't invoke a subagent that isn't relevant.

Call only the subagents a request actually needs; an unneeeded one just adds
delay. A plain workspace/status question, or "what should I work on next",
needs thesis-agent (plus planning-agent for the recommendation) -- it does NOT
need research-agent or email-agent, even for "status and next steps" or
"what are the gaps in my thesis". thesis-agent already reports literature
recorded in earlier sessions; a live search is ~1-2 minutes of extra delay. Bring in research-agent only for a
literature question (finding/comparing papers, research gaps), and email-agent
only when the request is about email or advisor messages.

When you get a subagent's result back, don't just relay it verbatim (except
planning-agent's final recommendation, which you should relay directly) --
synthesize it into a direct, actionable answer. If you invoked more than one
subagent, connect their findings explicitly (e.g. "your workspace shows X is
unfinished, and recent papers on Y suggest an approach for it").

If a message starts with a [Session focus: ...] line, follow its routing
directive; it overrides the default routing above (including answering
Learning questions yourself instead of delegating).

GitHub actions (repos, commits, PRs) have no subagent yet -- say so plainly
rather than attempting them yourself if asked.

Security: the directory {sensitive_data_dir} is permanently off-limits to
every tool call, enforced at the tool layer, not by this instruction -- any
attempt to read, write, or reference it is denied before it reaches you or
any subagent. Never suggest routing sensitive/patient data through this
workflow's file or network tools.
"""


# Interest areas a UI can preselect for a session. Each maps to a routing
# directive prefixed onto the researcher's message, so the orchestrator goes
# straight to the right subagent(s) instead of deciding from scratch -- and
# skips the ones that area never needs. Per-message (not baked into the
# system prompt) so switching focus mid-session needs no reconnect.
FOCUS_AREAS: dict[str, tuple[str, str]] = {
    "planning": (
        "Planning",
        "Go straight to thesis-agent, then planning-agent with its findings. "
        "Skip research-agent and email-agent unless explicitly asked.",
    ),
    "research": (
        "Research",
        "Invoke research-agent directly (it can read the workspace itself). "
        "Skip thesis-agent, planning-agent and email-agent unless asked.",
    ),
    "coding": (
        "Coding",
        "Invoke coding-agent directly. No other subagents unless asked.",
    ),
    "learning": (
        "Learning",
        "Tutor mode: explain the concept yourself, tied to this thesis. Invoke "
        "thesis-agent only if you need the researcher's own files, and "
        "research-agent only if they ask for papers or sources.",
    ),
}


def apply_focus(prompt: str, focus: str | None) -> str:
    """Prefix `prompt` with the routing directive for `focus`, if any."""
    if focus not in FOCUS_AREAS:
        return prompt
    label, directive = FOCUS_AREAS[focus]
    return f"[Session focus: {label} -- {directive}]\n\n{prompt}"


def build_options(
    cfg: WorkspaceConfig,
    approval_provider: ApprovalProvider | None = None,
    event_sink: EventSink | None = None,
) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=ORCHESTRATOR_SYSTEM_PROMPT.format(
            sensitive_data_dir=cfg.sensitive_data_dir
        ),
        cwd=str(cfg.workspace_dir),
        agents=build_agents(cfg),
        # The orchestrator MUST stay on the smart model. It has to invoke a
        # subagent (the Agent tool), WAIT for its result, then synthesize --
        # a multi-step tool loop the fast model mishandles: it treats the
        # Agent call as fire-and-forget and replies "I'll get back to you once
        # it completes" instead of waiting, so the user gets a non-answer.
        # Speed still comes from the leaf agents running on FAST_MODEL; the
        # orchestrator does only routing + a short final synthesis.
        model=SMART_MODEL,
        # `tools` is the base toolset for the *whole session*, not just the
        # orchestrator -- a subagent's own AgentDefinition.tools can only
        # narrow this set, never grant a tool outside it. It must be the
        # union of every subagent's tools (confirmed by the CLI hard-denying
        # Glob/Grep for thesis-agent, bypassing the PreToolUse hook entirely,
        # when this list omitted them) plus whatever the orchestrator itself
        # calls directly (Agent, Read).
        tools=[
            "Agent",
            "Read",
            "Grep",
            "Glob",
            "Write",
            "Edit",
            "Bash",
            "WebSearch",
            "WebFetch",
            "mcp__arxiv__search_papers",
            *SOGO_TOOLS,
        ],
        # Registered here (not on the AgentDefinition) because
        # AgentDefinition.mcpServers takes a list of server *names* to
        # reference, not inline server objects -- scoping to research-agent
        # is done via its own `tools` list instead.
        mcp_servers={"arxiv": arxiv_server, "sogo": sogo_server},
        # Only the servers above: don't inherit connectors authorized on the
        # researcher's claude.ai account (Gmail, etc.). Email goes through
        # the in-process SOGo tools, which have no way to send.
        strict_mcp_config=True,
        # A PreToolUse hook, not can_use_tool: the CLI only invokes
        # can_use_tool for calls it doesn't already have a default answer
        # for (confirmed against the SDK source), so built-in read-only
        # tools like Read/Grep/Glob never reach it and would go unlogged.
        # The hook fires for every tool call unconditionally. The optional
        # approval_provider / event_sink let the web UI drive approvals and a
        # live audit trail; when omitted (the CLI path) the hook uses the
        # terminal y/N prompt and no event sink.
        hooks=make_audit_hooks(cfg, approval_provider, event_sink),
        permission_mode="default",
        # The CLI otherwise sometimes launches a subagent as a background
        # task ("Async agent launched"): the turn then ends with "I'll report
        # back" and the result never reaches the researcher. Measured: 2 of 3
        # status queries went async without this, 0 of 4 with it.
        env={"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"},
    )
