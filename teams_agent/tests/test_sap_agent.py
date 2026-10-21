import asyncio
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from abap_sources import AbapSourceLibrary
from sap_agent import SapAgent


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        # Snapshot the history as sent, since the agent keeps appending to the same list.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def fake_client(*responses):
    messages = FakeMessages(responses)
    return NS(beta=NS(messages=messages)), messages


def text(value, citations=None):
    return NS(type="text", text=value, citations=citations)


def response(stop_reason, *content, stop_details=None):
    return NS(stop_reason=stop_reason, content=list(content), stop_details=stop_details)


@pytest.fixture
def sources(tmp_path: Path) -> AbapSourceLibrary:
    (tmp_path / "zbid.abap").write_text("method create.\n  select * from zrfq_head_bid_re.\nendmethod.\n")
    return AbapSourceLibrary(tmp_path)


def test_runs_client_tools_and_returns_final_text(sources):
    tool_call = NS(type="tool_use", id="tu_1", name="search_abap_sources", input={"pattern": "select \\*"})
    client, messages = fake_client(
        response("tool_use", text("Let me look."), tool_call),
        response("end_turn", text("Line 2 uses SELECT *.")),
    )
    agent = SapAgent(client, sources)

    reply = asyncio.run(agent.answer("conv", "Where do we use SELECT *?"))

    assert reply == "Line 2 uses SELECT *."
    tool_result = messages.calls[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "tu_1"
    assert "zbid.abap:2:" in tool_result["content"]
    # Request shape: frozen system/tools, adaptive thinking, compaction and fallbacks.
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default"
    assert call["context_management"] == {"edits": [{"type": "compact_20260112"}]}
    assert messages.calls[0]["tools"] is messages.calls[1]["tools"]


def test_history_is_append_only_across_turns(sources):
    client, messages = fake_client(
        response("end_turn", text("First answer")),
        response("end_turn", text("Second answer")),
    )
    agent = SapAgent(client, sources)

    asyncio.run(agent.answer("conv", "Q1"))
    asyncio.run(agent.answer("conv", "Q2"))

    first, second = messages.calls[0]["messages"], messages.calls[1]["messages"]
    assert second[: len(first)] == first
    assert [m["role"] for m in second] == ["user", "assistant", "user"]


def test_refusal_rolls_back_the_whole_turn(sources):
    tool_call = NS(type="tool_use", id="tu_1", name="list_abap_sources", input={})
    client, messages = fake_client(
        response("end_turn", text("Hello")),
        response("tool_use", tool_call),
        response("refusal", stop_details=NS(category="cyber")),
        response("end_turn", text("Next")),
    )
    agent = SapAgent(client, sources)

    asyncio.run(agent.answer("conv", "Q1"))
    assert asyncio.run(agent.answer("conv", "bad")) == "Sorry, I can't help with that request."
    asyncio.run(agent.answer("conv", "Q3"))

    assert [m["content"] for m in messages.calls[3]["messages"] if m["role"] == "user"] == ["Q1", "Q3"]


def test_pause_turn_is_resumed_and_citations_listed(sources):
    citation = NS(type="web_search_result_location", url="https://help.sap.com/x", title="SAP Help")
    client, messages = fake_client(
        response("pause_turn", NS(type="server_tool_use", id="st_1")),
        response("end_turn", text("Use CL_ABAP_...", citations=[citation])),
    )
    agent = SapAgent(client, sources, web_search_domains=["help.sap.com"])

    reply = asyncio.run(agent.answer("conv", "Q"))

    assert len(messages.calls) == 2
    assert messages.calls[1]["messages"][-1]["role"] == "assistant"
    assert "[SAP Help](https://help.sap.com/x)" in reply
    assert messages.calls[0]["tools"][-1]["allowed_domains"] == ["help.sap.com"]


def test_source_reader_stays_inside_root(sources):
    assert "No source file" in sources.read("../../etc/passwd")
    assert "    2    select * from zrfq_head_bid_re." in sources.read("zbid.abap", 2, 2)
