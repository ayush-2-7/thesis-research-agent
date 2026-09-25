# Thesis Research Agent

A local, multi-agent workspace for thesis work — running on **your** machine,
reusing your `claude login` (no API key).

The session opens straight into your workspace (set once with
`thesis-agent-web --workspace <folder>`; it's remembered after that). Pick a
focus — 🗓 **Planning**, 🔎 **Research**, 💻 **Coding**, 📚 **Learning** — so
requests go straight to the right agent, or just type:

- *"What's the current state of my thesis?"*
- *"Find recent papers on target-trial emulation relevant to my thesis."*
- *"What should I work on next?"*
- *"Add a `subtract` function to `src/analysis.py` and run it."*
- *"Check my university email for advisor feedback."*
- *"What's on my calendar in the next two weeks?"*

**Anything that writes files, runs shell commands, or saves an email draft
pauses for your Allow/Deny. Email can never be sent from here** -- drafts land
in your SOGo Drafts folder for you to send. (Mail needs the Uni VPN when off
campus.) The sidebar shows a live audit trail, the protected
sensitive-data directory, and a 🔴 **LOCKDOWN** kill switch that freezes every
tool call instantly.
