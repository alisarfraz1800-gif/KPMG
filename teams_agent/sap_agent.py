"""Claude-powered SAP / ABAP assistant.

Holds one append-only message history per Teams conversation and runs the
tool-use loop: Claude can search SAP's public documentation (server-side web
search) and read the team's ABAP sources (client-side tools below).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

import anthropic

from abap_sources import AbapSourceLibrary

logger = logging.getLogger(__name__)

BETAS = [
    "compact-2026-01-12",  # server-side compaction keeps long chats under the context window
    "server-side-fallback-2026-07-01",  # re-run a declined request on Anthropic's recommended fallback
]
MAX_TOOL_ROUNDS = 12

SYSTEM_PROMPT = """You are an SAP and ABAP expert assistant for a KPMG SAP delivery team. \
People ask you questions in Microsoft Teams chats.

Your expertise covers:
- ABAP: classic ABAP, ABAP Objects, modern ABAP syntax (7.40+ inline declarations, \
string templates, constructor expressions), ABAP Cloud / RAP, CDS views, AMDP, BAdIs, \
user exits and enhancements, performance tuning, and ATC / Code Inspector findings.
- SAP Gateway and OData (SEGW, /IWBEP/ runtime methods such as CREATE_DEEP_ENTITY, \
DPC_EXT/MPC_EXT classes), Fiori / SAPUI5, and SAP BTP.
- S/4HANA and ECC functional areas (MM, SD, FI/CO, PP, SRM/RFQ processes), standard \
tables, BAPIs, function modules, and transaction codes.

How to answer:
- Be accurate and practical. When you give ABAP code, it must be syntactically valid, \
and you say which release it needs when that matters (for example 7.40 SP08+).
- For questions about the team's own code, use the ABAP source tools to read the \
relevant file before answering, and cite file names and line numbers.
- When the answer depends on current SAP documentation, release-specific behaviour, or \
anything you are not sure of, search SAP's documentation and cite the pages you used.
- Never invent SAP Note numbers, table names, BAPIs, or transaction codes. If you can't \
verify one, say so.
- Keep answers short enough to read in a chat. Lead with the answer, then the detail.
- Format for Microsoft Teams: short paragraphs, bullet lists, **bold**, and fenced code \
blocks marked as abap. Avoid wide tables and headings deeper than ###.
- If a question is ambiguous (for example, the release, or ECC versus S/4HANA), state \
the assumption you made, or ask one short clarifying question."""


class _Declined(Exception):
    """Claude (and its fallback) declined the request."""


class SapAgent:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        sources: AbapSourceLibrary,
        *,
        model: str = "claude-opus-5-5",
        effort: str = "medium",
        web_search_domains: list[str] | None = None,
    ):
        self._client = client
        self._sources = sources
        self._model = model
        self._effort = effort
        self._histories: dict[str, list[dict]] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

        # Frozen for the life of the process: system and tools must not change
        # mid-conversation, or the prompt cache and earlier thinking are invalidated.
        self._tools: list[dict] = [
            {
                "name": "list_abap_sources",
                "description": "List the team's ABAP source files available to read, with line counts.",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                "strict": True,
            },
            {
                "name": "search_abap_sources",
                "description": (
                    "Case-insensitive regex search across the team's ABAP source files. "
                    "Returns matching lines as path:line: text. Use it to find methods, "
                    "tables, entity sets, or statements before reading a file."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Python regular expression."}
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            {
                "name": "read_abap_source",
                "description": (
                    "Read one of the team's ABAP source files with line numbers. "
                    "Pass a line range to read part of a large file."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path as returned by list_abap_sources."},
                        "start_line": {"type": "integer", "description": "First line, 1-based."},
                        "end_line": {"type": "integer", "description": "Last line, inclusive."},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        ]
        if web_search_domains:
            self._tools.append(
                {
                    "type": "web_search_20260209",
                    "name": "web_search",
                    "max_uses": 5,
                    "allowed_domains": web_search_domains,
                }
            )

    def reset(self, conversation_id: str) -> None:
        self._histories.pop(conversation_id, None)

    async def answer(self, conversation_id: str, question: str) -> str:
        # One turn at a time per conversation, so concurrent messages can't interleave the history.
        async with self._locks[conversation_id]:
            history = self._histories.setdefault(conversation_id, [])
            turn_start = len(history)
            history.append({"role": "user", "content": question})
            try:
                return await self._run_turn(history)
            except _Declined:
                del history[turn_start:]
                return "Sorry, I can't help with that request."
            except Exception:
                # Only the unfinished trailing turn is removed; earlier turns stay untouched.
                del history[turn_start:]
                raise

    async def _run_turn(self, history: list[dict]) -> str:
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._client.beta.messages.create(
                model=self._model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=self._tools,
                messages=history,
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},
                context_management={"edits": [{"type": "compact_20260112"}]},
                cache_control={"type": "ephemeral"},
                fallbacks="default",
                betas=BETAS,
            )

            if response.stop_reason == "refusal":
                logger.warning("Request declined: %s", response.stop_details)
                raise _Declined()

            history.append({"role": "assistant", "content": _content_for_history(response.content)})

            if response.stop_reason == "pause_turn":
                continue  # a long server-side web search paused; resend to resume it

            tool_calls = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason == "tool_use" and tool_calls:
                history.append(
                    {"role": "user", "content": [self._run_tool(call) for call in tool_calls]}
                )
                continue

            reply = _reply_text(response.content)
            if response.stop_reason == "max_tokens":
                reply += "\n\n_(Answer cut off because it was too long. Ask me to continue.)_"
            return reply or "I couldn't produce an answer for that. Could you rephrase it?"

        return "That question needed more lookups than I'm allowed in one turn. Could you narrow it down?"

    def _run_tool(self, call) -> dict:
        args = call.input if isinstance(call.input, dict) else json.loads(call.input)
        try:
            if call.name == "list_abap_sources":
                output = self._sources.list_files()
            elif call.name == "search_abap_sources":
                output = self._sources.search(args["pattern"])
            elif call.name == "read_abap_source":
                output = self._sources.read(args["path"], args.get("start_line", 1), args.get("end_line"))
            else:
                raise ValueError(f"Unknown tool {call.name}")
        except Exception as exc:  # report tool failures to Claude rather than failing the turn
            logger.exception("Tool %s failed", call.name)
            return {"type": "tool_result", "tool_use_id": call.id, "content": str(exc), "is_error": True}
        return {"type": "tool_result", "tool_use_id": call.id, "content": output}


def _content_for_history(content: list) -> list:
    """Content to append to the history, keeping thinking and compaction blocks as-is.

    After a mid-output fallback, blocks from the declined model that precede the
    last fallback marker are not echoed back (text and paired server-tool blocks are).
    """
    fallback_positions = [i for i, b in enumerate(content) if b.type == "fallback"]
    if not fallback_positions:
        return content
    boundary = fallback_positions[-1]
    dropped = {"thinking", "redacted_thinking", "tool_use"}
    return [b for i, b in enumerate(content) if i >= boundary or b.type not in dropped]


def _reply_text(content: list) -> str:
    text = "".join(b.text for b in content if b.type == "text").strip()

    sources: dict[str, str] = {}
    for block in content:
        if block.type != "text":
            continue
        for citation in block.citations or []:
            url = getattr(citation, "url", None)
            if url and url not in sources:
                sources[url] = getattr(citation, "title", None) or url
    if sources:
        text += "\n\n**Sources**\n" + "\n".join(f"- [{title}]({url})" for url, title in sources.items())
    return text
