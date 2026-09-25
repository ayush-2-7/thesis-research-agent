"""Custom arXiv search tool.

Deliberately not a RAG/embeddings pipeline: this calls the public arXiv
export API directly and hands raw abstracts back to the model, which does
the relevance judgment, comparison, and gap analysis itself.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import requests

from claude_agent_sdk import create_sdk_mcp_server, tool

ARXIV_API_URL = "http://export.arxiv.org/api/query"
ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _parse_entry(entry: ET.Element) -> dict[str, Any]:
    arxiv_url = _text(entry.find(f"{ATOM_NS}id"))
    arxiv_id = arxiv_url.rsplit("/", 1)[-1] if arxiv_url else ""

    authors = [
        _text(author.find(f"{ATOM_NS}name"))
        for author in entry.findall(f"{ATOM_NS}author")
    ]

    pdf_url = ""
    for link in entry.findall(f"{ATOM_NS}link"):
        if link.get("title") == "pdf" or link.get("type") == "application/pdf":
            pdf_url = link.get("href", "")
            break

    return {
        "arxiv_id": arxiv_id,
        "title": " ".join(_text(entry.find(f"{ATOM_NS}title")).split()),
        "authors": authors,
        "published": _text(entry.find(f"{ATOM_NS}published")),
        "summary": " ".join(_text(entry.find(f"{ATOM_NS}summary")).split()),
        "pdf_url": pdf_url,
        "abstract_url": arxiv_url,
    }


def fetch_arxiv_text(query: str, max_results: int = 10) -> str:
    """Query arXiv and format the results as plain text.

    Shared by both the in-process SDK tool below and the standalone MCP
    server (mcp_server.py), so the HTTP+parse logic lives in one place.
    """
    max_results = min(max(int(max_results or 10), 1), 50)

    response = requests.get(
        ARXIV_API_URL,
        params={
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": max_results,
            "sortBy": "relevance",
            "sortOrder": "descending",
        },
        timeout=30,
    )
    response.raise_for_status()

    root = ET.fromstring(response.text)
    entries = [_parse_entry(e) for e in root.findall(f"{ATOM_NS}entry")]

    if not entries:
        return f"No arXiv results for: {query}"

    lines = [f"Found {len(entries)} arXiv result(s) for '{query}':\n"]
    for e in entries:
        lines.append(
            f"- [{e['arxiv_id']}] {e['title']} ({e['published'][:10]})\n"
            f"  Authors: {', '.join(e['authors']) or 'unknown'}\n"
            f"  PDF: {e['pdf_url']}\n"
            f"  Abstract: {e['summary']}\n"
        )

    return "\n".join(lines)


@tool(
    "search_papers",
    (
        "Search arXiv for papers matching a query. Returns titles, authors, "
        "publication dates, abstracts, and links, ranked by arXiv relevance. "
        "Use this to find candidate papers, then read the returned abstracts "
        "yourself to judge relevance, extract contributions, and compare "
        "across papers -- do not expect this tool to do that judgment for you."
    ),
    {"query": str, "max_results": int},
)
async def search_papers(args: dict[str, Any]) -> dict[str, Any]:
    text = fetch_arxiv_text(args["query"], args.get("max_results", 10))
    return {"content": [{"type": "text", "text": text}]}


arxiv_server = create_sdk_mcp_server(
    name="arxiv",
    version="0.1.0",
    tools=[search_papers],
)
