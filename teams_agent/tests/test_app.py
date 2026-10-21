"""End-to-end check of the Teams bridge: a Bot Service style request comes in, and the
reply is posted back to a fake Bot Connector, with Claude replaced by a stub."""

import asyncio
import os

from aiohttp import ClientSession, web

os.environ["CONNECTIONS__SERVICE_CONNECTION__SETTINGS__ANONYMOUS_ALLOWED"] = "true"
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import app  # noqa: E402


def test_split_message_keeps_chunks_under_limit():
    text = "\n\n".join(["a" * 30, "b" * 30, "c" * 100])
    chunks = app.split_message(text, limit=70)
    assert all(len(c) <= 70 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_message_is_answered_through_the_connector(monkeypatch):
    questions = []

    async def fake_answer(conversation_id, question):
        questions.append((conversation_id, question))
        return "Use **FOR ALL ENTRIES** with a non-empty driver table."

    monkeypatch.setattr(app.SAP_AGENT, "answer", fake_answer)
    asyncio.run(_exercise_bot())

    assert questions == [("conv-1", "How do I avoid SELECT in a loop?")]


async def _exercise_bot():
    received = []
    got_reply = asyncio.Event()

    async def connector(request: web.Request):
        activity = await request.json()
        received.append(activity)
        if activity.get("type") == "message":
            got_reply.set()
        return web.json_response({"id": "reply-1"})

    connector_app = web.Application()
    connector_app.router.add_post("/v3/conversations/{conversation_id}/activities/{activity_id}", connector)
    connector_app.router.add_post("/v3/conversations/{conversation_id}/activities", connector)

    runners = []
    for application, port in ((connector_app, 39781), (app.create_app(), 39782)):
        runner = web.AppRunner(application)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        runners.append(runner)

    try:
        incoming = {
            "type": "message",
            "id": "msg-1",
            "channelId": "msteams",
            "serviceUrl": "http://127.0.0.1:39781/",
            "from": {"id": "user-1", "name": "Tester"},
            "recipient": {"id": "bot-1", "name": "SAP Assistant"},
            "conversation": {"id": "conv-1"},
            "text": "<at>SAP Assistant</at> How do I avoid SELECT in a loop?",
            "entities": [
                {
                    "type": "mention",
                    "mentioned": {"id": "bot-1", "name": "SAP Assistant"},
                    "text": "<at>SAP Assistant</at>",
                }
            ],
        }
        async with ClientSession() as session:
            async with session.post("http://127.0.0.1:39782/api/messages", json=incoming) as resp:
                assert resp.status in (200, 202)
            async with session.get("http://127.0.0.1:39782/health") as resp:
                assert resp.status == 200

        await asyncio.wait_for(got_reply.wait(), timeout=10)
        replies = [a for a in received if a.get("type") == "message"]
        assert replies[0]["text"].startswith("Use **FOR ALL ENTRIES**")
        assert replies[0]["conversation"]["id"] == "conv-1"
    finally:
        for runner in runners:
            await runner.cleanup()
