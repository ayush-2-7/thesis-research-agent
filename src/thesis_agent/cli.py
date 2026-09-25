"""CLI entrypoint: an interactive REPL against the orchestrator."""

from __future__ import annotations

import asyncio

import typer
from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, TextBlock

from thesis_agent.config import WorkspaceConfig
from thesis_agent.orchestrator import build_options

app = typer.Typer(add_completion=False)


async def _run_repl(workspace: str) -> None:
    cfg = WorkspaceConfig.resolve(workspace)
    options = build_options(cfg)

    typer.echo(f"Thesis workspace: {cfg.workspace_dir}")
    typer.echo(f"Audit log: {cfg.audit_log_path}")
    typer.echo(f"Sensitive data dir (blocked from every tool call): {cfg.sensitive_data_dir}")
    if cfg.lockdown_file.exists():
        typer.echo(
            f"LOCKDOWN ACTIVE ({cfg.lockdown_file} exists): all tool calls "
            "will be denied until that file is removed."
        )
    typer.echo("Type a request, or 'exit' to quit.\n")

    async with ClaudeSDKClient(options=options) as client:
        while True:
            try:
                user_input = await asyncio.to_thread(input, "> ")
            except (EOFError, KeyboardInterrupt):
                typer.echo("\nExiting.")
                break

            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                break

            await client.query(user_input)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            typer.echo(block.text)


@app.command()
def chat(
    workspace: str = typer.Option(
        ".",
        "--workspace",
        "-w",
        help="Path to the local thesis workspace this session operates on.",
    ),
) -> None:
    """Start an interactive session with the thesis research agent."""
    asyncio.run(_run_repl(workspace))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
