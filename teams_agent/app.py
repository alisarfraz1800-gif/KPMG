"""Microsoft Teams bridge for the SAP / ABAP assistant.

Teams -> Azure Bot Service -> POST /api/messages -> this app -> Claude.

The bot answers 1:1 chats, and in group chats and channels it answers when
@mentioned. Each question is acknowledged right away and answered in the
background, so slow answers don't hit the channel's 15-second request timeout.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import anthropic
from aiohttp.web import Application, Request, Response, json_response, middleware, run_app
from dotenv import load_dotenv
from microsoft_agents.activity import ActivityTypes, load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import CloudAdapter, jwt_authorization_middleware, start_agent_process
from microsoft_agents.hosting.core import (
    AgentApplication,
    MemoryStorage,
    TurnContext,
    TurnState,
    TypingIndicator,
)

from abap_sources import AbapSourceLibrary
from sap_agent import SapAgent

load_dotenv()
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("teams_agent")

# Teams rejects messages much over ~28 KB; stay well below it.
TEAMS_MESSAGE_LIMIT = 20_000
HELP_TEXT = (
    "Hi! I'm the SAP / ABAP assistant. Ask me about ABAP code, ATC findings, "
    "OData / Gateway, CDS, RAP, Fiori, or SAP functional topics. I can also read "
    "the team's ABAP sources.\n\n"
    "- In a group chat or channel, @mention me with your question.\n"
    "- Type **reset** to start a fresh conversation."
)

agents_sdk_config = load_configuration_from_env(os.environ)
CONNECTION_MANAGER = MsalConnectionManager(**agents_sdk_config)
ADAPTER = CloudAdapter(connection_manager=CONNECTION_MANAGER)
AGENT_APP = AgentApplication[TurnState](
    storage=MemoryStorage(),
    connection_manager=CONNECTION_MANAGER,
    # The typing indicator runs in the background task that writes the answer.
    start_typing_timer=False,
    **agents_sdk_config,
)

SAP_AGENT = SapAgent(
    anthropic.AsyncAnthropic(),
    AbapSourceLibrary(os.environ.get("ABAP_SOURCE_DIR", Path(__file__).resolve().parent.parent)),
    model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"),
    effort=os.environ.get("CLAUDE_EFFORT", "medium"),
    web_search_domains=[
        d.strip()
        for d in os.environ.get(
            "SAP_WEB_SEARCH_DOMAINS",
            "help.sap.com,community.sap.com,learning.sap.com,developers.sap.com,api.sap.com",
        ).split(",")
        if d.strip()
    ],
)

_background_tasks: set[asyncio.Task] = set()


@AGENT_APP.conversation_update("membersAdded")
async def on_members_added(context: TurnContext, _state: TurnState):
    for member in context.activity.members_added or []:
        if member.id != context.activity.recipient.id:
            await context.send_activity(HELP_TEXT)


@AGENT_APP.activity(ActivityTypes.message)
async def on_message(context: TurnContext, _state: TurnState):
    question = (context.activity.text or "").strip()  # the @mention is already stripped
    conversation_id = context.activity.conversation.id

    if not question or question.lower() in {"help", "/help", "hi", "hello"}:
        await context.send_activity(HELP_TEXT)
        return
    if question.lower() in {"reset", "/reset"}:
        SAP_AGENT.reset(conversation_id)
        await context.send_activity("Okay, I've cleared our conversation. What's your question?")
        return

    task = asyncio.create_task(
        ADAPTER.continue_conversation_with_claims(
            claims_identity=context.identity,
            continuation_activity=context.activity,
            callback=lambda ctx: _answer(ctx, conversation_id, question),
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _answer(context: TurnContext, conversation_id: str, question: str) -> None:
    try:
        async with TypingIndicator(context):
            reply = await SAP_AGENT.answer(conversation_id, question)
    except anthropic.RateLimitError:
        logger.warning("Claude rate limit hit")
        reply = "I'm getting a lot of questions right now. Please try again in a minute."
    except anthropic.APIConnectionError:
        logger.exception("Could not reach the Claude API")
        reply = "I couldn't reach my language model just now. Please try again shortly."
    except anthropic.APIStatusError as exc:
        logger.exception("Claude API error %s (request id %s)", exc.status_code, exc.request_id)
        reply = "Something went wrong while I was answering. Please try again."
    except Exception:
        logger.exception("Failed to answer question")
        reply = "Something went wrong while I was answering. Please try again."

    for chunk in split_message(reply):
        await context.send_activity(chunk)


def split_message(text: str, limit: int = TEAMS_MESSAGE_LIMIT) -> list[str]:
    """Split a long reply on paragraph boundaries, keeping each piece under the Teams size limit."""
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        while len(paragraph) > limit:  # a single huge paragraph or code block
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) > limit:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


@AGENT_APP.error
async def on_error(context: TurnContext, error: Exception):
    logger.exception("Unhandled error in turn", exc_info=error)
    await context.send_activity("Sorry, something went wrong on my side.")


async def messages(request: Request) -> Response:
    return await start_agent_process(request, AGENT_APP, ADAPTER)


async def health(_request: Request) -> Response:
    return json_response({"status": "ok"})


@middleware
async def authorize(request: Request, handler):
    # Bot Service requests must carry a valid token; the health probe doesn't.
    if request.path == "/health":
        return await handler(request)
    return await jwt_authorization_middleware(request, handler)


def create_app() -> Application:
    app = Application(middlewares=[authorize])
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/health", health)
    app["agent_configuration"] = CONNECTION_MANAGER.get_default_connection_configuration()
    return app


if __name__ == "__main__":
    run_app(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "3978")))
