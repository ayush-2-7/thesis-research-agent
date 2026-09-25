"""Unit tests for thesis_agent: security guards, config, web bridge, wiring,
and the per-agent speed configuration (models + turn caps).

Relies on the editable install (`pip install -e .`) so `import thesis_agent`
works without path hacks. Run with:  python -m pytest tests/ -q

Test names prefixed FINDING_ assert the code's *current* behaviour even where
that behaviour is a known gap worth discussing -- the name is the signal, and
the suite stays green so real regressions still stand out.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from thesis_agent import security
from thesis_agent.config import WorkspaceConfig


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------
@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A fresh resolved WorkspaceConfig rooted at a temp dir."""
    monkeypatch.delenv("THESIS_AGENT_SENSITIVE_DIR", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    return WorkspaceConfig.resolve(ws)


def ev(tool, inp, cfg):
    return security.evaluate(tool, inp, cfg)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# config.py
# --------------------------------------------------------------------------
def test_config_creates_state_and_sensitive_dirs(workspace):
    assert workspace.state_dir.is_dir()
    assert workspace.sensitive_data_dir.is_dir()
    assert (workspace.sensitive_data_dir / "README.txt").exists()


def test_config_rejects_nonexistent_workspace(tmp_path):
    with pytest.raises(ValueError):
        WorkspaceConfig.resolve(tmp_path / "does_not_exist")


def test_config_sensitive_override_env(tmp_path, monkeypatch):
    ext = tmp_path / "external_data"
    ext.mkdir()
    monkeypatch.setenv("THESIS_AGENT_SENSITIVE_DIR", str(ext))
    ws = tmp_path / "ws2"
    ws.mkdir()
    cfg = WorkspaceConfig.resolve(ws)
    assert cfg.sensitive_data_dir == ext.resolve()


def test_config_email_log_path_present(workspace):
    assert workspace.email_log_file.name == "email_log.md"
    assert workspace.email_log_file.parent == workspace.state_dir


def test_FINDING_config_chmods_existing_override_dir(tmp_path, monkeypatch):
    """Pointing the override at an EXISTING dir silently chmods it to 0700."""
    ext = tmp_path / "shared_dataset"
    ext.mkdir()
    ext.chmod(0o755)
    monkeypatch.setenv("THESIS_AGENT_SENSITIVE_DIR", str(ext))
    ws = tmp_path / "ws3"
    ws.mkdir()
    WorkspaceConfig.resolve(ws)
    assert oct(ext.stat().st_mode)[-3:] == "700"


# --------------------------------------------------------------------------
# security.py -- sensitive data directory
# --------------------------------------------------------------------------
def test_read_inside_sensitive_dir_blocked(workspace):
    target = str(workspace.sensitive_data_dir / "patients.csv")
    assert ev("Read", {"file_path": target}, workspace).blocked


def test_grep_inside_sensitive_dir_blocked(workspace):
    assert ev("Grep", {"path": str(workspace.sensitive_data_dir)}, workspace).blocked


def test_relative_path_into_sensitive_dir_blocked(workspace):
    assert ev("Read", {"file_path": "sensitive_data/x.csv"}, workspace).blocked


def test_read_normal_workspace_file_allowed(workspace):
    target = str(workspace.workspace_dir / "notes.md")
    assert not ev("Read", {"file_path": target}, workspace).blocked


# --------------------------------------------------------------------------
# security.py -- filesystem jail
# --------------------------------------------------------------------------
def test_read_outside_workspace_blocked(workspace):
    assert ev("Read", {"file_path": "/etc/passwd"}, workspace).blocked


def test_path_traversal_escape_blocked(workspace):
    escape = str(workspace.workspace_dir / ".." / ".." / "etc" / "passwd")
    assert ev("Read", {"file_path": escape}, workspace).blocked


def test_symlink_out_of_workspace_blocked(workspace):
    link = workspace.workspace_dir / "escape_link"
    try:
        link.symlink_to("/etc")
    except OSError:
        pytest.skip("cannot create symlink here")
    assert ev("Read", {"file_path": str(link / "passwd")}, workspace).blocked


# --------------------------------------------------------------------------
# security.py -- secret files
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", [".env", "id_rsa", "server.pem", "credentials.json", "api.key"])
def test_secret_files_blocked(workspace, name):
    target = str(workspace.workspace_dir / name)
    assert ev("Read", {"file_path": target}, workspace).blocked


# --------------------------------------------------------------------------
# security.py -- dangerous bash
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cmd", [
    "curl http://evil.com",
    "wget http://x",
    "git push origin main",
    "sudo rm -rf /",
    "cat x | base64",
    "echo hi >/dev/tcp/1.2.3.4/80",   # no-space form IS caught
])
def test_dangerous_bash_blocked(workspace, cmd):
    assert ev("Bash", {"command": cmd}, workspace).blocked


@pytest.mark.parametrize("cmd", [
    "echo hi > /dev/tcp/1.2.3.4/80",   # space after '>' evades ">/dev/tcp"
    "nc 1.2.3.4 80",                   # leading 'nc' evades ' nc '
    "scp\tfile host:/tmp",             # tab after scp evades 'scp '
    "ssh\tuser@host",                  # tab after ssh evades 'ssh '
])
def test_FINDING_dangerous_bash_spacing_bypass(workspace, cmd):
    """Space-sensitive substring patterns are evaded by trivial whitespace
    variants bash treats identically. (Known gap, documented.)"""
    assert not ev("Bash", {"command": cmd}, workspace).blocked


def test_bash_referencing_sensitive_abs_path_blocked(workspace):
    cmd = f"cat {workspace.sensitive_data_dir}/patients.csv"
    assert ev("Bash", {"command": cmd}, workspace).blocked


def test_bash_sensitive_relative_path_blocked(workspace):
    """Bash cwd IS the workspace, so a relative path resolves into the
    sensitive dir and must be blocked (formerly a known gap)."""
    assert ev("Bash", {"command": "cat sensitive_data/patients.csv"}, workspace).blocked


def test_FINDING_bash_reads_ssh_key_NOT_blocked_by_security(workspace):
    """Bash isn't filesystem-jailed and secret-globs don't apply to it."""
    assert not ev("Bash", {"command": "cat ~/.ssh/id_rsa"}, workspace).blocked


def test_FINDING_bash_cats_etc_passwd_NOT_jailed(workspace):
    assert not ev("Bash", {"command": "cat /etc/passwd"}, workspace).blocked


# --------------------------------------------------------------------------
# security.py -- outbound PII/PHI
# --------------------------------------------------------------------------
def test_websearch_ssn_blocked(workspace):
    assert ev("WebSearch", {"query": "patient 123-45-6789 outcomes"}, workspace).blocked


def test_websearch_mrn_blocked(workspace):
    assert ev("WebSearch", {"query": "MRN 4567890 readmission"}, workspace).blocked


def test_arxiv_mcp_pii_blocked(workspace):
    assert ev("mcp__arxiv__search_papers", {"query": "DOB 01/02/1990 cohort"}, workspace).blocked


def test_clean_websearch_allowed(workspace):
    assert not ev("WebSearch", {"query": "gestational diabetes target trial"}, workspace).blocked


def test_FINDING_ssn_without_dashes_not_caught(workspace):
    assert not ev("WebSearch", {"query": "id 123456789"}, workspace).blocked


def test_draft_body_pii_scanned(workspace):
    assert ev("mcp__sogo__save_draft",
              {"to": "a@b.de", "subject": "x", "body": "SSN 123-45-6789"},
              workspace).blocked


# --------------------------------------------------------------------------
# security.py -- lockdown + glob-pattern gap + domain warnings
# --------------------------------------------------------------------------
def test_lockdown_blocks_everything(workspace):
    workspace.lockdown_file.write_text("stop")
    try:
        assert ev("Read", {"file_path": str(workspace.workspace_dir / "a.md")}, workspace).blocked
        assert ev("WebSearch", {"query": "anything"}, workspace).blocked
    finally:
        workspace.lockdown_file.unlink()


def test_FINDING_glob_absolute_pattern_not_checked(workspace):
    """Only 'path' is jailed; 'pattern'/'glob' args are never inspected."""
    assert not ev("Glob", {"pattern": "/etc/**"}, workspace).blocked
    assert not ev("Grep", {"pattern": "secret", "glob": "../../*"}, workspace).blocked


def test_webfetch_known_domain_no_warning(workspace):
    assert security.webfetch_domain_warning("WebFetch", {"url": "https://arxiv.org/abs/1"}) is None


def test_webfetch_unknown_domain_warns(workspace):
    assert security.webfetch_domain_warning("WebFetch", {"url": "https://evil.example/x"}) == "evil.example"


def test_FINDING_webfetch_subdomain_of_lookalike(workspace):
    w = security.webfetch_domain_warning("WebFetch", {"url": "https://arxiv.org.evil.com/x"})
    assert w == "arxiv.org.evil.com"


# --------------------------------------------------------------------------
# Cross-module wiring
# --------------------------------------------------------------------------
def test_orchestrator_tools_superset_of_all_agents(workspace):
    """Top-level ClaudeAgentOptions.tools must be a superset of every
    subagent's tools, or the CLI hard-denies the missing ones."""
    from thesis_agent.orchestrator import build_options
    from thesis_agent.agents import build_agents

    opts = build_options(workspace)
    top = set(opts.tools)
    for name, adef in build_agents(workspace).items():
        for t in (adef.tools or []):
            assert t in top, f"{name} needs '{t}' missing from orchestrator tools"


def test_save_draft_is_gated_not_auto_allowed():
    from thesis_agent.permissions import READ_ONLY_TOOLS
    from thesis_agent.agents import SOGO_GATED_TOOLS
    assert "mcp__sogo__save_draft" in SOGO_GATED_TOOLS
    assert not (set(SOGO_GATED_TOOLS) & READ_ONLY_TOOLS)


def test_sogo_read_and_gated_disjoint():
    from thesis_agent.agents import SOGO_READ_ONLY_TOOLS, SOGO_GATED_TOOLS, SOGO_TOOLS
    assert not (set(SOGO_READ_ONLY_TOOLS) & set(SOGO_GATED_TOOLS))
    assert set(SOGO_TOOLS) == set(SOGO_READ_ONLY_TOOLS) | set(SOGO_GATED_TOOLS)


def test_all_five_agents_registered(workspace):
    from thesis_agent.agents import build_agents
    assert set(build_agents(workspace)) == {
        "thesis-agent", "research-agent", "planning-agent",
        "coding-agent", "email-agent",
    }


def test_planning_agent_has_bash_and_calendar_only(workspace):
    from thesis_agent.agents import build_agents
    assert build_agents(workspace)["planning-agent"].tools == [
        "Bash", "mcp__sogo__upcoming_events"]


def test_readonly_sogo_tools_are_auto_allowed():
    from thesis_agent.permissions import READ_ONLY_TOOLS
    from thesis_agent.agents import SOGO_READ_ONLY_TOOLS
    for t in SOGO_READ_ONLY_TOOLS:
        assert t in READ_ONLY_TOOLS


def test_summarize_bash_and_write():
    from thesis_agent.permissions import _summarize_input
    assert _summarize_input("Bash", {"command": "ls -la"}) == "ls -la"
    assert _summarize_input("Write", {"file_path": "/x/y.md"}) == "/x/y.md"


def test_FINDING_draft_summary_is_raw_json(workspace):
    from thesis_agent.permissions import _summarize_input
    s = _summarize_input("mcp__sogo__save_draft",
                         {"to": "x@y.com", "body": "confidential text"})
    assert "confidential text" in s


# --------------------------------------------------------------------------
# Web layer: approval broker, event bus, lockdown, recents, wiring
# --------------------------------------------------------------------------
from thesis_agent.web import bridge  # noqa: E402
from thesis_agent.permissions import (  # noqa: E402
    ApprovalRequest,
    AuditEvent,
    terminal_approval_provider,
)


def test_web_approval_resolves_allow():
    async def inner():
        broker = bridge.WebApprovalBroker()
        req = ApprovalRequest("Write", "agentX", "summary", "tid1")

        async def consumer():
            pending = await broker.next_pending()
            assert pending.request.tool_use_id == "tid1"
            pending.resolve(True)

        res, _ = await asyncio.gather(broker.request_approval(req), consumer())
        return res

    assert _run(inner()) is True


def test_web_approval_resolves_deny():
    async def inner():
        broker = bridge.WebApprovalBroker()

        async def consumer():
            (await broker.next_pending()).resolve(False)

        res, _ = await asyncio.gather(
            broker.request_approval(ApprovalRequest("Bash", "a", "rm x", "t")),
            consumer(),
        )
        return res

    assert _run(inner()) is False


def test_web_approval_resolve_is_idempotent():
    async def inner():
        loop = asyncio.get_running_loop()
        p = bridge.PendingApproval(
            request=ApprovalRequest("Write", "a", "s", "t"),
            future=loop.create_future(),
        )
        p.resolve(False)
        p.resolve(True)  # no-op, must not raise
        return await p.future

    assert _run(inner()) is False


def test_web_event_bus_records_and_forwards():
    async def inner():
        bus = bridge.WebEventBus()
        seen = []

        async def listener(e):
            seen.append(e)

        bus.set_listener(listener)
        await bus.emit(AuditEvent("ts", "agent", "Write", "sum", "allow"))
        await bus.emit(AuditEvent("ts", "agent", "Read", "sum", "deny", security_block=True))
        return bus.events, seen

    events, seen = _run(inner())
    assert [e.tool_name for e in events] == ["Write", "Read"]
    assert len(seen) == 2 and seen[1].security_block is True


def test_lockdown_toggle_roundtrip(workspace):
    assert not bridge.lockdown_active(workspace)
    assert bridge.set_lockdown(workspace, True) is True
    assert workspace.lockdown_file.exists()
    assert ev("Read", {"file_path": str(workspace.workspace_dir / "a.md")}, workspace).blocked
    assert bridge.set_lockdown(workspace, False) is False
    assert not workspace.lockdown_file.exists()
    assert not ev("Read", {"file_path": str(workspace.workspace_dir / "a.md")}, workspace).blocked


def test_recents_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RECENTS_PATH", tmp_path / "recents.json")
    ws = tmp_path / "w"
    ws.mkdir()
    bridge.remember_workspace(ws)
    assert str(ws.resolve()) in bridge.load_recent_workspaces()


def test_recents_dedupes_and_orders(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RECENTS_PATH", tmp_path / "recents.json")
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    bridge.remember_workspace(a)
    bridge.remember_workspace(b)
    bridge.remember_workspace(a)
    got = bridge.load_recent_workspaces()
    assert got[0] == str(a.resolve())
    assert got.count(str(a.resolve())) == 1


def test_terminal_provider_denies_on_eof(monkeypatch):
    def boom(*a, **k):
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)

    async def inner():
        return await terminal_approval_provider(ApprovalRequest("Write", "a", "s", "t"))

    assert _run(inner()) is False


def test_build_options_accepts_web_injectables(workspace):
    from thesis_agent.orchestrator import build_options

    async def prov(req):
        return True

    async def sink(ev_):
        return None

    opts = build_options(workspace, approval_provider=prov, event_sink=sink)
    assert "PreToolUse" in opts.hooks
    assert "PreToolUse" in build_options(workspace).hooks


# --------------------------------------------------------------------------
# Speed configuration: per-agent models + turn caps + orchestrator model
# --------------------------------------------------------------------------
def test_agent_models_follow_fast_smart_split(workspace):
    from thesis_agent.agents import build_agents, FAST_MODEL, SMART_MODEL
    agents = build_agents(workspace)
    expected = {
        "thesis-agent": FAST_MODEL,
        "email-agent": FAST_MODEL,
        "coding-agent": FAST_MODEL,
        "planning-agent": FAST_MODEL,
        "research-agent": SMART_MODEL,
    }
    for name, model in expected.items():
        assert agents[name].model == model, f"{name} should be {model}"


def test_every_agent_has_a_turn_cap(workspace):
    from thesis_agent.agents import build_agents
    for name, adef in build_agents(workspace).items():
        assert isinstance(adef.maxTurns, int) and adef.maxTurns > 0, f"{name} missing maxTurns"


def test_orchestrator_runs_on_smart_model(workspace):
    """The orchestrator must stay on the smart model -- the fast model
    mishandles the invoke-subagent -> wait -> synthesize loop and returns a
    premature 'I'll get back to you' non-answer."""
    from thesis_agent.orchestrator import build_options
    from thesis_agent.agents import SMART_MODEL
    assert build_options(workspace).model == SMART_MODEL


def test_model_env_overrides(tmp_path, monkeypatch):
    """FAST/SMART models are env-overridable (re-import to pick up the env)."""
    import importlib
    monkeypatch.setenv("THESIS_AGENT_FAST_MODEL", "custom-fast")
    monkeypatch.setenv("THESIS_AGENT_SMART_MODEL", "custom-smart")
    import thesis_agent.agents as agents_mod
    importlib.reload(agents_mod)
    try:
        assert agents_mod.FAST_MODEL == "custom-fast"
        assert agents_mod.SMART_MODEL == "custom-smart"
    finally:
        # restore module state for any later tests importing it
        monkeypatch.delenv("THESIS_AGENT_FAST_MODEL", raising=False)
        monkeypatch.delenv("THESIS_AGENT_SMART_MODEL", raising=False)
        importlib.reload(agents_mod)


# --------------------------------------------------------------------------
# Web UI: default workspace + focus areas
# --------------------------------------------------------------------------
def test_default_workspace_env_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RECENTS_PATH", tmp_path / "recents.json")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    bridge.remember_workspace(a)
    monkeypatch.setenv("THESIS_AGENT_WORKSPACE", str(b))
    assert bridge.default_workspace() == str(b)


def test_default_workspace_falls_back_to_recent(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RECENTS_PATH", tmp_path / "recents.json")
    monkeypatch.setenv("THESIS_AGENT_WORKSPACE", str(tmp_path / "missing"))
    a = tmp_path / "a"
    a.mkdir()
    bridge.remember_workspace(a)
    assert bridge.default_workspace() == str(a.resolve())


def test_default_workspace_none_when_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RECENTS_PATH", tmp_path / "recents.json")
    monkeypatch.delenv("THESIS_AGENT_WORKSPACE", raising=False)
    assert bridge.default_workspace() is None


def test_apply_focus():
    from thesis_agent.orchestrator import FOCUS_AREAS, apply_focus

    assert apply_focus("hi", None) == "hi"
    assert apply_focus("hi", "bogus") == "hi"
    for key, (label, directive) in FOCUS_AREAS.items():
        out = apply_focus("hi", key)
        assert out.startswith(f"[Session focus: {label}")
        assert directive in out and out.endswith("\n\nhi")


# --------------------------------------------------------------------------
# Bash: symlinks into the sensitive dir
# --------------------------------------------------------------------------
@pytest.fixture
def linked(workspace):
    """A workspace file `data/raw/p.xlsx` symlinked into the sensitive dir."""
    real = workspace.sensitive_data_dir / "p.xlsx"
    real.write_text("secret")
    raw = workspace.workspace_dir / "data" / "raw"
    raw.mkdir(parents=True)
    (raw / "p.xlsx").symlink_to(real)
    return workspace


def test_bash_symlink_relative_blocked(linked):
    assert ev("Bash", {"command": "head data/raw/p.xlsx"}, linked).blocked


def test_bash_symlink_quoted_absolute_blocked(linked):
    path = linked.workspace_dir / "data" / "raw" / "p.xlsx"
    assert ev("Bash", {"command": f'python -c x "{path}"'}, linked).blocked


def test_bash_symlink_flag_value_blocked(linked):
    assert ev("Bash", {"command": "tool --in=data/raw/p.xlsx"}, linked).blocked


def test_bash_symlink_glob_blocked(linked):
    assert ev("Bash", {"command": "cat data/raw/*"}, linked).blocked


def test_bash_unrelated_command_allowed(linked):
    assert not ev("Bash", {"command": "ls data && git log -3"}, linked).blocked


def test_bash_unbalanced_quotes_does_not_crash(linked):
    ev("Bash", {"command": "echo 'unterminated"}, linked)  # must not raise


def test_planning_route_does_not_require_research_agent(workspace):
    """Status/next-steps must not pull in the slow research-agent by default."""
    from thesis_agent.agents import build_agents
    from thesis_agent.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT

    agents = build_agents(workspace)
    assert "Do NOT run research-agent" in agents["planning-agent"].description
    assert "thesis-agent and/or research-agent" not in ORCHESTRATOR_SYSTEM_PROMPT
    assert str(workspace.research_log_file) in agents["thesis-agent"].prompt


# --------------------------------------------------------------------------
# Never send email + SOGo tools
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", [
    "mcp__sogo__send_mail", "mcp__anything__reply", "mcp__x__forward_message",
    "mcp__mail__smtp_send", "mcp__claude_ai_Gmail__send_message", "SendMessage",
])
def test_send_like_tools_hard_blocked(workspace, name):
    assert ev(name, {}, workspace).blocked


def test_gmail_read_tools_blocked_too(workspace):
    assert ev("mcp__claude_ai_Gmail__search_threads", {"query": "x"}, workspace).blocked


@pytest.mark.parametrize("cmd", [
    "python -c 'import smtplib'", "sendmail advisor@uni.de < d.txt",
    "openssl s_client -connect smtp.uni-koblenz.de:465",
    "security find-generic-password -s thesis-agent-sogo -w",
])
def test_bash_send_and_keychain_blocked(workspace, cmd):
    assert ev("Bash", {"command": cmd}, workspace).blocked


def test_sogo_tools_not_send_blocked(workspace):
    from thesis_agent.agents import SOGO_TOOLS
    for t in SOGO_TOOLS:
        assert not ev(t, {"to": "a@b.de", "subject": "s", "body": "hi"}, workspace).blocked


def test_no_smtp_code_in_project():
    src = Path(__file__).resolve().parents[1] / "src" / "thesis_agent"
    for f in src.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "import smtplib" not in text and "SMTP(" not in text, f


def test_options_strict_mcp_and_no_gmail(workspace):
    from thesis_agent.orchestrator import build_options
    opts = build_options(workspace)
    assert opts.strict_mcp_config is True
    assert set(opts.mcp_servers) == {"arxiv", "sogo"}
    assert not [t for t in opts.tools if "gmail" in t.lower()]


class FakeIMAP:
    """Records every command; serves one message in INBOX and a Drafts folder."""

    instances: list = []
    RAW = (b"Date: Mon, 21 Sep 2026 10:00:00 +0200\r\nFrom: Prof X <x@uni.de>\r\n"
           b"To: me@uni.de\r\nSubject: Feedback on chapter 3\r\n\r\nPlease revise by Friday.\r\n")

    def __init__(self, host, port, timeout=None):
        self.calls = []
        FakeIMAP.instances.append(self)

    def login(self, user, pw):
        self.calls.append(("login", user))
        return "OK", [b"ok"]

    def logout(self):
        return "BYE", [b""]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                      b'(\\HasNoChildren \\Drafts) "/" "Drafts"',
                      b'(\\HasNoChildren \\Sent) "/" "Sent"']

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        self.calls.append(("uid", cmd) + args)
        if cmd == "SEARCH":
            return "OK", [b"7"]
        if cmd == "FETCH":
            return "OK", [(b"1 (UID 7 BODY[] {100}", self.RAW), b")"]
        raise AssertionError(cmd)

    def append(self, mailbox, flags, date, msg):
        self.calls.append(("append", mailbox, flags, msg))
        return "OK", [b"appended"]

    def enable(self, cap):
        return "OK", [b""]


@pytest.fixture
def fake_imap(monkeypatch):
    from thesis_agent.tools import sogo
    FakeIMAP.instances.clear()
    monkeypatch.setattr(sogo.imaplib, "IMAP4_SSL", FakeIMAP)
    return sogo


def _all_calls():
    return [c for inst in FakeIMAP.instances for c in inst.calls]


def test_sogo_reads_are_read_only_peek(fake_imap):
    out = fake_imap.search_mail_text("u", "pw", query="feedback")
    assert "uid 7" in out and "Feedback on chapter 3" in out
    text = fake_imap.read_mail_text("u", "pw", "INBOX", "7")
    assert "Please revise by Friday." in text
    calls = _all_calls()
    assert all(c[2] is True for c in calls if c[0] == "select")  # EXAMINE only
    fetches = [c for c in calls if c[:2] == ("uid", "FETCH")]
    assert fetches and all("BODY.PEEK" in c[-1] for c in fetches)
    assert not [c for c in calls if c[0] == "append"]


def test_save_draft_appends_only_to_drafts(fake_imap):
    out = fake_imap.save_draft_text("u", "pw", "x@uni.de", "Re: chapter 3", "Thanks!")
    assert "NOT been sent" in out
    appends = [c for c in _all_calls() if c[0] == "append"]
    assert len(appends) == 1
    _, mailbox, flags, msg = appends[0]
    assert mailbox == '"Drafts"' and "\\Draft" in flags
    assert b"Subject: Re: chapter 3" in msg


def test_save_draft_rejects_header_injection(fake_imap):
    with pytest.raises(fake_imap.SogoError):
        fake_imap.save_draft_text("u", "pw", "x@uni.de\r\nBcc: evil@x.com", "s", "b")


def test_search_quotes_and_strips_crlf(fake_imap):
    fake_imap.search_mail_text("u", "pw", sender='a"b\r\nX')
    search = [c for c in _all_calls() if c[:2] == ("uid", "SEARCH")][0]
    assert search[2:] == ("FROM", '"a\\"b  X"')


def test_unreachable_server_gives_vpn_hint(monkeypatch):
    from thesis_agent.tools import sogo

    def boom(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(sogo.imaplib, "IMAP4_SSL", boom)
    with pytest.raises(sogo.SogoError, match="VPN"):
        sogo.list_folders_text("u", "pw")


def test_tool_errors_never_leak_password(monkeypatch):
    from thesis_agent.tools import sogo
    secret = "s3cr3t-Pa55"
    monkeypatch.setenv("THESIS_AGENT_SOGO_USER", "u")
    monkeypatch.setattr(sogo, "_password", lambda user: secret)

    def leaky(user, password, **kw):
        raise RuntimeError(f"auth failed for {user}:{password}")

    res = _run(sogo._call(leaky))
    assert res["is_error"] and secret not in res["content"][0]["text"]


def test_missing_user_explains_setup(monkeypatch):
    from thesis_agent.tools import sogo
    monkeypatch.delenv("THESIS_AGENT_SOGO_USER", raising=False)
    res = _run(sogo._call(sogo.list_folders_text))
    assert res["is_error"] and "THESIS_AGENT_SOGO_USER" in res["content"][0]["text"]


def test_subagents_never_backgrounded(workspace):
    from thesis_agent.orchestrator import build_options
    assert build_options(workspace).env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


def test_calendar_formats_and_sorts_events(monkeypatch):
    import datetime as dt
    import caldav
    from thesis_agent.tools import sogo

    class Ev:
        def __init__(self, start, title):
            self.icalendar_component = {"dtstart": type("D", (), {"dt": start})(),
                                        "summary": title}

    class Cal:
        name = "Personal"
        def search(self, **kw):
            assert kw["event"] and kw["expand"]
            return [Ev(dt.datetime(2026, 9, 30, 14, 0), "Advisor meeting"),
                    Ev(dt.date(2026, 9, 25), "Chapter 3 deadline")]

    class Client:
        def __init__(self, **kw):
            assert kw["timeout"] == sogo.TIMEOUT
        def principal(self):
            return type("P", (), {"calendars": lambda self: [Cal()]})()

    monkeypatch.setattr(caldav, "DAVClient", Client)
    out = sogo.upcoming_events_text("u", "pw", days=14)
    assert out.index("Chapter 3 deadline") < out.index("Advisor meeting")
    assert "(all day)" in out and "14:00" in out


def test_calendar_unreachable_is_friendly(monkeypatch):
    import caldav
    from caldav.lib.http_sync import requests as caldav_http
    from thesis_agent.tools import sogo

    def boom(**kw):
        raise caldav_http.exceptions.ConnectionError("down")

    monkeypatch.setattr(caldav, "DAVClient", boom)
    with pytest.raises(sogo.SogoError, match="not reachable"):
        sogo.upcoming_events_text("u", "pw")


# --------------------------------------------------------------------------
# Web UI server (FastAPI + WebSocket)
# --------------------------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

LOCAL = "http://127.0.0.1:8000"
# TestClient ignores base_url for WebSockets (sends Host: testserver).
WS_HEADERS = {"origin": LOCAL, "host": "127.0.0.1:8000"}


class FakeSDKClient:
    """Stands in for ClaudeSDKClient: one tool call, then an answer."""

    def __init__(self, options=None):
        self.options = options
        self.prompts = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def interrupt(self):
        pass

    async def query(self, prompt):
        self.prompts.append(prompt)

    async def receive_response(self):
        from claude_agent_sdk import (AssistantMessage, TextBlock, ToolResultBlock,
                                      ToolUseBlock, UserMessage)
        yield AssistantMessage(content=[ToolUseBlock(id="t1", name="Read",
                                                     input={"file_path": "notes.md"})],
                               model="m")
        yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="hello")])
        yield AssistantMessage(content=[TextBlock(text="**Done.**")], model="m")


@pytest.fixture
def web(workspace, monkeypatch, tmp_path):
    from thesis_agent.web import server
    monkeypatch.setattr(server, "ClaudeSDKClient", FakeSDKClient)
    monkeypatch.setattr(bridge, "RECENTS_PATH", tmp_path / "recents.json")
    monkeypatch.setenv("THESIS_AGENT_WORKSPACE", str(workspace.workspace_dir))
    return TestClient(server.create_app(), base_url=LOCAL)


def _recv_until(ws, wanted):
    seen = []
    while True:
        msg = ws.receive_json()
        seen.append(msg)
        if msg["type"] == wanted:
            return msg, seen


def test_web_index_served_only_to_localhost(web):
    assert web.get("/").status_code == 200
    assert "Thesis Agent" in web.get("/").text
    assert web.get("/", headers={"host": "evil.example"}).status_code == 400


def test_web_ws_rejects_foreign_origin(web):
    with pytest.raises(WebSocketDisconnect):
        with web.websocket_connect("/ws", headers={"origin": "https://evil.example",
                                                   "host": "127.0.0.1:8000"}) as ws:
            ws.receive_json()


def test_web_ws_rejects_missing_origin(web):
    with pytest.raises(WebSocketDisconnect):
        with web.websocket_connect("/ws", headers={"host": "127.0.0.1:8000"}) as ws:
            ws.receive_json()


def test_web_full_turn(web, workspace):
    with web.websocket_connect("/ws", headers=WS_HEADERS) as ws:
        ws.send_json({"type": "hello"})
        session, _ = _recv_until(ws, "session")
        assert session["workspace"] == str(workspace.workspace_dir)
        assert {f["key"] for f in session["focus_areas"]} >= {"planning", "coding"}

        ws.send_json({"type": "prompt", "text": "status?", "focus": "planning"})
        end, seen = _recv_until(ws, "turn_end")
        kinds = [m["type"] for m in seen]
        assert kinds.index("tool_start") < kinds.index("tool_end") < kinds.index("text")
        start = next(m for m in seen if m["type"] == "tool_start")
        assert start["label"] == "Read" and start["kind"] == "read"
        assert start["detail"] == "notes.md"
        assert next(m for m in seen if m["type"] == "text")["text"] == "**Done.**"


def test_web_prompt_carries_focus_directive(web, monkeypatch):
    from thesis_agent.web import server
    made = []
    monkeypatch.setattr(server, "ClaudeSDKClient",
                        lambda options=None: made.append(FakeSDKClient(options)) or made[-1])
    with web.websocket_connect("/ws", headers=WS_HEADERS) as ws:
        ws.send_json({"type": "hello"})
        _recv_until(ws, "session")
        ws.send_json({"type": "prompt", "text": "fix tests", "focus": "coding"})
        _recv_until(ws, "turn_end")
    assert made[-1].prompts[0].startswith("[Session focus: Coding")


def test_web_no_workspace_asks_for_one(web, monkeypatch):
    monkeypatch.delenv("THESIS_AGENT_WORKSPACE")
    with web.websocket_connect("/ws", headers=WS_HEADERS) as ws:
        ws.send_json({"type": "hello"})
        msg, _ = _recv_until(ws, "need_workspace")
        ws.send_json({"type": "set_workspace", "path": "/definitely/not/here"})
        msg, _ = _recv_until(ws, "need_workspace")
        assert "does not exist" in msg["error"]


def test_web_lockdown_toggle(web, workspace):
    with web.websocket_connect("/ws", headers=WS_HEADERS) as ws:
        ws.send_json({"type": "hello"})
        _recv_until(ws, "session")
        ws.send_json({"type": "lockdown", "active": True})
        msg, _ = _recv_until(ws, "lockdown")
        assert msg["active"] and workspace.lockdown_file.exists()
        ws.send_json({"type": "lockdown", "active": False})
        msg, _ = _recv_until(ws, "lockdown")
        assert not msg["active"] and not workspace.lockdown_file.exists()


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        import json as _json
        self.sent.append(_json.loads(text))


def test_web_session_approval_roundtrip_and_fail_closed():
    from thesis_agent.permissions import ApprovalRequest
    from thesis_agent.web.server import Session

    async def inner():
        s = Session(_FakeWS())
        s.broker = bridge.WebApprovalBroker()
        s.consumer = asyncio.create_task(s._consume_approvals())

        allowed = asyncio.create_task(
            s.broker.request_approval(ApprovalRequest("Write", "coding", "/x.py", "t9")))
        await asyncio.sleep(0.01)
        req = next(m for m in s.ws.sent if m["type"] == "approval_request")
        assert req["label"] == "Write" and req["kind"] == "write" and req["tool_use_id"] == "t9"
        await s.handle({"type": "approval", "id": req["id"], "decision": "allow"})
        first = await allowed

        pending = asyncio.create_task(
            s.broker.request_approval(ApprovalRequest("Bash", "coding", "ls", "t10")))
        await asyncio.sleep(0.01)
        await s.stop()  # tab closed: anything still waiting must be denied
        return first, await pending

    assert _run(inner()) == (True, False)


def test_tool_labels():
    from thesis_agent.web.server import tool_detail, tool_label
    assert tool_label("mcp__sogo__save_draft") == ("Save draft", "mail")
    assert tool_label("mcp__sogo__upcoming_events")[1] == "calendar"
    assert tool_label("Agent") == ("Delegate", "agent")
    assert tool_detail("Bash", {"command": "pytest  -q\n"}) == "pytest -q"


# --------------------------------------------------------------------------
# Delegation limited to this project's own subagents
# --------------------------------------------------------------------------
def test_allowed_subagents_match_defined_agents(workspace):
    from thesis_agent.agents import build_agents
    assert security.ALLOWED_SUBAGENTS == set(build_agents(workspace))


@pytest.mark.parametrize("inp", [
    {"description": "Continue survey", "prompt": "..."},                 # no type
    {"subagent_type": "general-purpose", "prompt": "..."},
    {"subagent_type": "Explore", "prompt": "..."},
])
def test_generic_agent_delegation_blocked(workspace, inp):
    v = ev("Agent", inp, workspace)
    assert v.blocked and "thesis-agent" in v.reason


def test_own_subagent_delegation_allowed(workspace):
    assert not ev("Agent", {"subagent_type": "thesis-agent", "prompt": "status"},
                  workspace).blocked


def test_thesis_agent_turn_cap_raised(workspace):
    from thesis_agent.agents import build_agents
    assert build_agents(workspace)["thesis-agent"].maxTurns >= 20
