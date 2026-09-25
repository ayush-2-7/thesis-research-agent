/* Thesis Agent — browser client. Talks to server.py over one WebSocket. */
(function () {
  "use strict";

  // ------------------------------------------------------------------ data
  const FOCUS = {
    planning: {
      label: "Planning", icon: "planning",
      desc: "Decide what to do next from your files, git history and calendar.",
      placeholder: "What should we plan?",
      suggestions: ["What should I work on next?", "Summarize my progress this week", "What deadlines are coming up?"],
    },
    research: {
      label: "Research", icon: "research",
      desc: "Search arXiv and the web, compare papers, find gaps.",
      placeholder: "What should I look into?",
      suggestions: ["Find recent papers related to my thesis", "What are the open research gaps in my area?", "Compare the main methods used in my field"],
    },
    coding: {
      label: "Coding", icon: "coding",
      desc: "Read, change and test the code in your workspace.",
      placeholder: "What should we build or fix?",
      suggestions: ["Run the tests and fix any failures", "Explain how my analysis code is structured", "Find and clean up dead code"],
    },
    learning: {
      label: "Learning", icon: "learning",
      desc: "Concepts explained simply, tied to your own thesis.",
      placeholder: "What would you like to understand?",
      suggestions: ["Explain a key method from my thesis simply", "Quiz me on the core concepts I use", "What should I read to understand my methods?"],
    },
  };
  const GENERAL_SUGGESTIONS = [
    "What's the current state of my thesis?",
    "Check my email for advisor feedback",
    "What's on my calendar this week?",
  ];

  // ------------------------------------------------------------------ dom
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  };
  const icon = (name) => `<svg><use href="#i-${name}"/></svg>`;
  const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const app = $("app"), thread = $("thread"), empty = $("empty");
  const input = $("input"), composer = $("composer"), sendBtn = $("send");
  const approvalsBox = $("approvals");

  // ------------------------------------------------------------------ state
  const state = {
    ws: null,
    retry: 0,
    session: null,
    focus: null,
    busy: false,
    turn: null,           // current assistant turn view
    approvals: [],        // queued approval requests
    activity: 0,
  };
  try { state.focus = localStorage.getItem("ta-focus") || null; } catch (e) {}
  if (!FOCUS[state.focus]) state.focus = null;

  // ------------------------------------------------------------------ socket
  function connect() {
    setConn("wait", "Connecting…");
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    state.ws = ws;
    ws.onopen = () => {
      state.retry = 0;
      setConn("wait", "Starting session…");
      send({ type: "hello" });
    };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      const fn = handlers[msg.type];
      if (fn) fn(msg);
    };
    ws.onclose = () => {
      setConn("bad", "Disconnected — retrying…");
      setReady(false);
      if (state.busy) endTurn({ cancelled: true });
      clearApprovals();
      const wasOpen = !!state.session;
      state.session = null;
      const delay = Math.min(8000, 800 * 2 ** state.retry++);
      setTimeout(() => {
        if (wasOpen) addSeparator("Reconnected — new session");
        connect();
      }, delay);
    };
  }
  const send = (obj) => {
    if (state.ws && state.ws.readyState === 1) state.ws.send(JSON.stringify(obj));
  };

  function setConn(kind, title) {
    const c = $("conn");
    c.className = "conn " + kind;
    c.title = title;
  }

  // ------------------------------------------------------------------ handlers
  const handlers = {
    session(m) {
      state.session = m;
      setConn("ok", "Connected");
      $("ws-name").textContent = m.workspace_name || m.workspace;
      $("ws-path").textContent = tildify(m.workspace);
      $("ws-card").title = m.workspace + " — click to change";
      $("top-ws").textContent = m.workspace_name || "Thesis Agent";
      $("protected-path").textContent = shortPath(m.protected, m.workspace);
      $("protected").title = "No agent tool can read " + m.protected;
      setLock(m.locked);
      closeWorkspaceDialog();
      setReady(true);
      input.focus();
    },
    need_workspace(m) {
      setConn("ok", "Connected — choose a workspace");
      openWorkspaceDialog(m.recents || [], m.error, !state.session);
    },
    turn_start() { /* view is created optimistically on submit */ },
    text(m) { turnView().addText(m.text, m.parent); },
    tool_start(m) { turnView().toolStart(m); },
    tool_end(m) { turnView().toolEnd(m); },
    turn_end(m) { endTurn(m); },
    approval_request(m) { enqueueApproval(m); },
    approval_resolved(m) { dropApproval(m.id); },
    audit(m) { addActivity(m); },
    lockdown(m) {
      setLock(m.active);
      toast(m.active ? "Lockdown engaged — all tool calls blocked" : "Lockdown lifted", m.active ? "lock" : "check", m.active);
    },
    error(m) {
      if (state.turn) state.turn.error(m.message);
      else toast(m.message, "alert", true);
    },
  };

  // ------------------------------------------------------------------ helpers
  // /Users/<name>/x or /home/<name>/x -> ~/x (display only).
  function tildify(p) {
    return String(p || "").replace(/^\/(Users|home)\/[^/]+(?=\/|$)/, "~");
  }

  // Workspace-relative for display: /…/eGDM/Repo/x.py -> Repo/x.py
  function relPath(text) {
    const ws = state.session && state.session.workspace;
    if (!ws || !text) return text;
    return text.split(ws + "/").join("").split(ws).join(".");
  }

  // Audit summaries are often a JSON dump of the tool input; show the one
  // field a human cares about instead.
  function prettySummary(summary) {
    try {
      const o = JSON.parse(summary);
      if (o && typeof o === "object") {
        for (const k of ["description", "file_path", "pattern", "command", "query", "url", "subject", "sender", "folder"]) {
          if (typeof o[k] === "string" && o[k].trim()) return relPath(o[k]);
        }
      }
    } catch (e) { /* not JSON: plain summary */ }
    return relPath(summary || "");
  }

  function shortPath(p, workspace) {
    if (!p) return "—";
    if (workspace && p.startsWith(workspace + "/")) return "./" + p.slice(workspace.length + 1);
    return p;
  }

  function setReady(ready) {
    input.disabled = !ready;
    sendBtn.disabled = !ready || (!state.busy && !input.value.trim());
  }

  function setLock(active) {
    $("lockdown").checked = !!active;
    $("lock-banner").hidden = !active;
  }

  function scrollDown(force) {
    const nearBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 160;
    if (force || nearBottom) thread.scrollTop = thread.scrollHeight;
  }

  function greeting() {
    const h = new Date().getHours();
    return h < 5 ? "Working late?" : h < 12 ? "Good morning." : h < 18 ? "Good afternoon." : "Good evening.";
  }

  function fmtTime(iso) {
    const d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function fmtAgent(id) {
    if (!id || id === "orchestrator") return "orchestrator";
    return id.length > 10 ? "subagent " + id.slice(0, 6) : id;
  }

  // ------------------------------------------------------------------ focus
  function renderFocus() {
    const pills = $("focus-pills");
    pills.innerHTML = "";
    const cards = $("focus-cards");
    cards.innerHTML = "";
    for (const [key, f] of Object.entries(FOCUS)) {
      const on = state.focus === key;
      const pill = el("button", "pill", `${icon(f.icon)}<span>${f.label}</span>`);
      pill.type = "button";
      pill.setAttribute("role", "radio");
      pill.setAttribute("aria-checked", String(on));
      pill.onclick = () => setFocus(on ? null : key);
      pills.appendChild(pill);

      const card = el("button", "fcard",
        `<span class="fcard-icon">${icon(f.icon)}</span>` +
        `<span class="fcard-text"><span class="fcard-title">${f.label}</span>` +
        `<span class="fcard-desc">${f.desc}</span></span>`);
      card.type = "button";
      card.setAttribute("aria-pressed", String(on));
      card.onclick = () => { setFocus(on ? null : key); input.focus(); };
      cards.appendChild(card);
    }
    const chip = $("focus-chip");
    if (state.focus) {
      const f = FOCUS[state.focus];
      chip.innerHTML = icon(f.icon) + f.label;
      chip.hidden = false;
    } else {
      chip.hidden = true;
    }
    input.placeholder = state.focus ? FOCUS[state.focus].placeholder : "Ask anything about your thesis…";
    renderSuggestions();
  }

  function renderSuggestions() {
    const box = $("suggestions");
    box.innerHTML = "";
    const list = state.focus ? FOCUS[state.focus].suggestions : GENERAL_SUGGESTIONS;
    list.forEach((text, i) => {
      const chip = el("button", "chip");
      chip.type = "button";
      chip.textContent = text;
      chip.style.animationDelay = `${i * 60}ms`;
      chip.onclick = () => submit(text);
      box.appendChild(chip);
    });
  }

  function setFocus(key) {
    state.focus = key;
    try { key ? localStorage.setItem("ta-focus", key) : localStorage.removeItem("ta-focus"); } catch (e) {}
    renderFocus();
  }

  // ------------------------------------------------------------------ turns
  function turnView() {
    if (!state.turn) state.turn = new Turn();
    return state.turn;
  }

  function submit(text) {
    text = (text ?? input.value).trim();
    if (!text || state.busy || !state.session) return;
    empty.hidden = true;
    addUserMessage(text, state.focus);
    input.value = "";
    autosize();
    state.busy = true;
    composer.classList.add("busy");
    sendBtn.disabled = false;
    sendBtn.setAttribute("aria-label", "Stop");
    state.turn = new Turn();
    send({ type: "prompt", text, focus: state.focus });
    closeNav();
  }

  function endTurn(stats) {
    if (state.turn) state.turn.finish(stats || {});
    state.turn = null;
    state.busy = false;
    composer.classList.remove("busy");
    sendBtn.setAttribute("aria-label", "Send");
    setReady(!!state.session);
  }

  function addUserMessage(text, focus) {
    const wrap = el("div", "msg user");
    const bubble = el("div", "bubble");
    if (focus) bubble.appendChild(el("span", "bubble-focus", FOCUS[focus].label));
    bubble.appendChild(document.createTextNode(text));
    wrap.appendChild(bubble);
    thread.appendChild(wrap);
    scrollDown(true);
  }

  function addSeparator(text) {
    const s = el("div", "sep");
    s.textContent = text;
    thread.appendChild(s);
  }

  class Turn {
    constructor() {
      this.started = performance.now();
      this.steps = new Map();     // tool id -> {li, children, row, body}
      this.count = 0;
      this.pendingText = null;    // top-level text not yet followed by a tool
      this.hasAnswer = false;

      this.root = el("article", "msg assistant running");
      this.root.innerHTML = `<div class="avatar">${icon("logo")}</div>`;
      const body = el("div", "msg-body");
      this.stepsBox = el("div", "steps open");
      this.stepsBox.hidden = true;
      this.stepsHead = el("button", "steps-head",
        `<span class="spinner"></span><span class="steps-title">Working</span>` +
        `<span class="steps-meta"></span><svg class="chev"><use href="#i-chevron"/></svg>`);
      this.stepsHead.type = "button";
      this.stepsHead.onclick = () => this.stepsBox.classList.toggle("open");
      const sb = el("div", "steps-body");
      const inner = el("div", "steps-inner");
      this.list = el("ol", "step-list");
      inner.appendChild(this.list);
      sb.appendChild(inner);
      this.stepsBox.append(this.stepsHead, sb);
      this.answer = el("div", "answer");
      this.typing = el("div", "typing", "<i></i><i></i><i></i>");
      this.foot = el("div", "msg-foot");
      body.append(this.stepsBox, this.answer, this.typing, this.foot);
      this.root.appendChild(body);
      thread.appendChild(this.root);
      scrollDown(true);
    }

    addText(text, parent) {
      if (parent && this.steps.has(parent)) {
        const note = el("li", "note");
        note.appendChild(el("div", "md", window.renderMarkdown(text)));
        this.steps.get(parent).children.appendChild(note);
      } else {
        const block = el("div", "md", window.renderMarkdown(text));
        this.answer.appendChild(block);
        this.pendingText = block;
        this.hasAnswer = true;
        this.typing.remove();
      }
      scrollDown();
    }

    toolStart(m) {
      // Text the orchestrator wrote *before* delegating ("Let me check…")
      // is narration, not the answer: tuck it into the steps as a note.
      if (!m.parent && this.pendingText) {
        const note = el("li", "note");
        note.appendChild(this.pendingText);
        this.list.appendChild(note);
        this.pendingText = null;
        this.hasAnswer = this.answer.childElementCount > 0;
      }
      this.stepsBox.hidden = false;
      this.count++;
      const isAgent = m.kind === "agent";
      const li = el("li", "step");
      li.dataset.state = "running";
      li.dataset.kind = m.kind;
      const label = isAgent ? `Delegated to ${escapeHtml(m.subagent || "subagent")}` : escapeHtml(m.label);
      const row = el("button", "step-row",
        `<span class="step-icon">${icon(m.kind)}</span>` +
        `<span class="step-label">${label}</span>` +
        `<span class="step-detail">${escapeHtml(relPath(m.detail || ""))}</span>` +
        `<span class="step-status"><span class="spinner"></span></span>`);
      row.type = "button";
      row.title = m.detail || m.label;
      const body = el("div", "step-body");
      body.hidden = true;
      body.innerHTML = `<div><div class="sec-title">Input</div><pre>${escapeHtml(m.input)}</pre></div>`;
      row.onclick = () => { body.hidden = !body.hidden; };
      const children = el("ol", "step-children");
      li.append(row, body);
      if (isAgent) li.appendChild(children);
      const parent = m.parent && this.steps.get(m.parent);
      (parent ? parent.children : this.list).appendChild(li);
      this.steps.set(m.id, { li, row, body, children });
      // The approval can arrive before its step is streamed.
      if (state.approvals.some((a) => a.tool_use_id === m.id)) this.markWaiting(m.id, true);
      this.setMeta(isAgent ? `Delegating to ${m.subagent || "subagent"}` : `${m.label} ${m.detail || ""}`);
      scrollDown();
    }

    toolEnd(m) {
      const step = this.steps.get(m.id);
      if (!step) return;
      step.li.dataset.state = m.is_error ? "error" : "done";
      step.row.querySelector(".step-status").innerHTML = icon(m.is_error ? "x" : "check");
      const wait = step.row.querySelector(".step-wait");
      if (wait) wait.remove();
      if (m.output) {
        const sec = el("div", "", `<div class="sec-title">${m.is_error ? "Error" : "Result"}</div>`);
        const pre = el("pre");
        pre.textContent = m.output;
        sec.appendChild(pre);
        step.body.appendChild(sec);
      }
    }

    markWaiting(toolUseId, waiting) {
      const step = toolUseId && this.steps.get(toolUseId);
      if (!step) return;
      if (waiting) {
        step.li.dataset.state = "waiting";
        if (!step.row.querySelector(".step-wait")) {
          step.row.querySelector(".step-status").insertAdjacentHTML("beforebegin", `<span class="step-wait">Waiting for you</span>`);
        }
      } else if (step.li.dataset.state === "waiting") {
        step.li.dataset.state = "running";
        const w = step.row.querySelector(".step-wait");
        if (w) w.remove();
      }
    }

    setMeta(text) {
      this.stepsHead.querySelector(".steps-meta").textContent = "· " + text.trim();
    }

    error(message) {
      this.answer.insertAdjacentHTML("beforeend",
        `<div class="err-note">${icon("alert")}<span>${escapeHtml(message)}</span></div>`);
      this.typing.remove();
    }

    finish(stats) {
      this.root.classList.remove("running");
      this.typing.remove();
      const secs = ((performance.now() - this.started) / 1000).toFixed(1);
      const head = this.stepsHead;
      head.querySelector(".spinner").outerHTML = icon("check");
      head.querySelector(".steps-title").textContent =
        `Used ${this.count} ${this.count === 1 ? "tool" : "tools"}`;
      this.setMeta(`${secs}s`);
      for (const { li, row } of this.steps.values()) {
        if (li.dataset.state === "running" || li.dataset.state === "waiting") {
          li.dataset.state = "error";
          row.querySelector(".step-status").innerHTML = icon("x");
          const w = row.querySelector(".step-wait");
          if (w) w.remove();
        }
      }
      if (this.hasAnswer) this.stepsBox.classList.remove("open");
      if (stats.cancelled) {
        this.foot.innerHTML = `<span>Stopped</span>`;
      } else if (!this.hasAnswer && !this.answer.querySelector(".err-note")) {
        this.answer.innerHTML = `<div class="md"><p><em>No answer was returned.</em></p></div>`;
      }
      const bits = [`${secs}s`];
      if (typeof stats.cost_usd === "number") bits.push(`$${stats.cost_usd.toFixed(3)}`);
      if (!stats.cancelled) this.foot.innerHTML = bits.map((b) => `<span>${b}</span>`).join("");
      scrollDown();
    }
  }

  // ------------------------------------------------------------------ approvals
  function enqueueApproval(m) {
    state.approvals.push(m);
    if (state.turn) state.turn.markWaiting(m.tool_use_id, true);
    renderApproval();
    notifyAttention();
  }

  function dropApproval(id) {
    const idx = state.approvals.findIndex((a) => a.id === id);
    if (idx < 0) return;
    const [a] = state.approvals.splice(idx, 1);
    if (state.turn) state.turn.markWaiting(a.tool_use_id, false);
    renderApproval(true);
  }

  function clearApprovals() {
    state.approvals = [];
    approvalsBox.innerHTML = "";
    notifyAttention();
  }

  function decide(id, decision) {
    const card = approvalsBox.querySelector(".approval");
    if (card) card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    send({ type: "approval", id, decision });
  }

  function renderApproval(animateOut) {
    const current = state.approvals[0];
    const old = approvalsBox.querySelector(".approval");
    const draw = () => {
      approvalsBox.innerHTML = "";
      notifyAttention();
      if (!current) return;
      const total = state.approvals.length;
      const card = el("div", "approval");
      card.setAttribute("role", "alertdialog");
      card.setAttribute("aria-label", `Approval needed: ${current.label}`);
      card.innerHTML =
        `<div class="ap-head">` +
          `<span class="ap-icon">${icon(current.kind)}</span>` +
          `<div class="ap-titles"><div class="ap-kicker">Approval needed</div>` +
          `<div class="ap-title">Allow <b>${escapeHtml(current.label)}</b>` +
          `<span class="ap-agent">${escapeHtml(fmtAgent(current.agent))}</span></div></div>` +
          (total > 1 ? `<span class="ap-count">1 of ${total}</span>` : "") +
        `</div>` +
        `<pre class="ap-summary"></pre>` +
        (current.domain_warning
          ? `<div class="ap-warn">${icon("alert")}<span>Domain not on the known-safe list: <code>${escapeHtml(current.domain_warning)}</code></span></div>`
          : "") +
        `<div class="ap-actions"><span class="ap-note">Security checks already passed · <kbd>Esc</kbd> denies</span>` +
          `<button type="button" class="btn ghost" data-deny>Deny</button>` +
          `<button type="button" class="btn primary" data-allow>${icon("check")}Allow</button></div>`;
      card.querySelector(".ap-summary").textContent = current.summary || current.tool;
      card.querySelector("[data-allow]").onclick = () => decide(current.id, "allow");
      card.querySelector("[data-deny]").onclick = () => decide(current.id, "deny");
      approvalsBox.appendChild(card);
      scrollDown(true);
    };
    if (animateOut && old) {
      old.classList.add("leaving");
      setTimeout(draw, 200);
    } else {
      draw();
    }
  }

  let titleTimer = null;
  function notifyAttention() {
    clearInterval(titleTimer);
    const n = state.approvals.length;
    if (!n) { document.title = "Thesis Agent"; return; }
    let flip = false;
    const alertTitle = `(${n}) Approval needed · Thesis Agent`;
    document.title = alertTitle;
    if (document.hidden) {
      titleTimer = setInterval(() => {
        document.title = (flip = !flip) ? "⚠ Waiting for you" : alertTitle;
      }, 1200);
    }
  }
  document.addEventListener("visibilitychange", notifyAttention);

  // ------------------------------------------------------------------ activity
  function addActivity(m) {
    const list = $("act-list");
    $("act-empty").hidden = true;
    const kind = m.blocked ? "block" : m.decision === "allow" ? "allow" : "deny";
    const li = el("li", "act " + kind);
    const tag = kind === "block" ? `<span class="act-tag">Blocked</span>` : kind === "deny" ? `<span class="act-tag">Denied</span>` : "";
    li.innerHTML =
      `<span class="act-icon">${icon(kind === "allow" ? "check" : kind === "block" ? "ban" : "x")}</span>` +
      `<div style="min-width:0"><div class="act-top"><span class="act-tool">${escapeHtml(m.label)}</span>${tag}` +
      `<span class="act-agent">${escapeHtml(fmtAgent(m.agent))}</span><time>${fmtTime(m.time)}</time></div>` +
      `<div class="act-sum"></div></div>`;
    const sum = li.querySelector(".act-sum");
    sum.textContent = m.reason && kind === "block" ? m.reason : prettySummary(m.summary);
    li.title = (m.reason ? m.reason + "\n\n" : "") + m.summary;
    list.prepend(li);
    while (list.children.length > 200) list.lastChild.remove();
    $("act-count").textContent = String(++state.activity);
  }

  // ------------------------------------------------------------------ workspace dialog
  const wsDialog = $("ws-dialog");
  function openWorkspaceDialog(recents, error, required) {
    $("ws-error").hidden = !error;
    $("ws-error").textContent = error || "";
    $("ws-cancel").hidden = !!required;
    wsDialog.dataset.required = required ? "1" : "";
    const box = $("recents");
    box.innerHTML = "";
    (recents || []).slice(0, 6).forEach((p) => {
      const b = el("button", "recent", `${icon("folder")}<span></span>`);
      b.type = "button";
      b.querySelector("span").textContent = tildify(p);
      b.title = p;
      b.onclick = () => openWorkspace(p);
      box.appendChild(b);
    });
    if (!wsDialog.open) wsDialog.showModal();
    setTimeout(() => $("ws-input").focus(), 50);
  }
  function closeWorkspaceDialog() { if (wsDialog.open) wsDialog.close(); }
  function openWorkspace(path) {
    if (!path.trim()) return;
    $("ws-open").disabled = true;
    empty.hidden = false;
    thread.querySelectorAll(".msg, .sep").forEach((n) => n.remove());
    send({ type: "set_workspace", path: path.trim() });
    setTimeout(() => ($("ws-open").disabled = false), 1500);
  }
  $("ws-form").addEventListener("submit", (e) => {
    e.preventDefault();
    openWorkspace($("ws-input").value);
  });
  $("ws-cancel").onclick = () => closeWorkspaceDialog();
  wsDialog.addEventListener("cancel", (e) => { if (wsDialog.dataset.required) e.preventDefault(); });
  $("ws-card").onclick = () => {
    if (state.busy) return toast("Wait for the current answer to finish", "alert", true);
    openWorkspaceDialog(state.session ? state.session.recents : [], null, false);
    $("ws-input").value = state.session ? state.session.workspace : "";
  };

  // ------------------------------------------------------------------ toasts
  function toast(text, iconName, bad) {
    const t = el("div", "toast" + (bad ? " bad" : ""), icon(iconName || "check") + `<span></span>`);
    t.querySelector("span").textContent = text;
    $("toasts").appendChild(t);
    setTimeout(() => { t.classList.add("leaving"); setTimeout(() => t.remove(), 260); }, 3200);
  }

  // ------------------------------------------------------------------ composer
  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 220) + "px";
    if (!state.busy) sendBtn.disabled = input.disabled || !input.value.trim();
  }
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (!state.busy) submit();
    }
  });
  composer.addEventListener("submit", (e) => {
    e.preventDefault();
    if (state.busy) send({ type: "interrupt" });
    else submit();
  });

  // ------------------------------------------------------------------ misc wiring
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.approvals.length && !wsDialog.open) {
      e.preventDefault();
      decide(state.approvals[0].id, "deny");
    }
  });

  thread.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-copy]");
    if (!btn) return;
    const code = btn.closest(".code").querySelector("code").textContent;
    navigator.clipboard.writeText(code).then(() => {
      btn.lastChild.textContent = "Copied";
      setTimeout(() => (btn.lastChild.textContent = "Copy"), 1400);
    });
  });

  $("lockdown").addEventListener("change", (e) => send({ type: "lockdown", active: e.target.checked }));
  $("unlock-btn").onclick = () => send({ type: "lockdown", active: false });

  $("new-btn").onclick = () => {
    if (!state.session) return;
    if (state.busy) send({ type: "interrupt" });
    thread.querySelectorAll(".msg, .sep").forEach((n) => n.remove());
    empty.hidden = false;
    clearApprovals();
    setReady(false);
    send({ type: "reset" });
    toast("Started a new conversation", "plus");
  };

  $("theme-btn").onclick = () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("ta-theme", next); } catch (e) {}
  };

  const closeNav = () => app.classList.remove("nav-open");
  $("menu-btn").onclick = () => app.classList.add("nav-open");
  $("scrim").onclick = closeNav;

  // ------------------------------------------------------------------ boot
  $("greeting").textContent = greeting();
  renderFocus();
  connect();
})();
