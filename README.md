# Thesis Research Agent

A multi-agent research and development environment for academic thesis work,
built on the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python).

This is **not** a chatbot you re-prompt for different tasks. It's an
orchestrator that routes your request to specialist subagents -- each with
its own narrow toolset -- and synthesizes their results:

- **thesis-agent** -- understands your local thesis workspace: what exists,
  what's done, what's missing.
- **research-agent** -- searches arXiv and the web for literature, compares
  papers, and identifies research gaps. No RAG pipeline, no vector database:
  it searches live and does the relevance judgment itself, the same way you'd
  read and compare papers yourself.

More specialist agents (coding, email, GitHub, planning) are planned but not
yet built -- see [Roadmap](#roadmap).

## Why this architecture

- **Runs locally.** Your thesis files, your machine. The only thing that
  leaves your machine is what you explicitly ask an agent to search for or
  send.
- **No RAG for literature.** Papers are found via live arXiv/web search, not
  a pre-indexed embeddings store -- the model reads and judges relevance
  itself, the same way you would.
- **Scoped tools per agent.** thesis-agent can't call the web; research-agent
  can't run shell commands. Each subagent only has the tools its job needs.
- **Every tool call is logged, and destructive ones need your approval.**
  See [Permissions and logging](#permissions-and-logging).

## Requirements

- Python 3.10+
- A Claude subscription (Pro/Max) or an Anthropic API key
- [Claude Code](https://code.claude.com) installed, for `claude login`

You do **not** need a separate Anthropic API key if you have a Claude
Pro/Max subscription -- the Agent SDK reuses your `claude login` session.

## Setup

```bash
# 1. Log in once (reuses your Claude subscription -- no API key needed)
npm install -g @anthropic-ai/claude-code
claude login

# 2. Install this tool
python3 -m venv .venv
source .venv/bin/activate
pip install -e .            # CLI only
pip install -e ".[web]"     # CLI + local web UI
```

## Usage

### Terminal (CLI)

```bash
thesis-agent --workspace /path/to/your/thesis/folder
```

If you omit `--workspace`, it uses the current directory. Example session:

```
> what should I work on next?
> find recent papers on target-trial emulation relevant to my thesis
> what's the current state of my thesis?
```

### Web UI

```bash
thesis-agent-web --workspace ~/path/to/thesis   # opens http://localhost:8000
thesis-agent-web                                # later: reuses the last workspace
```

A local [Chainlit](https://chainlit.io) app (nothing leaves your machine that
the CLI wouldn't also send). The workspace comes from `--workspace`, else the
`THESIS_AGENT_WORKSPACE` env var, else the last one used; the browser only
asks for a path if none of those exist (a 📁 **Change workspace** button
switches mid-session). The landing message offers focus buttons --
Planning, Research, Coding, Learning -- that pre-route every following
message to the right subagent(s), so the orchestrator skips ones that area
doesn't need; you can also just type without picking one. Streamed
answers show each tool call as a collapsible step; anything that writes files,
runs shell, or saves an email draft pauses for an **Allow / Deny** click instead of a
terminal `y/N`. A sidebar shows a live audit trail, the protected
sensitive-data directory, and a 🔴 **LOCKDOWN** kill switch that freezes every
tool call instantly.

The web UI reuses the exact same orchestrator, subagents, security checks, and
audit log as the CLI -- it only swaps the terminal approval prompt for browser
buttons. Port override: `THESIS_AGENT_WEB_PORT=8501 thesis-agent-web`.

> **First launch on macOS may hang for 1-3 minutes** the first time, while
> Gatekeeper does a one-time online notarization check on Chainlit's freshly
> installed native dependencies (worse behind some VPNs). It's a one-time cost
> per install -- subsequent launches are fast.

### Claude Desktop (MCP connector)

Instead of this project's own orchestrator, you can let **Claude Desktop** be
the orchestrator and call the workspace directly, inside any Claude *Project*.
`thesis-agent-mcp` is a local [MCP](https://modelcontextprotocol.io) server
(stdio) exposing these tools, each guarded by the **same `security.py` checks**
as the CLI/web UI:

| Tool | Purpose |
|---|---|
| `workspace_info` | workspace path, protected dir, LOCKDOWN status |
| `list_workspace` | recursive file listing (VCS/build noise + sensitive dir filtered out) |
| `read_workspace_file` | read a text file (jail + secret + sensitive-dir guarded) |
| `search_arxiv` | arXiv search (outbound PII-scanned) |
| `git_log` | recent commits |
| `get_progress` / `append_progress` | read/update the progress log |

Add it to `~/Library/Application Support/Claude/claude_desktop_config.json`
(merge into any existing `mcpServers`), then fully restart Claude Desktop:

```json
{
  "mcpServers": {
    "thesis-agent": {
      "command": "/ABSOLUTE/PATH/TO/.venv/bin/thesis-agent-mcp",
      "env": {
        "THESIS_AGENT_WORKSPACE": "/ABSOLUTE/PATH/TO/your/thesis/folder"
      }
    }
  }
}
```

The `command` points straight at the venv's entry-point script (no shell
activation needed). The connector's tools then appear in Claude Desktop and can
be used inside a Project.

> **This surface only works in Claude Desktop, not claude.ai web** -- the web
> app accepts only *remote* (hosted) connectors, and hosting a server that
> reads your local files would defeat the "data stays local" guarantee.

> ⚠️ **Workspace choice matters.** `read_workspace_file` / `list_workspace` can
> read anything in the workspace *except* the sensitive-data directory. Only
> `<workspace>/sensitive_data/` (or `THESIS_AGENT_SENSITIVE_DIR`) is hard
> blocked -- raw sensitive files sitting loose elsewhere in the workspace are
> readable. Point the connector at a folder whose sensitive data is inside the
> protected directory, or set `THESIS_AGENT_SENSITIVE_DIR` accordingly.

On first run, a `.thesis-agent/` directory is created inside your workspace
(git-ignored by default) holding:

- `progress.md` -- thesis-agent's running summary of workspace state
- `research_log.md` -- research-agent's running summary of literature found
- `audit.log` -- a line-per-tool-call log of everything every agent did

## Permissions and logging

Every tool call from every agent -- built-in or MCP -- passes through a
`PreToolUse` hook (`src/thesis_agent/permissions.py`) that:

1. Runs the hard security checks below (`src/thesis_agent/security.py`).
   A hit is denied immediately, with no prompt.
2. Logs the call (timestamp, agent, tool, summary, decision) to
   `.thesis-agent/audit.log`.
3. Auto-allows read-only operations (reading files, searching, running the
   arXiv tool) that pass the security checks.
4. Prompts you interactively in the terminal before anything that writes
   files, runs shell commands, or saves an email draft (sending is blocked) or
   touches GitHub.

If you deny a prompt, the agent is told why and can adjust its approach
rather than failing silently.

## Security model

The most important property of this tool: **sensitive source data (e.g.
patient-level data) never leaves this machine, structurally, regardless of
what any agent decides to do** -- including a subagent whose context gets
hijacked by a prompt injection hidden in a fetched web page or paper
abstract. That's a code-level guarantee in `src/thesis_agent/security.py`,
not just a system-prompt instruction (the prompts *also* tell each agent
about it, as a second layer, but the enforcement doesn't depend on the
model reading or obeying that text).

The `PreToolUse` hook runs these checks, in order, before any tool call is
allowed to execute. A hit is a silent, unprompted deny (not a y/N prompt --
alert fatigue means a human rubber-stamping the Nth approval of the session
is not a real control):

1. **Sensitive data directory is fully unreadable.** `sensitive_data_dir`
   (default: `<workspace>/sensitive_data`; override with
   `THESIS_AGENT_SENSITIVE_DIR` to point at an existing dataset elsewhere on
   disk) cannot be referenced by Read, Grep, Glob, Write, Edit, Bash, or any
   MCP tool, from any agent. No agent is currently granted an exception.
   For Bash, every argument (including `--flag=value` values and glob
   matches) is resolved like a file path, following symlinks -- so a
   workspace symlink pointing into the sensitive dir (e.g. a repo's
   `data/raw/…` kept for notebooks) is blocked too. Paths built at runtime
   inside a script aren't visible to this check; running such a script still
   goes through the approval prompt.
   Drop patient-level files there for a hard guarantee no subagent ever
   reads them into context.
2. **Credential/secret files are blocked everywhere**, even inside the
   workspace (`.env`, `*.pem`, `*.key`, `id_rsa*`, `credentials.json`, etc.).
3. **Filesystem jail.** File tools can only reference paths inside the
   workspace directory (or the sensitive dir, which is already blocked by
   #1) -- no reading `~/.ssh`, other drives, or arbitrary absolute paths.
4. **Dangerous shell commands are blocked**, not just gated behind a
   prompt: network clients (`curl`, `wget`, `scp`, `rsync`, `ssh`, `nc`,
   `ftp`), `git push`/`git remote add`, `base64`, `sudo`, `chmod 777`,
   `printenv`/`env`, `/dev/tcp` redirection, and similar. (No agent has
   `Bash` yet -- this is in place ahead of the planned coding-agent.)
5. **Outbound PII/PHI heuristic scan.** Any WebSearch query, WebFetch call,
   or MCP tool call (including arXiv search, since it's still a live
   network request) is scanned for identifier-shaped patterns -- SSNs, MRN
   numbers, DOB, patient-ID -- and blocked if one is found. This is
   defense-in-depth for the case where sensitive data was typed directly
   into the chat rather than read from the sensitive-data directory; tune
   the patterns in `security.py` for your actual identifier formats.
6. **Emergency stop.** Create `.thesis-agent/LOCKDOWN` (any content, even
   empty) to freeze every tool call immediately; delete it to resume.
7. **Email is never sent.** Any tool whose name looks like send/reply/
   forward/smtp/submit, and every `mcp__claude_ai_Gmail__*` tool, is denied
   outright; so are shell commands that could send mail (`smtplib`,
   `sendmail`, `msmtp`, ports 465/587, ...) or read the SOGo password from
   the Keychain. See [Email + calendar](#email--calendar-uni-koblenz-sogo-read--draft-never-send).

Additional layers, on top of the hard blocks above:

- **WebFetch domain awareness.** Fetches to a domain outside a small
  allowlist (arXiv, PubMed/NCBI, Google Scholar, Wikipedia, GitHub, DOI,
  Semantic Scholar, OpenReview) aren't blocked, but the approval prompt
  flags the domain so you see it before typing `y`.
- **Full audit trail.** `.thesis-agent/audit.log` gets a line per tool call
  (allowed or denied); `.thesis-agent/security.log` additionally captures
  the full, untruncated `tool_input` for every hard-blocked attempt, so a
  real incident can be fully reconstructed.
- **Scoped tools per agent** (see [Architecture](#architecture)):
  thesis-agent has no network tools at all; research-agent has no shell
  access. Least-privilege by construction, independent of the checks above.
- **Fail-closed on ambiguity.** If the approval prompt has no interactive
  terminal to ask (e.g. a non-interactive process), the tool call is denied
  rather than defaulting to allow.

None of this is a substitute for OS-level controls if your data is highly
sensitive (e.g. running this on a machine with no general internet route to
begin with, full-disk encryption, restricting the sensitive-data directory's
OS permissions beyond what `WorkspaceConfig` sets). Treat it as
application-layer defense-in-depth on top of, not instead of, that.

## Architecture

```
orchestrator (routes requests, has no direct tools besides Agent/Read)
├── thesis-agent    (Read, Grep, Glob, Write -> progress.md)
├── research-agent  (WebSearch, WebFetch, Read, Grep, Glob, Write -> research_log.md,
│                     + custom arXiv search tool, an in-process MCP server)
├── planning-agent  (Bash -> `git log` only, + SOGo calendar (read-only);
│                     synthesizes thesis-agent / research-agent / email-agent
│                     findings, pasted into its prompt by the orchestrator,
│                     into one recommendation)
├── coding-agent    (Read, Write, Edit, Grep, Glob, Bash -- code changes,
│                     running tests)
└── email-agent     (Read, Write -> email_log.md, + in-process SOGo tools
                      (tools/sogo.py): search/read mail + calendar
                      auto-allowed, save_draft gated behind approval,
                      sending impossible)
```

- `orchestrator.py` -- builds the top-level `ClaudeAgentOptions`: system
  prompt (routing logic), registered subagents, the arXiv MCP server, and
  the audit/permission hook.
- `agents.py` -- `AgentDefinition`s for every subagent. Prompts are
  domain-agnostic (they learn your thesis topic by reading your workspace),
  so this works for any research project, not just this one.
- `tools/arxiv.py` -- a custom tool calling the public arXiv API directly
  (`export.arxiv.org`), wrapped as an in-process MCP server via
  `create_sdk_mcp_server`. No API key required.
- `permissions.py` -- the audit-logging and approval-gating hook, explained
  above. The approval mechanism and an audit event sink are *injectable*: the
  CLI uses a terminal y/N prompt; the web UI injects browser buttons and a
  live-panel sink. `build_options(cfg, approval_provider, event_sink)` threads
  them through.
- `security.py` -- the hard, unprompted security blocks (sensitive-data
  directory, secrets, filesystem jail, dangerous shell commands, outbound
  PII/PHI heuristics, lockdown switch), explained in
  [Security model](#security-model).
- `cli.py` -- the interactive REPL entrypoint.
- `web/` -- the local Chainlit UI (`[web]` extra). `bridge.py` is UI-agnostic
  (approval queue/broker, event bus, lockdown toggle, recent-workspace memory);
  `app.py` is the Chainlit app; `launch.py` is the `thesis-agent-web` entry
  point. It reuses everything above unchanged.
- `mcp_server.py` -- a standalone local MCP server (`thesis-agent-mcp`) for
  Claude Desktop. Here *Claude* is the orchestrator and calls granular tools
  (`read_workspace_file`, `search_arxiv`, `git_log`, ...); each one routes
  through `security.evaluate`, so the sensitive-data / jail / secret / PII /
  LOCKDOWN guarantees hold identically. See
  [Claude Desktop (MCP connector)](#claude-desktop-mcp-connector).

### Email + calendar: Uni Koblenz SOGo (read + draft, never send)

`tools/sogo.py` is an in-process tool server (like the arXiv one) talking to
your university mailbox over IMAP (`imap.uni-koblenz.de:993`) and your SOGo
calendar over CalDAV (`https://sogo.uni-koblenz.de/SOGo/dav/`):

| Tool | Does | Gate |
|---|---|---|
| `list_folders`, `search_mail`, `read_mail` | find/read mail; folders opened read-only and bodies fetched with `BODY.PEEK`, so **nothing is marked as read** | auto |
| `upcoming_events` | calendar events for the next N days (also used by planning-agent) | auto |
| `save_draft` | IMAP-append a draft to your **Drafts** folder -- you send it from SOGo | approval |

**Sending is impossible, not just gated:** there is no SMTP code in the
project, `save_draft`'s target folder isn't a parameter, and `security.py`
hard-blocks any tool whose name looks like send/reply/forward/smtp, the old
Gmail connector, and mail-sending shell commands (`smtplib`, `sendmail`,
ports 465/587, ...). `strict_mcp_config=True` also stops the session from
inheriting any connector from your claude.ai account.

One-time setup (the password goes into the macOS Keychain -- never into a
file, an env var, or the agent's context; the shell is blocked from reading
that Keychain entry):

```bash
security add-generic-password -s thesis-agent-sogo -a <Rechnerkennung> -w
echo 'export THESIS_AGENT_SOGO_USER=<Rechnerkennung>' >> ~/.zshrc
```

Optional: `THESIS_AGENT_SOGO_FROM` sets the draft's From address;
`THESIS_AGENT_SOGO_IMAP_HOST` / `THESIS_AGENT_SOGO_DAV_URL` override the
servers. **Mail only works on campus or over the Uni Koblenz VPN
(WireGuard)**; the calendar works from anywhere. SOGo's MFA only guards the
web login, so IMAP/CalDAV use the plain password.

## Performance and models

Every subagent is a separate model+tool session, so the two biggest latency
levers are **which model each agent runs on** and **how many turns it may
take**. Both are set in `agents.py`:

| Agent | Model | Turn cap | Why |
|---|---|---|---|
| orchestrator | smart | — | must reliably invoke a subagent, wait, then synthesize |
| thesis-agent | fast | 8 | list/read files (mechanical) |
| email-agent | fast | 10 | SOGo search/read (mechanical) |
| coding-agent | fast | 12 | inspect/edit/run (bump to smart if correctness suffers) |
| research-agent | smart | 15 | reads/compares abstracts (real reasoning) |
| planning-agent | fast | 6 | prioritizes over findings the orchestrator pre-gathers (bump to smart if shallow) |

The speedup comes from the **leaf agents** running on the fast model; the
orchestrator stays on the smart model because the fast model mishandles the
invoke-subagent → wait → synthesize loop (it replies "I'll get back to you"
instead of waiting, yielding a non-answer). "fast"/"smart" default to `haiku`
and `sonnet`; override either globally without touching code:

```bash
THESIS_AGENT_FAST_MODEL=haiku THESIS_AGENT_SMART_MODEL=opus thesis-agent -w .
```

The orchestrator prompt also invokes the *fewest* subagents a request needs --
it won't call the slow research-agent or the email-agent for a plain
status/next-steps question.

## Roadmap

Not yet built:

- **github-agent** -- needs one manual step first: connect the official GitHub MCP server
  yourself, e.g. `claude mcp add --transport http github
  https://api.githubcopilot.com/mcp/` (or a personal-access-token variant),
  authorize it, then add a `github-agent` `AgentDefinition` in `agents.py`
  the same way the others are built, using the tool names that connector
  actually exposes.

## Known limitations (v1)

- thesis-agent reads text files directly; it doesn't yet parse `.docx`/
  `.pptx`/`.xlsx` content (python-docx/python-pptx would fix this).
- coding-agent's Bash access is gated by the interactive approval prompt and
  the dangerous-command patterns in `security.py`, not a full sandbox --
  same posture the project already takes for Bash everywhere else. Don't
  point it at a workspace you wouldn't otherwise trust with shell access.
- planning-agent relies on the orchestrator to paste in current
  thesis-agent/research-agent/email-agent findings; it doesn't invoke them
  itself, so a stale or incomplete orchestrator prompt for it produces a
  stale or incomplete recommendation.

## License

MIT -- see [LICENSE](LICENSE).
