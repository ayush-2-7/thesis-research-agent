"""Uni Koblenz SOGo mail (IMAP) and calendar (CalDAV) tools.

Read + draft only, by construction: there is no SMTP code anywhere in this
project, and `save_draft` can only IMAP-APPEND into the Drafts mailbox (the
target folder is not a parameter). The researcher reviews and sends drafts
from SOGo themselves. security.py additionally hard-blocks any send-like
tool name and mail-sending shell commands, so "never send" doesn't depend on
this module alone.

Mail reads never change mailbox state: folders are opened read-only
(EXAMINE) and bodies are fetched with BODY.PEEK, so nothing gets marked as
read.

Credentials: the username (Rechnerkennung) comes from THESIS_AGENT_SOGO_USER;
the password is read from the macOS Keychain on every connect and is never
put in an env var, a file, or any text returned to the model (error messages
are scrubbed of it).

Uni Koblenz specifics: IMAP is only reachable on campus or over the
university's WireGuard VPN; CalDAV on sogo.uni-koblenz.de is reachable from
anywhere. SOGo's TOTP MFA guards only the web login, not IMAP/CalDAV.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import email
import email.policy
import html
import imaplib
import os
import re
import subprocess
import time
from contextlib import contextmanager
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Callable, Iterator

from claude_agent_sdk import create_sdk_mcp_server, tool

IMAP_HOST = os.environ.get("THESIS_AGENT_SOGO_IMAP_HOST", "imap.uni-koblenz.de")
IMAP_PORT = 993
DAV_URL = os.environ.get(
    "THESIS_AGENT_SOGO_DAV_URL", "https://sogo.uni-koblenz.de/SOGo/dav/"
)
KEYCHAIN_SERVICE = "thesis-agent-sogo"
TIMEOUT = 15
MAX_BODY_CHARS = 20_000

VPN_HINT = (
    f"Mail server {IMAP_HOST} is not reachable. Uni Koblenz mail only works "
    "on the campus network or over the university VPN (WireGuard) -- connect "
    "to the VPN and try again. (The calendar works without the VPN.)"
)


class SogoError(Exception):
    """A failure whose message is safe and useful to show the model."""


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------
def _user() -> str:
    user = os.environ.get("THESIS_AGENT_SOGO_USER", "").strip()
    if not user:
        raise SogoError(
            "SOGo is not configured: set THESIS_AGENT_SOGO_USER to your "
            "Rechnerkennung (e.g. in ~/.zshrc) and restart the app."
        )
    return user


def _password(user: str) -> str:
    setup = (
        "No SOGo password found in the macOS Keychain. The researcher must run "
        f"(in a terminal): security add-generic-password -s {KEYCHAIN_SERVICE} "
        f"-a {user} -w"
    )
    try:
        res = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", user, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SogoError(f"{setup} (Keychain lookup failed: {type(e).__name__})")
    password = res.stdout.rstrip("\n")
    if res.returncode != 0 or not password:
        raise SogoError(setup)
    return password


def _scrub(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


# --------------------------------------------------------------------------
# IMAP helpers
# --------------------------------------------------------------------------
@contextmanager
def _imap(user: str, password: str) -> Iterator[imaplib.IMAP4_SSL]:
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=TIMEOUT)
    except OSError as e:  # includes timeouts and refused connections
        raise SogoError(f"{VPN_HINT} ({type(e).__name__})")
    try:
        try:
            conn.login(user, password)
        except imaplib.IMAP4.error:
            raise SogoError(
                f"Login to {IMAP_HOST} rejected for user '{user}'. Check the "
                "Rechnerkennung and the password stored in the Keychain."
            )
        yield conn
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass


def _quote(value: str) -> str:
    """IMAP quoted string. CR/LF are dropped so input can't inject commands."""
    value = value.replace("\r", " ").replace("\n", " ")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _check(typ: str, data: Any, what: str) -> Any:
    if typ != "OK":
        detail = b" ".join(d for d in data if isinstance(d, bytes)).decode(
            "utf-8", "replace"
        )
        raise SogoError(f"{what} failed: {detail or typ}")
    return data


def _examine(conn: imaplib.IMAP4_SSL, folder: str) -> None:
    """Open `folder` read-only (EXAMINE): nothing in it can change."""
    _check(*conn.select(_quote(folder), readonly=True), f"Opening folder '{folder}'")


_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)')


def _folders(conn: imaplib.IMAP4_SSL) -> list[tuple[str, str]]:
    """[(name, flags)] for every mailbox, names as the server spells them."""
    data = _check(*conn.list(), "Listing folders")
    out = []
    for line in data:
        if not isinstance(line, bytes):
            continue
        m = _LIST_RE.match(line)
        if not m:
            continue
        name = m.group("name").decode("utf-8", "replace").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        out.append((name, m.group("flags").decode("ascii", "replace")))
    return out


def _decode_mutf7(name: str) -> str:
    """IMAP modified UTF-7 -> readable text (e.g. 'Entw&APw-rfe' -> 'Entwürfe')."""
    def repl(m: re.Match) -> str:
        chunk = m.group(1)
        if not chunk:
            return "&"
        b64 = chunk.replace(",", "/")
        b64 += "=" * (-len(b64) % 4)
        try:
            return base64.b64decode(b64).decode("utf-16-be")
        except Exception:  # noqa: BLE001 - show the raw name instead
            return m.group(0)
    return re.sub(r"&([^-]*)-", repl, name)


def _html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style).*?</\1>", "", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", "", markup))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


# --------------------------------------------------------------------------
# Operations (plain functions: user, password first -- unit-testable)
# --------------------------------------------------------------------------
def list_folders_text(user: str, password: str) -> str:
    with _imap(user, password) as conn:
        folders = _folders(conn)
    lines = ["Mail folders (pass the name exactly as shown in quotes):"]
    for name, flags in folders:
        readable = _decode_mutf7(name)
        label = f' = {readable}' if readable != name else ""
        special = " ".join(f for f in flags.split() if f.lower() in
                           ("\\drafts", "\\sent", "\\trash", "\\junk", "\\archive"))
        lines.append(f'- "{name}"{label}{"  " + special if special else ""}')
    return "\n".join(lines)


def search_mail_text(
    user: str,
    password: str,
    query: str = "",
    sender: str = "",
    since: str = "",
    folder: str = "INBOX",
    limit: int = 20,
) -> str:
    limit = min(max(int(limit or 20), 1), 50)
    criteria: list[str] = []
    if sender:
        criteria += ["FROM", _quote(sender)]
    if query:
        criteria += ["TEXT", _quote(query)]
    if since:
        try:
            day = dt.date.fromisoformat(since)
        except ValueError:
            raise SogoError(f"'since' must be YYYY-MM-DD, got {since!r}.")
        criteria += ["SINCE", day.strftime("%d-%b-%Y")]
    if not criteria:
        criteria = ["ALL"]

    with _imap(user, password) as conn:
        if any(not c.isascii() for c in criteria):
            try:
                conn.enable("UTF8=ACCEPT")
            except imaplib.IMAP4.error:
                raise SogoError("This server only accepts ASCII search terms.")
        _examine(conn, folder)
        data = _check(*conn.uid("SEARCH", *criteria), "Search")
        uids = (data[0] or b"").split()
        total = len(uids)
        uids = list(reversed(uids[-limit:]))  # newest first
        if not uids:
            return f"No messages in '{folder}' match {' '.join(criteria)}."
        data = _check(
            *conn.uid("FETCH", b",".join(uids).decode(),
                      "(BODY.PEEK[HEADER.FIELDS (DATE FROM SUBJECT)])"),
            "Fetching headers",
        )

    rows: dict[bytes, str] = {}
    for item in data:
        if not isinstance(item, tuple):
            continue
        m = re.search(rb"UID (\d+)", item[0])
        if not m:
            continue
        hdr = email.message_from_bytes(item[1], policy=email.policy.default)
        rows[m.group(1)] = (
            f"- uid {m.group(1).decode()} | {hdr.get('date', '?')} | "
            f"{hdr.get('from', '?')} | {hdr.get('subject', '(no subject)')}"
        )
    lines = [
        f"{total} match(es) in '{folder}', showing the newest {len(uids)} "
        "(use read_mail with folder + uid for the full text):"
    ]
    lines += [rows[u] for u in uids if u in rows]
    return "\n".join(lines)


def read_mail_text(user: str, password: str, folder: str, uid: str) -> str:
    uid = str(uid).strip()
    if not uid.isdigit():
        raise SogoError(f"uid must be a number from search_mail, got {uid!r}.")
    with _imap(user, password) as conn:
        _examine(conn, folder)
        data = _check(*conn.uid("FETCH", uid, "(BODY.PEEK[])"), "Fetching message")
    raw = next((item[1] for item in data if isinstance(item, tuple)), None)
    if raw is None:
        raise SogoError(f"No message with uid {uid} in '{folder}'.")

    msg = email.message_from_bytes(raw, policy=email.policy.default)
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        body = "(no text body)"
    else:
        body = part.get_content()
        if part.get_content_type() == "text/html":
            body = _html_to_text(body)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n[... truncated]"
    attachments = [p.get_filename() or "(unnamed)" for p in msg.iter_attachments()]

    head = [f"{h}: {msg[h]}" for h in ("Date", "From", "To", "Cc", "Subject")
            if msg[h]]
    if attachments:
        head.append("Attachments (not opened): " + ", ".join(attachments))
    return "\n".join(head) + "\n\n" + body


def _drafts_folder(conn: imaplib.IMAP4_SSL) -> str:
    folders = _folders(conn)
    for name, flags in folders:
        if "\\drafts" in flags.lower():
            return name
    for name, _flags in folders:
        if name.lower().rsplit("/", 1)[-1].rsplit(".", 1)[-1] in ("drafts", "entwürfe"):
            return name
    raise SogoError("Could not find a Drafts folder in this mailbox.")


def save_draft_text(
    user: str, password: str, to: str, subject: str, body: str, cc: str = ""
) -> str:
    for field_name, value in (("to", to), ("cc", cc), ("subject", subject)):
        if "\r" in value or "\n" in value:
            raise SogoError(f"'{field_name}' must be a single line.")
    if not to.strip():
        raise SogoError("A draft needs at least one recipient in 'to'.")

    msg = EmailMessage()
    sender = os.environ.get("THESIS_AGENT_SOGO_FROM", "").strip()
    if sender:
        msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)

    with _imap(user, password) as conn:
        folder = _drafts_folder(conn)
        _check(
            *conn.append(_quote(folder), r"(\Draft \Seen)",
                         imaplib.Time2Internaldate(time.time()), msg.as_bytes()),
            "Saving draft",
        )
    return (
        f"Draft saved to '{_decode_mutf7(folder)}' (to: {to}; subject: {subject}). "
        "It has NOT been sent -- the researcher reviews and sends it from SOGo."
    )


def upcoming_events_text(user: str, password: str, days: int = 14) -> str:
    import caldav
    # caldav talks HTTP through niquests when installed, else requests --
    # catch whichever one it actually uses.
    from caldav.lib.http_sync import requests as caldav_http

    days = min(max(int(days or 14), 1), 90)
    start = dt.datetime.now().astimezone()
    end = start + dt.timedelta(days=days)
    try:
        client = caldav.DAVClient(
            url=DAV_URL, username=user, password=password, timeout=TIMEOUT
        )
        calendars = client.principal().calendars()
        events = []
        for cal in calendars:
            for ev in cal.search(start=start, end=end, event=True, expand=True):
                comp = ev.icalendar_component
                begin = comp.get("dtstart").dt if comp.get("dtstart") else None
                events.append((
                    _sort_key(begin),
                    _fmt_when(begin),
                    str(comp.get("summary", "(no title)")),
                    str(comp.get("location", "") or ""),
                    str(cal.name or ""),
                ))
    except caldav.lib.error.AuthorizationError:
        raise SogoError(
            f"Calendar login rejected for user '{user}'. Check the "
            "Rechnerkennung and the Keychain password."
        )
    except (caldav_http.exceptions.RequestException, OSError) as e:
        raise SogoError(f"Calendar server not reachable ({type(e).__name__}).")

    if not events:
        return f"No calendar events in the next {days} day(s)."
    events.sort()
    lines = [f"Calendar events in the next {days} day(s):"]
    for _k, when, title, location, cal_name in events:
        extra = f" @ {location}" if location else ""
        src = f" [{cal_name}]" if len(calendars) > 1 and cal_name else ""
        lines.append(f"- {when}: {title}{extra}{src}")
    return "\n".join(lines)


def _sort_key(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.astimezone().isoformat() if value.tzinfo else value.isoformat()
    return str(value or "")


def _fmt_when(value: Any) -> str:
    if isinstance(value, dt.datetime):
        v = value.astimezone() if value.tzinfo else value
        return v.strftime("%a %Y-%m-%d %H:%M")
    if isinstance(value, dt.date):
        return value.strftime("%a %Y-%m-%d") + " (all day)"
    return "?"


# --------------------------------------------------------------------------
# SDK tools
# --------------------------------------------------------------------------
async def _call(fn: Callable[..., str], **kwargs: Any) -> dict[str, Any]:
    """Resolve credentials, run `fn` off the event loop, never leak the password."""
    password = ""
    try:
        user = _user()
        password = _password(user)
        text = await asyncio.to_thread(fn, user, password, **kwargs)
        return {"content": [{"type": "text", "text": text}]}
    except SogoError as e:
        text = _scrub(str(e), password)
    except Exception as e:  # noqa: BLE001 - report, don't crash the session
        text = _scrub(f"SOGo error ({type(e).__name__}): {e}", password)
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


_STR = {"type": "string"}


@tool("list_folders", "List the researcher's Uni Koblenz mail folders.", _schema({}, []))
async def list_folders(args: dict[str, Any]) -> dict[str, Any]:
    return await _call(list_folders_text)


@tool(
    "search_mail",
    (
        "Search one mail folder (default INBOX) of the researcher's Uni Koblenz "
        "mailbox. Returns uid, date, sender and subject only, newest first -- "
        "call read_mail for the text. Never marks anything as read."
    ),
    _schema(
        {
            "query": {**_STR, "description": "Words anywhere in the message."},
            "sender": {**_STR, "description": "Part of the sender name/address."},
            "since": {**_STR, "description": "Only mail on/after this date, YYYY-MM-DD."},
            "folder": {**_STR, "description": "Folder name from list_folders. Default INBOX."},
            "limit": {"type": "integer", "description": "Max results, 1-50. Default 20."},
        },
        [],
    ),
)
async def search_mail(args: dict[str, Any]) -> dict[str, Any]:
    return await _call(
        search_mail_text,
        query=args.get("query", ""),
        sender=args.get("sender", ""),
        since=args.get("since", ""),
        folder=args.get("folder") or "INBOX",
        limit=args.get("limit", 20),
    )


@tool(
    "read_mail",
    "Read one message's full text by folder + uid (from search_mail). Never marks it as read.",
    _schema({"folder": _STR, "uid": _STR}, ["folder", "uid"]),
)
async def read_mail(args: dict[str, Any]) -> dict[str, Any]:
    return await _call(read_mail_text, folder=args["folder"], uid=str(args["uid"]))


@tool(
    "save_draft",
    (
        "Save an email as a DRAFT in the researcher's SOGo Drafts folder. It is "
        "never sent -- the researcher reviews and sends it from SOGo. Requires "
        "the researcher's approval."
    ),
    _schema(
        {
            "to": {**_STR, "description": "Recipient address(es), comma-separated."},
            "subject": _STR,
            "body": {**_STR, "description": "Plain-text body."},
            "cc": {**_STR, "description": "Optional CC address(es)."},
        },
        ["to", "subject", "body"],
    ),
)
async def save_draft(args: dict[str, Any]) -> dict[str, Any]:
    return await _call(
        save_draft_text,
        to=args["to"], subject=args["subject"], body=args["body"],
        cc=args.get("cc", ""),
    )


@tool(
    "upcoming_events",
    "List the researcher's SOGo calendar events for the next N days (default 14). Read-only.",
    _schema({"days": {"type": "integer", "description": "1-90, default 14."}}, []),
)
async def upcoming_events(args: dict[str, Any]) -> dict[str, Any]:
    return await _call(upcoming_events_text, days=args.get("days", 14))


sogo_server = create_sdk_mcp_server(
    name="sogo",
    version="0.1.0",
    tools=[list_folders, search_mail, read_mail, save_draft, upcoming_events],
)
